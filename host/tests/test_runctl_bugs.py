"""実行制御（停止・再開・ステップ・ブレークポイント・ウォッチポイント・故障の解読）の不具合の再現（直した項目は普通の試験、未修正は xfail(strict)）。

偽の Cortex-M（cpu_fake.FakeCpu）の上で Debugger / Probe を動かし、状態モデルとの食い違いを xfail(strict) で記録する。
台帳: docs/reports/bugs/runctl.md。直ったら xfail が外れて失敗する = 印を外す合図。
"""

from types import SimpleNamespace

import pytest
from cpu_fake import BKPT, DEMCR, VC_HARDERR, FakeCpu
from probe_fakes import FakeDevice
from pyocd.core.target import Target
from test_stepping import PROGRAM

from fprobe import fault
from fprobe.debug import Debugger
from fprobe.mcp_server import Probe
from fprobe.stepping import dfsr_reason

# 0x800: op / 0x802: fault（HardFault。ベクタキャッチが有効なら VCATCH で止まる）/ 0x700: HardFault のハンドラ（spin）


def machine(pc=0x800, catch=True):
    cpu = FakeCpu(PROGRAM, pc, handlers={15: 0x500, 3: 0x700})
    if catch:
        cpu.write32(DEMCR, VC_HARDERR)
    return cpu


def probe_on(cpu) -> Probe:
    p = Probe("unused")
    p._session = SimpleNamespace(device=FakeDevice())
    p._debug = SimpleNamespace(target=cpu, probe=None, close=lambda: None)
    return p


# --- Debugger.step ---------------------------------------------------------------------------

def test_step_reports_the_real_halt_reason():
    cpu = machine(0x802)
    stop = Debugger(cpu).step()
    assert cpu.dfsr == 8                              # 事実: ベクタキャッチで止まった
    assert stop.reason == "vector_catch"


def test_step_from_a_breakpoint_advances():
    cpu = machine(0x800, catch=False)
    dbg = Debugger(cpu)
    dbg.breakpoint(0x800)
    dbg.step()
    assert cpu.pc == 0x802


def test_target_step_from_a_breakpoint_advances():
    cpu = machine(0x800, catch=False)
    p = probe_on(cpu)
    p.breakpoint_set("0x800")
    p.target_step()
    assert cpu.pc == 0x802


def test_target_step_stops_at_a_vector_catch():
    cpu = machine(0x800)
    p = probe_on(cpu)
    status = p.target_step(3)                         # 1 回目 op、2 回目 fault（VCATCH で停止）、3 回目はハンドラの中
    assert cpu.steps == 2
    assert status["halt_reason"] == "vector_catch"


# --- Debugger.resume -------------------------------------------------------------------------

def test_resume_keeps_a_vector_catch_hit_by_the_stepped_instruction():
    cpu = machine(0x802)
    dbg = Debugger(cpu)
    dbg.breakpoint(0x802)                             # 故障する命令にブレークポイント
    dbg.resume()
    assert cpu.state == Target.State.HALTED and cpu.dfsr == 8


class StepFails(FakeCpu):
    def step(self, *a, **k):
        raise RuntimeError("転送エラー")


def test_resume_restores_the_breakpoint_when_the_step_fails():
    cpu = StepFails(PROGRAM, 0x800, handlers={3: 0x700})
    dbg = Debugger(cpu)
    dbg.breakpoint(0x800)
    with pytest.raises(RuntimeError):
        dbg.resume()
    assert 0x800 in dbg.breakpoints
    assert 0x800 in cpu.breakpoints                   # 食い違い: 帳簿にはあるが機器には無い


# --- run_until_halt --------------------------------------------------------------------------

def test_run_until_halt_timeout_does_not_hide_a_real_halt():
    cpu = machine(0x800, catch=False)
    dbg = Debugger(cpu)
    dbg.breakpoint(0x802)
    stop = dbg.run_until_halt(timeout_s=0)            # 再開 → すぐ時間切れ。偽のコアは再開の中で 0x802 に着いて止まっている
    assert cpu.dfsr == BKPT
    assert stop.reason == "breakpoint"


# --- watchpoint ------------------------------------------------------------------------------

class DwtTarget(FakeCpu):
    """pyOCD と同じく、同じ (番地, 大きさ, 種類) は重ねず、違えば別の比較器を使うウォッチポイント。"""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.comparators: list[tuple] = []

    def set_watchpoint(self, address, size, kind):
        if (address, size, kind) not in self.comparators:
            if len(self.comparators) >= 4:
                return False
            self.comparators.append((address, size, kind))
        return True

    def remove_watchpoint(self, address, size, kind):
        if (address, size, kind) in self.comparators:
            self.comparators.remove((address, size, kind))


def test_watchpoint_clear_frees_every_comparator():
    cpu = DwtTarget(PROGRAM, 0x800)
    dbg = Debugger(cpu)
    dbg.watchpoint(0x20000000, 4, "write")
    dbg.watchpoint(0x20000000, 2, "read")
    dbg.clear()
    assert cpu.comparators == []


# --- Probe の帳簿 ----------------------------------------------------------------------------

def test_enable_failure_keeps_the_disabled_breakpoint():
    cpu = machine(0x800, catch=False)
    cpu.breakpoint_limit = 1
    p = probe_on(cpu)
    p.breakpoint_set("0x800", condition="1")
    p.breakpoint_enable("0x800", False)
    p.breakpoint_set("0x802")                         # 空いた比較器を別のブレークポイントが使う
    with pytest.raises(RuntimeError):
        p.breakpoint_enable("0x800", True)
    listed = {b["address"]: b["enabled"] for b in p.breakpoint_list()["breakpoints"]}
    assert listed.get(0x800) is False                 # 無効のまま残っている（有効に戻す再試行ができる）


def test_disconnect_closes_the_session_even_if_cleanup_fails():
    cpu = machine(0x800, catch=False)
    p = probe_on(cpu)
    closed = []
    p._debug.close = lambda: closed.append(True)

    def boom():
        raise RuntimeError("Flash の BKPT を戻せない")
    p._flash_bp_detach = boom
    with pytest.raises(RuntimeError):
        p.target_disconnect()
    assert closed and p._debug is None


# --- 故障の解読・停止理由の語 ----------------------------------------------------------------

class Core:
    def __init__(self, partno, memory=None, regs=None):
        self.memory = {fault.CPUID: 0x410F0000 | partno << 4, **(memory or {})}
        self.regs = {"xpsr": 0, "lr": 0, "msp": 0, "psp": 0, **(regs or {})}

    def read32(self, address):
        return self.memory.get(address, 0)

    def read_core_register(self, name):
        return self.regs[name]


@pytest.mark.parametrize("partno", [0xC20, 0xC21, 0xC60])
def test_analyze_knows_other_armv6m_parts(partno):
    r = fault.analyze(Core(partno))
    assert r["architecture"].startswith("ARMv6-M")


def test_cortex_m4_is_armv7m():
    # 0xC24 は Cortex-M4（pyocd core_ids。台帳の「SC000」は誤り）
    assert fault.analyze(Core(0xC24))["architecture"].startswith("ARMv7-M")


@pytest.mark.xfail(strict=True, reason="BUG-RUNCTL-11: 同じ停止（DFSR の BKPT+VCATCH）で、ステップ系の reason は vector_catch、target_status の halt_reason は breakpoint と食い違う")
def test_one_halt_gets_one_name_in_every_path():
    dfsr = 0x02 | 0x08
    assert dfsr_reason(dfsr, stepping=False) == fault.halt_reason(dfsr, instruction=0x4770)


def test_write0_near_the_end_of_memory():
    from test_semihosting import BASE, CODE, Console, FakeTarget

    from fprobe.semihosting import Op, SemihostingHost
    target, console = FakeTarget(), Console()
    end = BASE + len(target.mem)
    target.poke(end - 6, b"hello\0")                  # 終端は領域の中。ただし 64 バイト先は領域の外
    target.regs.update(r0=Op.SYS_WRITE0, r1=end - 6, pc=CODE)
    result = SemihostingHost(console=console).handle(target)
    assert result.handled and console.text() == b"hello"


@pytest.mark.xfail(strict=True, reason="BUG-RUNCTL-12: MMARVALID と BFARVALID が両方立つと fault_address は BFAR だけで MMFAR の番地が落ちる")
def test_both_fault_addresses_are_reported():
    core = Core(0x270, {fault.CFSR: (1 << 7) | (1 << 15) | 2 | (1 << 9), fault.MMFAR: 0x1000, fault.BFAR: 0x2000})
    r = fault.analyze(core)
    addresses = r["fault_address"]
    flat = str(addresses)
    assert "4096" in flat and "8192" in flat           # 0x1000 と 0x2000 の両方が出ている
