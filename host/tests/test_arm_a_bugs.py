"""検出専用: Cortex-A / ARM11 の JTAG 直接デバッグ・JTAG の層の不具合の再現（台帳 docs/reports/bugs/arm-a-jtag-sampler-xtrig.md）。
どれも xfail(strict=True): 直ったら XPASS で失敗するので、marker を外して通常の試験にする。"""

import pytest
from arm_a_fakes import ArmCore
from pyocd.core.target import Target
from test_arm_a import connect, pi2


def _breakpoint_at_restart_pc(monkeypatch):
    """実機の挙動の模型: 再開した命令の番地に一致のブレークポイント（BT = 0）が有効なら、1 命令も進まず、すぐ止まる
    （ARMv7 の外部デバッグ。OpenOCD の cortex_a_step / resume が現在の PC のブレークポイントを外してから再開する理由）。"""
    original = ArmCore.restart

    def restart(self):
        if self.halted:
            for i in range(self.brps):
                if self.bcr[i] & 1 and (self.bcr[i] >> 20) & 0xF == 0 and self.bvr[i] == self.regs[15] & ~3:
                    self.restarts += 1
                    self.restarted, self.moe = True, 1          # 停止のまま（再開してすぐ同じ番地で止まり直す）
                    return
        original(self)
    monkeypatch.setattr(ArmCore, "restart", restart)


def test_v7_step_from_an_address_with_a_breakpoint_advances(monkeypatch):
    _breakpoint_at_restart_pc(monkeypatch)
    link, cores, _ = pi2()
    cores[0].regs[15] = 0x8100
    conn = connect(link, "bcm2836")
    t = conn.target
    assert t.set_breakpoint(0x8100)
    t.step()
    assert t.read_core_register("pc") == 0x8104, "ステップしたのに PC が進まない（ブレークポイントに止まり直した）"
    conn.close()


@pytest.mark.xfail(strict=True, reason="BUG-ARMA-02: Cortex-A のブレークポイントの ARM / Thumb は番地でなく最後に止まったときの CPSR.T で決まる")
def test_v7_breakpoint_encoding_follows_the_address_not_the_mode_at_the_last_halt():
    link, cores, _ = pi2()
    cores[0].cpsr = 0x13 | 1 << 5                         # Thumb で止まっている
    cores[0].regs[15] = 0x8000
    conn = connect(link, "bcm2836")
    t = conn.target
    # 奇数の番地 = ARM の関数でなく Thumb の関数（pyOCD / ELF の慣習）。ARM の番地 0x9000 には ARM の BAS（0b1111）が要る
    assert t.set_breakpoint(0x9000)
    bcr = cores[0].bcr[0]
    assert (bcr >> 5) & 0xF == 0xF, f"ARM の命令なのに Thumb の BAS（0x{(bcr >> 5) & 0xF:X}）で置いた"
    conn.close()

