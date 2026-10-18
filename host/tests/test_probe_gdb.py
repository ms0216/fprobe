"""Probe の GDB サーバの操作（fprobe.probe_gdb）: レジスタ（d は s の対）・タスクの文脈・共有のブレークポイント・
停止の判定・セミホスティングの出力と終了・Flash。偽の Cortex-M（gdb_fakes）と、偽の FreeRTOS（rtos_kernel）。"""

import pytest
from gdb_fakes import FLASH, FakeCore, make_probe
from pyocd.core.target import Target
from rtos_kernel import FreeRTOSBuilder, shared

from fprobe import probe_gdb
from fprobe.gdb.description import Region


@pytest.fixture
def core():
    return FakeCore()


@pytest.fixture
def probe(core):
    return make_probe(core)


def test_attach_tells_the_registers_and_the_memory_regions(probe, core):
    core.state = Target.State.RUNNING
    info = probe.gdb_attach()
    assert core.state == Target.State.HALTED                     # all-stop の GDB は止まった状態から
    assert "basepri" in info["registers"] and "s31" in info["registers"]
    assert info["regions"] == [Region("flash", FLASH[0], FLASH[1], FLASH[2]), Region("ram", 0x2000_0000, 0x1_8000)]


def test_attach_connects_when_not_connected(monkeypatch, probe, core):
    debug = probe._debug
    probe._debug = None
    calls = []

    def connect(target, frequency_hz, connect_mode):
        calls.append((target, frequency_hz, connect_mode))
        probe._debug = debug

    monkeypatch.setattr(probe, "target_connect", connect)
    probe.gdb_attach("stm32f401xe", 1_000_000)
    assert calls == [("stm32f401xe", 1_000_000, "halt")]


def test_registers_read_and_write_d_as_pairs_of_s(probe, core):
    core.registers.update(s2=0x1111_1111, s3=0x2222_2222, r0=5)
    assert probe.gdb_read_registers(["r0", "d1", "pc"]) == [5, 0x2222_2222_1111_1111, 0x0800_0100]
    probe.gdb_write_registers({"d2": 0xAAAA_AAAA_BBBB_BBBB, "r1": 7})
    assert core.registers["s4"] == 0xBBBB_BBBB and core.registers["s5"] == 0xAAAA_AAAA and core.registers["r1"] == 7


def test_registers_the_core_lacks_read_as_none():
    probe = make_probe(FakeCore(fpu=False))
    assert probe.gdb_read_registers(["r0", "d0"]) == [0, None]


def test_memory(probe):
    probe.gdb_write_memory(0x2000_0000, b"\x01\x02\x03")
    assert probe.gdb_read_memory(0x2000_0000, 4) == b"\x01\x02\x03\x00"


# --- ブレークポイント ---------------------------------------------------------------------------

def test_gdb_breakpoints_are_shared_with_the_api_and_owned(probe, core):
    probe.gdb_breakpoint("gdb1", True, "break", 0x0800_0200, 2)
    probe.gdb_breakpoint("gdb2", True, "break", 0x0800_0200, 2)
    assert core.breakpoints[0x0800_0200] == Target.BreakpointType.HW
    assert probe.breakpoint_list()["breakpoints"][0]["location"] == "gdb:0x08000200"
    probe.gdb_breakpoint("gdb1", False, "break", 0x0800_0200, 2)
    assert 0x0800_0200 in core.breakpoints                       # gdb2 がまだ置いている
    probe.gdb_breakpoint("gdb2", False, "break", 0x0800_0200, 2)
    assert core.breakpoints == {} and probe.breakpoint_list()["breakpoints"] == []


def test_gdb_does_not_remove_what_the_ai_placed(probe, core):
    probe.breakpoint_set("0x08000300")
    probe.gdb_breakpoint("gdb1", True, "break", 0x0800_0300, 2)
    probe.gdb_breakpoint("gdb1", False, "break", 0x0800_0300, 2)
    assert 0x0800_0300 in core.breakpoints
    # GDB が置いた後に AI が同じ番地に置き直したら、GDB が外しても残す
    probe.gdb_breakpoint("gdb1", True, "break", 0x0800_0400, 2)
    probe.breakpoint_set("0x08000400")
    probe.gdb_breakpoint("gdb1", False, "break", 0x0800_0400, 2)
    assert 0x0800_0400 in core.breakpoints


def test_hardware_breakpoint_in_ram_and_release(probe, core):
    probe.gdb_breakpoint("gdb1", True, "hbreak", 0x2000_0100, 2)
    probe.gdb_breakpoint("gdb1", True, "break", 0x2000_0200, 2)
    probe.gdb_breakpoint("gdb1", True, "write", 0x2000_0400, 4)
    assert core.breakpoints == {0x2000_0100: Target.BreakpointType.HW, 0x2000_0200: Target.BreakpointType.SW}
    assert core.watchpoints[0x2000_0400][1] == Target.WatchpointType.WRITE
    probe.gdb_release("gdb1")
    assert core.breakpoints == {} and core.watchpoints == {}


def test_a_watchpoint_size_the_dwt_cannot_take_is_an_error(probe):
    with pytest.raises(RuntimeError):
        probe.gdb_breakpoint("gdb1", True, "read", 0x2000_0400, 3)


# --- 実行と停止 -----------------------------------------------------------------------------------

def test_resume_steps_over_a_breakpoint_at_the_pc_and_poll_reports_the_stop(probe, core):
    probe.gdb_breakpoint("gdb1", True, "break", 0x0800_0100, 2)
    probe.gdb_resume()
    assert core.steps == 1 and core.state == Target.State.RUNNING
    assert probe.gdb_poll()["stop"] is None
    core.hit(0x0800_0100)
    stop = probe.gdb_poll()["stop"]
    assert stop["reason"] == "breakpoint" and stop["pc"] == 0x0800_0100 and stop["breakpoint"] == "hw"
    assert stop["thread"] is None


def test_step_over_a_breakpoint_puts_it_back(probe, core):
    probe.gdb_breakpoint("gdb1", True, "break", 0x2000_0000, 2)
    core.registers["pc"] = 0x2000_0000
    stop = probe.gdb_step()
    assert stop["reason"] == "step" and stop["pc"] == 0x2000_0002
    assert core.breakpoints[0x2000_0000] == Target.BreakpointType.SW                 # 置き直した


def test_watchpoint_stop_names_the_matched_comparator(probe, core):
    probe.gdb_breakpoint("gdb1", True, "write", 0x2000_0400, 4)
    probe.gdb_breakpoint("gdb1", True, "access", 0x2000_0500, 4)
    core.write32(probe_gdb.DWT_CTRL, 4 << 28)
    core.write32(probe_gdb.DWT_COMP0 + 16, 0x2000_0500)
    core.write32(probe_gdb.DWT_FUNCTION0 + 16, probe_gdb.DWT_MATCHED | 7)
    probe.gdb_resume()
    core.hit(0x0800_0120, Target.HaltReason.WATCHPOINT)
    assert probe.gdb_poll()["stop"]["watch"] == {"address": 0x2000_0500, "kind": "access"}


def test_halt_by_another_user_is_a_stop(probe, core):
    probe.gdb_resume()
    probe.target_halt()                                          # AI・画面が止めた
    stop = probe.gdb_poll()["stop"]
    assert stop["reason"] == "user" and "fault" not in stop


def test_poll_continues_a_log_breakpoint_and_forwards_semihosting_output(probe, core):
    probe.breakpoint_set("0x08000500", ignore_count=1)            # 1 回目は止まらない
    probe.gdb_resume()
    core.hit(0x0800_0500)
    assert probe.gdb_poll()["stop"] is None and core.state == Target.State.RUNNING
    probe._received("semihosting", b"hello\n", 0.0)
    out = probe.gdb_poll(0)
    assert out["console"] == b"hello\n" and out["console_next"] == 6
    assert probe.gdb_poll(out["console_next"])["console"] == b""


def test_semihosting_exit_is_reported_once(probe, core):
    probe.gdb_resume()
    core.hit(0x0800_0600)
    probe._sh_exit = {"code": 3, "reason": "ApplicationExit"}
    probe._sh_watch = False
    assert probe.gdb_poll()["stop"]["exit"] == 3
    assert "exit" not in probe.gdb_poll()["stop"]


# --- RTOS のタスク -----------------------------------------------------------------------------------

@pytest.fixture
def rtos():
    k = FreeRTOSBuilder()
    h = {name: k.task(name, prio) for name, prio in [("ctrl", 3), ("sensor", 2), ("IDLE", 0)]}
    k.put_state(h["ctrl"], k.var("xSuspendedTaskList"))
    k.put_state(h["sensor"], k.ready(2))
    k.put_state(h["IDLE"], k.ready(0))
    k.set_current(h["sensor"])
    k.initial_context(h["ctrl"], "ctrl_task", 0x1234)
    k.handles = h
    core = FakeCore(memory=k.memory)
    core.registers.update(pc=0x0800_0111, r0=99)
    return k, core, make_probe(core, shared()[1])


def test_threads_are_the_tasks_and_their_saved_registers(rtos):
    k, core, probe = rtos
    listing = probe.gdb_threads()
    threads = {t["name"]: t for t in listing["threads"]}
    assert set(threads) == {"ctrl", "sensor", "IDLE"} and threads["sensor"]["current"]
    assert threads["ctrl"]["id"] == k.handles["ctrl"] and threads["ctrl"]["state"] == "suspended"
    ctrl = k.handles["ctrl"]
    r0, pc, msp = probe.gdb_read_registers(["r0", "pc", "msp"], ctrl)
    assert r0 == 0x1234 and pc == k.symbols.symbol("ctrl_task").address and msp is None
    assert probe.gdb_read_registers(["r0"], k.handles["sensor"]) == [99]       # 実行中のタスクは実レジスタ
    with pytest.raises(RuntimeError):
        probe.gdb_write_registers({"r0": 1}, ctrl)
    assert probe.gdb_stop()["thread"] == k.handles["sensor"]


def test_no_threads_without_an_elf(probe):
    listing = probe.gdb_threads()
    assert listing["threads"] == [] and "ELF" in listing["note"]


# --- Flash ----------------------------------------------------------------------------------------

def test_flash_writes_the_blocks_with_a_memory_loader(monkeypatch, probe):
    seen = {}

    class Loader:
        def __init__(self, session, keep_unwritten=True):
            seen["session"], seen["keep"], seen["data"] = session, keep_unwritten, []

        def add_data(self, address, data):
            seen["data"].append((address, bytes(data)))

        def commit(self):
            seen["committed"] = True

    monkeypatch.setattr(probe_gdb, "MemoryLoader", Loader)
    out = probe.gdb_flash([(0x0800_0000, b"\x00\x10\x00\x20"), (0x0800_4000, b"\xAA")])
    assert out["bytes"] == 5 and seen["committed"] and seen["keep"] is False
    assert seen["session"] is probe._debug and seen["data"][1] == (0x0800_4000, b"\xAA")


def test_flash_blocksize_comes_from_the_pyocd_region():
    """pyOCD の FlashRegion の attributes は既定値を関数で持つ（blocksize = lambda r: r.sector_size）。属性で引く
    （実機で memory-map が「unsupported format string passed to function」になった。2026-10-18）。"""
    from types import SimpleNamespace
    from pyocd.core.memory_map import FlashRegion, RamRegion
    from fprobe.mcp_server import Probe
    p = Probe("unused")
    p._debug = SimpleNamespace(target=SimpleNamespace(memory_map=[
        FlashRegion(start=0x08000000, length=0x80000, sector_size=0x4000, is_boot_memory=True),
        RamRegion(start=0x20000000, length=0x18000)]), close=lambda: None)
    regions = p._gdb_regions()
    assert regions[0].kind == "flash" and regions[0].blocksize == 0x4000 and regions[1].kind == "ram"
