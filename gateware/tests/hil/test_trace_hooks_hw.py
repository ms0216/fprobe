"""トレースフックの拡張（U10）とターゲットが出す値・イベント（U16）の実機試験: Nucleo F401RE の SRAM で freertos_demo。

freertos_demo（host/firmware/freertos_demo）: fprobe_trace.h がタスクの切り替え（ITM ポート 31）とカーネルの出来事
（ポート 30・29: キュー・セマフォ・ミューテックス・ready・待ち・優先度・SysTick の出入り）を出す。fprobe_user.h で
fpu の計算の区間 "fpu_step"・値 fpu_value（ポート 1、f32）・"i:sensor_count"・印 "done" を出す。
sensor（2）は 5 ms ごとに cmd_queue へ送り、ctrl（3）が受けて 4 回に 1 回 done_sem を渡す。worker（1）は log_mutex を
約 6 ms 持ち、その間に ctrl が待つ（継承で worker が 3 に上がる）。idle_spins はアイドルのフックが回った数。
SWO のピン（PB3）は DBGMCU_CR.TRACE_IOEN で有効にする（STM32 固有）。
"""

import time
from pathlib import Path

import pytest

from fprobe.mcp_server import Probe
from tests.hil.program import SERIAL, program

pytestmark = pytest.mark.hil
FREERTOS_DEMO = Path(__file__).resolve().parents[3] / "host" / "firmware" / "freertos_demo" / "freertos_demo.elf"
CORE_HZ = 16e6
SWO_BAUD = 4_000_000                         # 16 MHz / 4（FPGA は 100 MHz / 25 でちょうど）。2 Mbaud ではカーネルの出来事が溢れた


@pytest.fixture(scope="module")
def probe():
    program("fprobe")
    p = Probe(SERIAL)
    p.target_connect("stm32f401xe", frequency_hz=4_000_000)
    yield p
    p.target_reset()
    p.close()


@pytest.fixture
def running(probe):
    probe.program_ram(str(FREERTOS_DEMO))
    probe.target_resume()
    time.sleep(0.5)
    probe.peripheral_write("DBG.DBGMCU_CR", fields={"TRACE_IOEN": 1})
    yield probe
    if probe.recording_status()["recording"]:                           # 途中で落ちた試験の記録を閉じる（次に響かない）
        probe.recording_stop()


def idle_rate(probe, seconds: float = 1.0) -> float:
    """アイドルのフックが 1 秒に回る数（走らせたまま読む）。"""
    address = probe._symbols.variable("idle_spins").address
    with probe._lock:
        a = probe.target.read_memory(address)
    time.sleep(seconds)
    with probe._lock:
        b = probe.target.read_memory(address)
    return ((b - a) & 0xFFFF_FFFF) / seconds


def test_kernel_events_flow_blocking_queues_and_exact_inversions(running):
    probe = running
    probe.recording_start(name="u10_kernel")
    started = probe.rtos_timeline_start(method="itm", swo_baud=SWO_BAUD, core_clock_hz=CORE_HZ)
    assert not [w for w in started["warnings"] if "オブジェクト" in w], started["warnings"]
    time.sleep(1.0)
    stopped = probe.rtos_timeline_stop()
    assert stopped["kernel"]["lost_records"] == 0 and stopped["kernel"]["gaps"] == 0, stopped["kernel"]

    flow = {(e["from"], e["to"], e["op"]): e for e in probe.rtos_timeline_flow()["edges"]}
    # 5 ms ごとより少ない（vTaskDelay は相対: worker が継承で優先度 3 の間 sensor が遅れ、20 ms に 3 回前後になる。実機で 143〜150）
    assert 120 <= flow[("sensor", "cmd_queue", "send")]["count"] <= 250, flow
    assert flow[("cmd_queue", "ctrl", "receive")]["count"] >= 120
    # done_sem は sensor の 4 回に 1 回（上と同じく 1 秒 50 回より少ない。実機で 37〜39）
    assert flow[("ctrl", "done_sem", "give")]["count"] >= 30 and flow[("done_sem", "worker", "take")]["count"] >= 30
    assert flow[("worker", "log_mutex", "give")]["count"] >= 30 and ("ctrl", "log_mutex", "wait") in flow

    blocking = probe.rtos_timeline_blocking()
    assert blocking["objects"]["cmd_queue"]["count"] >= 120                     # ctrl が待つ
    assert blocking["tasks"]["sensor"]["by_on"]["delay"]["count"] >= 120
    mutex_waits = [w for w in blocking["waits"] if w["on"] == "log_mutex"]
    assert mutex_waits and all(w["outcome"] == "ok" for w in mutex_waits if not w.get("truncated"))

    queues = probe.rtos_timeline_queues()["objects"]
    assert queues["cmd_queue"]["length"] == 4 and queues["cmd_queue"]["max"] <= 4
    assert "log_mutex" not in queues and queues["done_sem"]["max"] <= 1

    inv = probe.rtos_timeline_inversions()
    assert inv["source"] == "trace" and inv["count"] >= 30, inv["count"]          # done の回ごと（1 秒に 37 前後）
    assert inv["unbounded"] == 0, inv["max"]
    assert all(e["holder"] == "worker" and e["waiter"] == "ctrl" and e["inherited"] for e in inv["episodes"]
               if not e.get("truncated"))
    print("inversion latency of inheritance (ticks):", sorted(e["inherited"]["latency"] for e in inv["episodes"]
                                                             if e["inherited"])[-3:])

    jobs = probe.rtos_timeline_jobs()
    sensor = jobs["tasks"]["sensor"]
    # 応答（ready → 走り始め）: worker が継承で優先度 3 の間（log_mutex を約 6 ms と再帰）は sensor（2）が待たされる。
    # 実機で最大 9 ms（フックの負荷込み）。20 ms（ctrl の周期）を超えたら異常
    assert sensor["response"] is not None and sensor["response_us"]["max"] < 20_000, sensor
    assert any("ready" in n for n in jobs["notes"])

    profile = probe.rtos_timeline_profile()                                       # SysTick はフックの isr_enter / exit
    span_ms = (stopped["t1"] - stopped["t0"]) / stopped["timebase_hz"] * 1e3     # 窓は sleep より長い
    assert 0.9 * span_ms <= profile["interrupts"]["SysTick"]["count"] <= 1.1 * span_ms + 2, profile["interrupts"]

    cost = probe.rtos_timeline_overhead()
    print("trace overhead:", {k: cost[k] for k in ("words_per_s", "swo_share", "cpu_share", "by_kind")})
    assert cost["swo_share"] < 0.8 and cost["cpu_share"] < 0.1, cost
    probe.recording_stop()


def test_hook_overhead_measured_by_idle_spins(running):
    """フックの負荷の実測: トレースの無いときと有るときの、アイドルのフックの回る速さの差（= フックに使った CPU）。
    rtos.timeline.overhead の見積もり（cpu_share）と比べて記録する。"""
    probe = running
    without = idle_rate(probe)
    probe.rtos_timeline_start(method="itm", swo_baud=SWO_BAUD, core_clock_hz=CORE_HZ)
    with_trace = idle_rate(probe)
    probe.rtos_timeline_stop()
    estimate = probe.rtos_timeline_overhead()["cpu_share"]
    measured = 1 - with_trace / without
    print(f"idle spins/s: without {without:.0f}, with {with_trace:.0f}; measured overhead {measured:.4f}, "
          f"estimate {estimate:.4f}")
    assert without > 0 and 0 <= measured < 0.15, (without, with_trace)
    assert measured / 4 <= max(estimate, 1e-4) <= max(measured, 1e-3) * 4, (measured, estimate)   # 見積もりの桁が合う


def test_user_values_and_named_event_intervals(running):
    probe = running
    probe.recording_start(name="u16_user")
    started = probe.user_trace_start(channels=[{"name": "fpu_value", "source": "itm", "index": 1, "type": "f32"}],
                                     swo_baud=SWO_BAUD, core_clock_hz=CORE_HZ)
    assert started["itm_ports"] == [1, 24, 25]
    time.sleep(1.0)
    stats = probe.user_trace_stats()
    assert stats["errors"] == {}, stats["errors"]
    assert stats["values"]["fpu_value"]["count"] > 100
    assert 120 <= stats["values"]["sensor_count"]["count"] <= 250                # 5 ms ごとより少ない（vTaskDelay は相対: worker が継承で優先度 3 の間 sensor が遅れ、20 ms に 3 回前後になる。実機で 143〜150）
    assert 30 <= stats["marks"]["done"] <= 60                                    # sensor の 4 回に 1 回（上と同じく 1 秒 50 回より少ない）
    step = stats["intervals"]["fpu_step"]
    assert step["count"] > 100 and step["seconds"]["median"] < 0.002, step
    stopped = probe.user_trace_stop()
    assert stopped["recorded"] and not stopped["unmatched_stops"]
    step = probe.user_trace_stats()["intervals"]["fpu_step"]                       # 止めた時点まで（記録と同じ範囲）
    measured = probe.timing_measure({"stream": "user", "kind": "start", "body": {"name": "fpu_step"}},
                                    {"stream": "user", "kind": "stop", "body": {"name": "fpu_step"}})
    assert abs(measured["count"] - step["count"]) <= 2
    probe.recording_stop()
