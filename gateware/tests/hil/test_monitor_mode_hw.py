"""モニタ方式（U37、止めずにデバッグする。J-Link の Monitor Mode Debugging の相当）の実機試験: Nucleo F401RE に
RAM で走らせた試験用ファーム（host/firmware/monitor_demo）。

試験用ファームの優先度: SysTick 0x00（1 kHz、fast_ticks）> DebugMon 0x80（fprobe のモニタ）> PendSV 0xF0（slow_ticks。
SysTick が 8 回ごとに保留にする）> main（loop_count。work → filter を回る）。

確かめること:
- モニタ方式でブレークポイントに止まっている間も、DebugMon より優先度の高い SysTick は数え続け（fast_ticks が増える）、
  低い PendSV と main は止まる（slow_ticks・loop_count が増えない）。モニタの待ちの輪は回る（poll_count が増える）。
- 停止方式（halt）では SysTick も止まる（対照）。
- 止まっている間に、同じ道具でステップ・行のステップ・レジスタ・呼び出し履歴・引数・FP のレジスタ・関数の呼び出しが
  使え、halt / resume もできる。停止方式へ同じ PC で戻れる。
- DebugMon の優先度を SysTick と同じ（0x00）にすると、SysTick も止まる（優先度の決まり）。

走らせるのは本流の担当（機器に触る）: pytest gateware/tests/hil/test_monitor_mode_hw.py --hil
"""

import time
from pathlib import Path

import pytest

from fprobe.mcp_server import Probe
from tests.hil.program import SERIAL, program

pytestmark = pytest.mark.hil
DEMO = Path(__file__).resolve().parents[3] / "host" / "firmware" / "monitor_demo" / "monitor_demo.elf"
WAIT_S = 0.2                                     # SysTick 1 kHz: 0.2 秒で 200 回ほど


@pytest.fixture(scope="module")
def probe():
    program("fprobe")
    p = Probe(SERIAL)
    p.target_connect("stm32f401xe", frequency_hz=4_000_000)
    yield p
    p.debug_mode_set("halt")
    p.target_reset()
    p.close()


@pytest.fixture
def fresh(probe):
    """停止方式で試験用ファームを RAM に読み込み、少し走らせてから止める（init が済み、FP の文脈もできている）。"""
    probe.debug_mode_set("halt")
    probe.breakpoint_clear()
    probe.program_ram(str(DEMO))
    probe.target_resume()
    time.sleep(0.05)
    probe.target_halt()
    return probe


def counters(probe) -> dict:
    return {name: probe.variable_read(name)["value"]
            for name in ("fast_ticks", "slow_ticks", "loop_count", "poll_count", "enter_count")}


def test_the_monitor_is_found_and_tells_what_keeps_running(fresh):
    out = fresh.debug_mode_get()
    assert out["available"] and out["mode"] == "halt", out
    assert out["priority"] == 0x80 and out["prio_bits"] == 0xF0 and out["fpu"], out
    names = [e["name"] for e in out["keeps_running"]]
    assert "SysTick" in names and "PendSV" not in names, out


def test_a_breakpoint_stops_only_what_is_below_the_monitor(fresh):
    fresh.debug_mode_set("monitor")
    fresh.breakpoint_set("work")
    stop = fresh.run_until_halt(timeout_s=2)
    assert stop["reason"] == "breakpoint" and stop["function"] == "work", stop
    status = fresh.target_status()
    assert status["state"] == "HALTED" and status["debug_mode"] == "monitor", status
    before = counters(fresh)
    time.sleep(WAIT_S)
    after = counters(fresh)
    assert after["fast_ticks"] - before["fast_ticks"] > 100, (before, after)     # SysTick は動き続ける
    assert after["slow_ticks"] == before["slow_ticks"], (before, after)          # PendSV は止まる
    assert after["loop_count"] == before["loop_count"], (before, after)          # main は止まる
    assert after["poll_count"] > before["poll_count"], (before, after)           # モニタの待ちの輪
    stop = fresh.run_until_halt(timeout_s=2)                                     # 踏み越えて一周
    assert stop["function"] == "work" and counters(fresh)["loop_count"] == after["loop_count"] + 1, stop


def test_halting_mode_stops_everything_for_contrast(fresh):
    before = counters(fresh)
    time.sleep(WAIT_S)
    assert counters(fresh)["fast_ticks"] == before["fast_ticks"]                 # 停止方式では SysTick も止まる


def test_stepping_registers_backtrace_and_arguments(fresh):
    fresh.debug_mode_set("monitor")
    fresh.breakpoint_set("filter")
    stop = fresh.run_until_halt(timeout_s=2)
    assert stop["function"] == "filter", stop
    names = [f["function"] for f in fresh.backtrace()]
    assert names[:3] == ["filter", "work", "reset_handler"], names
    n = fresh.evaluate("n")["value"]
    assert n == fresh.variable_read("loop_count")["value"] + 1, n               # 引数（モニタの箱のレジスタから）
    regs = fresh.read_registers(["pc", "sp", "lr", "xpsr"])
    assert regs["pc"] == stop["pc"] and regs["xpsr"] & 0x1FF == 0, regs           # スレッド（IPSR = 0）
    assert fresh.gdb_read_registers(["s0"])[0] is not None                        # FP 付きの枠（filter は浮動小数）
    pc = regs["pc"]
    stepped = fresh.target_step()
    assert stepped["pc"] != pc and stepped["debug_mode"] == "monitor", stepped
    line = fresh.step_line("over")
    assert line["reason"] == "step", line
    fresh.breakpoint_clear("filter")


def test_calling_a_function_while_stopped(fresh):
    fresh.debug_mode_set("monitor")
    fresh.breakpoint_set("work")
    fresh.run_until_halt(timeout_s=2)
    level = fresh.evaluate("level")["value"]
    out = fresh.call_function("filter", [3])
    assert out["returned"] and out["restored"], out
    assert abs(out["return"]["value"] - (level * 0.5 + 1.5 * 3)) < 1e-4, out
    assert fresh.target_status()["function"] == "work"                          # 元の場所に戻っている


def test_halt_resume_and_back_to_halting_mode_at_the_same_pc(fresh):
    fresh.debug_mode_set("monitor")
    assert fresh.target_resume()["state"] == "RUNNING"
    loops = counters(fresh)["loop_count"]
    time.sleep(0.05)
    assert counters(fresh)["loop_count"] > loops                                 # 走っている
    halted = fresh.target_halt()
    assert halted["state"] == "HALTED" and halted["debug_mode"] == "monitor", halted
    out = fresh.debug_mode_set("halt")
    assert out["mode"] == "halt", out
    status = fresh.target_status()
    assert status["state"] == "HALTED" and status["pc"] == halted["pc"] and "debug_mode" not in status, (halted, status)


def test_a_monitor_as_high_as_systick_stops_systick_too(fresh):
    fresh.debug_mode_set("monitor", priority=0x00)
    fresh.breakpoint_set("work")
    fresh.run_until_halt(timeout_s=2)
    before = counters(fresh)
    time.sleep(WAIT_S)
    assert counters(fresh)["fast_ticks"] == before["fast_ticks"]                 # 同じ優先度は割り込めない
    fresh.debug_mode_set("monitor", priority=0x80)
    time.sleep(WAIT_S)
    assert counters(fresh)["fast_ticks"] > before["fast_ticks"] + 100
