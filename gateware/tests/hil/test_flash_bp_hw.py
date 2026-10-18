"""Flash 上の無制限ブレークポイント（U28。fprobe.flash_bp・fprobe.probe_flash_bp）の実機試験: Nucleo F401RE。

Nucleo の Flash を全部書き換えることはしない。使うのは最後のセクタ（0x08060000、128 KiB）だけで、試験の前に中身を
読んで保存し、終わったら書き戻す（test_flash_hw.py と同じ）。そこに小さな試験用のループ（下の LOOP）を書き、PC をそこへ
向けて走らせる（割り込みは既存のファームの処理に入って戻ってくる）。比較器（FPB、F401 は命令用 6 個）は、最後のセクタの
実行されない番地に置いたブレークポイントで使い切り、ループの中のブレークポイントを Flash の BKPT にする。

確かめること:
- 比較器が尽きたら Flash に置ける。書くのは再開の直前の 1 回（同じセクタは 1 回の消去）。
- ブレークポイントの上からの再開で Flash を書き換えない（PC 相対の読み・BL は模擬、ほかは RAM で 1 命令）。プログラムの
  結果（レジスタの値）が、ブレークポイントの無いときと同じ。
- 外したものは休眠し、dormant_limit 回当たると戻す。読み出しは元の命令に見える。
- 切断で戻る。異常終了（戻さずに切断）で残ったものは、次の接続で記録から戻る。
"""

import atexit
import time

import pytest

from fprobe.flash_bp import BKPT_CODE, FlashBreakpoints
from fprobe.mcp_server import Probe
from tests.hil.program import SERIAL, program

pytestmark = pytest.mark.hil

LAST, LAST_SIZE = 0x08060000, 0x20000          # 最後のセクタ（試験で書いてよい唯一の場所）
IDLE = LAST + 0x200                             # 実行されない番地（比較器を使い切るためのブレークポイント）
COMPARATORS = 6


def _h(*halfwords: int) -> bytes:
    return b"".join(h.to_bytes(2, "little") for h in halfwords)


# LAST+0  movs r0,#0 / +2 adds r0,#1 / +4 ldr r1,[pc,#12]（→ +20）/ +6 bl +16 / +10 adds r0,#1 / +12 b +2 / +14 nop /
# +16 adds r2,#1 / +18 bx lr / +20 .word 0x12345678
LOOP = _h(0x2000, 0x3001, 0x4903, 0xF000, 0xF803, 0x3001, 0xE7F9, 0xBF00, 0x3201, 0x4770) + (0x12345678).to_bytes(4, "little")
STOPS = [LAST + 2, LAST + 4, LAST + 6, LAST + 16, LAST + 18]     # adds / ldr（模擬）/ bl（模擬）/ adds / bx lr（RAM）


@pytest.fixture(scope="module")
def probe():
    program("fprobe")
    p = Probe(SERIAL)
    p.target_connect("stm32f401xe", frequency_hz=4_000_000)
    yield p
    p.target_reset()
    p.close()


@pytest.fixture
def loop(probe, tmp_path):
    """最後のセクタを保存し、試験のループを書いて PC をその先頭に向ける。後で比較器・Flash の BKPT を外し、セクタを戻す。"""
    saved = tmp_path / "last_sector.bin"
    probe.flash_read(str(saved), address=LAST, length=LAST_SIZE)
    code = tmp_path / "loop.bin"
    code.write_bytes(LOOP)
    probe.flash(str(code), address=LAST, erase="sector", reset=False)
    probe.target_halt()
    probe.write_register("pc", LAST)
    probe.write_register("r2", 0)
    probe.write_register("xpsr", 1 << 24)
    yield
    probe.target_halt()
    probe.breakpoint_clear()
    probe.flash_breakpoints_restore()
    probe.flash(str(saved), address=LAST, reset=False)
    assert probe.flash_verify(str(saved), address=LAST)["match"]


def _fill_comparators(probe) -> None:
    for i in range(COMPARATORS):
        probe.breakpoint_set(hex(IDLE + 2 * i))


def _stats(probe) -> dict:
    """Flash のブレークポイントの統計（接続の間、試験をまたいで足されていく: 試験では差を見る）。"""
    return dict(probe.flash_breakpoints().get("stats") or {})


def _raw(probe, address: int, length: int = 2) -> bytes:
    """Flash の実際の中身（BKPT を元の命令に見せない読み）。"""
    with probe._lock:
        return bytes(probe.target.target.read_memory_block8(address, length))


def test_breakpoints_beyond_the_comparators_are_written_once_and_resumed_without_rewriting(probe, loop):
    assert isinstance(probe.target, FlashBreakpoints)
    _fill_comparators(probe)
    for address in STOPS:
        probe.breakpoint_set(hex(address))
    before = _stats(probe)
    kinds = {b["address"]: b.get("kind") for b in probe.breakpoint_list()["breakpoints"]}
    assert all(kinds[a] == "flash" for a in STOPS), kinds
    started = time.monotonic()
    seen = [probe.run_until_halt(timeout_s=5)["pc"]]
    first = time.monotonic() - started
    for _ in range(3 * len(STOPS) - 1):
        stop = probe.run_until_halt(timeout_s=5)
        assert stop["reason"] == "breakpoint", stop
        seen.append(stop["pc"])
    assert seen == STOPS * 3, [hex(a) for a in seen]
    regs = probe.read_registers(["r0", "r1", "r2"])
    assert regs["r1"] == 0x12345678 and regs["r2"] == 3, regs           # 模擬した ldr・bl、RAM で実行した adds
    status = probe.flash_breakpoints()
    stats = {k: v - before.get(k, 0) for k, v in status["stats"].items()}
    assert stats["commits"] == 1 and stats["sector_writes"] == 1 and stats["rewrites"] == 0, stats
    assert stats["emulated"] >= 4 and stats["displaced"] >= 6, stats
    assert _raw(probe, LAST + 2) == BKPT_CODE
    assert bytes.fromhex(probe.read_memory(LAST, len(LOOP))) == LOOP           # 読み出しは元の命令
    print(f"\n最初の再開（Flash に 5 個の BKPT を 1 回で書く）まで {first:.2f} s、{stats}")


def test_removed_breakpoints_are_passed_then_restored(probe, loop):
    _fill_comparators(probe)
    for address in STOPS:
        probe.breakpoint_set(hex(address))
    probe.run_until_halt(timeout_s=5)
    probe.breakpoint_clear(hex(LAST + 16))                                     # 休眠: まだ書き戻さない
    assert probe.flash_breakpoints()["dormant"] == [LAST + 16]
    stops = [probe.run_until_halt(timeout_s=5) for _ in range(4 * 3)]
    seen = [s["pc"] for s in stops]
    # 戻した後も、そこに PC を置いたまま再開すると CPU が古い BKPT を実行し続けた（Flash の読みの行の保持。flash.py の
    # _fetch_elsewhere で直した）
    assert LAST + 16 not in seen, " ".join(f"{s['pc'] - LAST}:{s['reason']}" for s in stops)
    status = probe.flash_breakpoints()
    assert status["dormant"] == [] and status["stats"]["dormant_hits"] >= 2, status["stats"]   # 3 回目で戻した
    assert _raw(probe, LAST + 16) == LOOP[16:18]


def test_step_on_a_flash_breakpoint_does_not_rewrite(probe, loop):
    _fill_comparators(probe)
    probe.breakpoint_set(hex(LAST + 4))
    probe.run_until_halt(timeout_s=5)
    commits = _stats(probe)["commits"]
    status = probe.target_step()
    assert status["pc"] == LAST + 6
    assert probe.read_registers(["r1"])["r1"] == 0x12345678
    assert _stats(probe)["commits"] == commits


def test_disconnect_restores_and_a_crash_is_recovered_on_the_next_connect(probe, loop):
    _fill_comparators(probe)
    probe.breakpoint_set(hex(LAST + 2))
    probe.run_until_halt(timeout_s=5)
    assert _raw(probe, LAST + 2) == BKPT_CODE
    probe.target_disconnect()                                                   # 切断で戻す
    probe.target_connect("stm32f401xe", frequency_hz=4_000_000)
    assert _raw(probe, LAST + 2) == LOOP[2:4]
    probe.target_halt()
    probe.write_register("pc", LAST)
    _fill_comparators(probe)
    probe.breakpoint_set(hex(LAST + 2))
    probe.run_until_halt(timeout_s=5)
    fbp = probe._flash_bp                                                        # 異常終了: 戻さずに切断する
    atexit.unregister(fbp._exit_hook)
    probe._flash_bp = None
    probe._dbg = None
    probe.target_disconnect()
    out = probe.target_connect("stm32f401xe", frequency_hz=4_000_000)
    assert out.get("flash_breakpoints_recovered") == {"restored": [LAST + 2], "skipped": []}, out
    assert _raw(probe, LAST + 2) == LOOP[2:4]
