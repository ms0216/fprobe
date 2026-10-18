"""Probe のデバッグの方式（fprobe.probe_monitor）: 方式の切り替え、モニタ方式で既存の道具（止める・再開・ステップ・
ブレークポイント・レジスタ・呼び出し履歴・GDB）が同じ形で使えること、止まっている間も優先度の高い割り込みが動くこと、
停止方式だけの操作の誤り、クロストリガの停止の書き込み、切断。偽のコア（monitor_fake）。"""

from types import SimpleNamespace

import pytest
from monitor_fake import FAST, LOOP, LOOPS, SLOW, WORK, MonitorChip

from fprobe import generated as g
from fprobe.mcp_server import Probe
from fprobe.monitor import DEMCR, MonitorError
from fprobe.probe_monitor import MONITOR_HALT
from fprobe.xtrig import DHCSR, HALT
from probe_fakes import FakeDevice


@pytest.fixture
def device():
    dev = FakeDevice()
    dev.write(g.XTRIG_INJECT_AP, [0, DHCSR, HALT])            # 割り込み書き込みの既定（リセット値）
    return dev


def connect(device, chip: MonitorChip) -> Probe:
    probe = Probe("unused")
    probe._session = SimpleNamespace(device=device)
    probe._debug = SimpleNamespace(target=chip, close=lambda: None)
    return probe


@pytest.fixture
def chip():
    return MonitorChip()


@pytest.fixture
def probe(device, chip):
    p = connect(device, chip)
    yield p
    p._stop_observing()


def spin(probe, n: int = 40) -> None:
    """時間を進める（偽のコアは機器の読みごとに 1 歩進む。止まっている間の状態は箱の写しで読むので、メモリを読む）。"""
    for _ in range(n):
        probe.read_memory(FAST, 4)


def test_mode_get_in_halt_mode_tells_whether_the_monitor_is_available(probe):
    out = probe.debug_mode_get()
    assert out["mode"] == "halt" and out["available"] and out["priority"] == 0x80 and out["prio_bits"] == 0xF0
    assert [e["name"] for e in out["keeps_running"]] == ["SysTick", "IRQ3"]
    assert "state" not in out


def test_without_the_monitor_get_says_why_and_set_refuses(device):
    probe = connect(device, MonitorChip(installed=False))
    out = probe.debug_mode_get()
    assert out == {"mode": "halt", "available": False, "reason": out["reason"]} and "組み込む" in out["reason"]
    with pytest.raises(MonitorError, match="組み込む"):
        probe.debug_mode_set("monitor")
    assert probe.debug_mode_get()["mode"] == "halt"


def test_switching_to_monitor_keeps_the_stop(probe, chip):
    out = probe.debug_mode_set("monitor", priority=0x90)
    assert out["mode"] == "monitor" and out["state"] == "stopped" and out["priority"] == 0x90
    status = probe.target_status()
    assert status == {"state": "HALTED", "pc": LOOP, "where": status["where"], "debug_mode": "monitor"}
    assert not chip.debugen


def test_breakpoint_run_and_the_higher_priority_interrupt_keeps_running(probe, chip):
    probe.debug_mode_set("monitor")
    probe.breakpoint_set(hex(WORK))
    probe.target_resume()
    stop = probe.run_until_halt(timeout_s=2)
    assert stop["reason"] == "breakpoint" and stop["pc"] == WORK
    fast, slow, loops = chip.var(FAST), chip.var(SLOW), chip.var(LOOPS)
    spin(probe)
    assert chip.var(FAST) > fast and (chip.var(SLOW), chip.var(LOOPS)) == (slow, loops)
    stop = probe.run_until_halt(timeout_s=2)                  # ブレークポイントの上から: 踏み越えて一周して戻る
    assert stop["reason"] == "breakpoint" and stop["pc"] == WORK and chip.var(LOOPS) == loops + 1


def test_halt_resume_step_and_registers(probe, chip):
    probe.debug_mode_set("monitor")
    assert probe.target_resume()["state"] == "RUNNING"
    spin(probe, 5)
    halted = probe.target_halt()
    assert halted["state"] == "HALTED" and halted["debug_mode"] == "monitor"
    pc = halted["pc"]
    for _ in range(2):
        pc = LOOP if pc >= LOOP + 14 else pc + 2              # 偽のコアの輪
    stepped = probe.target_step(count=2)
    assert stepped["pc"] == pc and stepped["debug_mode"] == "monitor"
    regs = probe.read_registers(["r0", "r4", "sp", "pc"])
    assert regs["r0"] == 0x1000 and regs["r4"] == 0x1004 and regs["pc"] == stepped["pc"]
    probe.write_register("r4", 0xBEEF)
    assert probe.read_registers(["r4"]) == {"r4": 0xBEEF}
    probe.target_resume()
    assert chip.regs["r4"] == 0xBEEF


def test_backtrace_and_watchpoint(probe, chip):
    probe.debug_mode_set("monitor")
    frames = probe.backtrace()
    assert frames[0]["pc"] == LOOP and frames[1]["pc"] == chip.regs["lr"] & ~1
    probe.watchpoint_set(hex(LOOPS), "write", 4)
    stop = probe.run_until_halt(timeout_s=2)
    assert stop["reason"] == "watchpoint"


def test_gdb_sees_the_same_stops_and_registers(probe):
    probe.debug_mode_set("monitor")
    r0, pc, s0 = probe.gdb_read_registers(["r0", "pc", "s0"])
    assert (r0, pc, s0) == (0x1000, LOOP, None)              # FP の文脈が無い: GDB には読めないと返す
    stop = probe.gdb_step()
    assert stop["reason"] == "step" and stop["pc"] == LOOP + 2
    probe.gdb_resume()
    probe.gdb_halt()
    assert probe.gdb_stop()["reason"] in ("user", "step")


def test_halting_only_operations_are_refused_in_monitor_mode(probe):
    probe.debug_mode_set("monitor")
    with pytest.raises(MonitorError, match="ベクタキャッチ"):
        probe.vector_catch(["hard_fault"])
    with pytest.raises(MonitorError, match="止めてのリセット"):
        probe.target_reset(halt=True)
    with pytest.raises(MonitorError, match="Flash の消去"):
        probe.flash_erase(mode="chip")


def test_switching_back_to_halt_keeps_the_stop_and_the_breakpoints(probe, chip):
    probe.debug_mode_set("monitor")
    probe.breakpoint_set(hex(WORK))
    probe.target_step()
    out = probe.debug_mode_set("halt")
    assert out["mode"] == "halt" and out["available"]
    assert chip.debugen and chip.halted and chip.regs["pc"] == LOOP + 2
    assert [b["address"] for b in probe.breakpoint_list()["breakpoints"]] == [WORK]
    assert "debug_mode" not in probe.target_status()
    stop = probe.run_until_halt(timeout_s=2)                  # 停止方式でも同じブレークポイントで止まる
    assert stop["reason"] == "breakpoint" and stop["pc"] == WORK


def test_cross_trigger_halt_follows_the_mode(probe, device):
    probe.debug_mode_set("monitor")
    assert device.read(g.XTRIG_INJECT_ADDR, 2) == [DEMCR, MONITOR_HALT]
    assert probe._halt_inject() == (DEMCR, MONITOR_HALT)
    probe.debug_mode_set("halt")
    assert device.read(g.XTRIG_INJECT_ADDR, 2) == [DHCSR, HALT]


def test_a_custom_cross_trigger_write_is_left_alone(probe, device):
    probe.xtrig_set_inject(0x2000_0000, 1)
    probe.debug_mode_set("monitor")
    assert device.read(g.XTRIG_INJECT_ADDR, 2) == [0x2000_0000, 1]


def test_disconnect_while_stopped_releases_the_target(probe, chip):
    probe.debug_mode_set("monitor")
    probe.target_disconnect()
    assert not chip.in_monitor and probe._monitor is None


def test_mode_tools_need_a_connected_target(device):
    from fprobe.api import NotConnected
    probe = Probe("unused")
    probe._session = SimpleNamespace(device=device)
    with pytest.raises(NotConnected):
        probe.debug_mode_get()
    with pytest.raises(NotConnected):
        probe.debug_mode_set("monitor")
