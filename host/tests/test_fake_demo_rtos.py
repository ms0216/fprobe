"""fake_demo_rtos.RtosDemo: RTOS（タスクの時間軸・CPU 負荷・オブジェクト・呼び出し履歴）・Code Profile・Interrupts・
Call Graph のデモの値（G05・G06）。値はデモの記録（tests/demo_recording.py）のタスク・割り込みとデモの ELF の関数に
そろい、形は本物の Probe と同じ（本物の解析の部品に偽のサンプル・区間を入れて作る）。"""

import tempfile
from pathlib import Path

import pytest

import fake_demo
from demo_recording import HZ, ISRS, SPAN, TASKS
from fake_demo import DemoProbe

HANDLES = dict(TASKS)


@pytest.fixture()
def demo():
    probe = DemoProbe()
    probe.open_demo_recording(Path(tempfile.mkdtemp(prefix="fprobe-demo-test-")) / "demo")
    now = [0.0]
    probe._rtos_clock = lambda: now[0]
    probe.now = now                       # 試験が時計を進める（実時間 1 秒 = 記録の 1 秒）
    return probe


def test_timeline_grows_while_running_and_stops_with_a_summary(demo):
    with pytest.raises(RuntimeError, match="rtos_timeline_start"):
        demo.rtos_timeline_get()
    started = demo.rtos_timeline_start(method="itm", exceptions=True)
    assert started["running"] and started["method"] == "itm" and started["timebase_hz"] == HZ
    demo.now[0] = 0.5
    half = demo.rtos_timeline_get(limit=5)
    assert half["running"] and half["t1"] <= SPAN // 4 + 1 and len(half["runs"]) == 5
    assert set(half["stats"]["tasks"]) == {"main", "sensor", "comm", "logger", "IDLE"}
    assert half["stats"]["tasks"]["sensor"]["handle"] == HANDLES["sensor"]
    demo.now[0] = 10.0
    stopped = demo.rtos_timeline_stop()
    assert not stopped["running"] and stopped["t1"] == SPAN and "runs" not in stopped
    assert stopped["exceptions"]["activations"] > 1000
    idle = stopped["stats"]["tasks"]["IDLE"]["cpu"]
    assert 0.5 < idle < 0.95 and stopped["stats"]["tasks"]["main"]["max_run_us"] > 100
    with pytest.raises(RuntimeError, match="取っていない"):
        demo.rtos_timeline_stop()
    assert not demo.rtos_timeline_get(limit=1)["running"]          # 止めた後も最後の結果


def test_cpu_load_windows_have_task_and_isr_shares(demo):
    demo.rtos_timeline_start(method="itm", exceptions=True)
    demo.now[0] = 3.0
    load = demo.rtos_timeline_load(window_us=100_000)
    assert load["windows_total"] == 20 and load["peak"]["load"] >= load["mean_load"] > 0.05
    w = load["windows"][3]
    assert set(w["tasks"]) >= {"main", "IDLE"} and w["isr"] > 0
    assert any(x["covered"] == 0 or x["covered"] < 100_000 * HZ // 1_000_000 for x in load["windows"])  # 止まった間


def test_sampled_timeline_is_a_lower_bound(demo):
    demo.rtos_timeline_start(method="sampled", rate_hz=5_000)
    demo.now[0] = 1.0
    got = demo.rtos_timeline_get(limit=3)
    assert got["method"] == "sampled" and got["lower_bound"] and got["resolution"] == HZ / 5_000
    assert "exceptions" not in got and all(r["samples"] >= 1 for r in got["runs"])
    demo.rtos_timeline_stop()
    with pytest.raises(ValueError, match="itm"):
        demo.rtos_timeline_start(method="sampled", exceptions=True)


def test_objects_queues_mutex_inversion_and_heap(demo):
    out = demo.rtos_objects()
    kinds = {o["kind"] for o in out["objects"]}
    assert {"queue", "mutex", "binary_semaphore", "counting_semaphore", "timer", "event_group",
            "stream_buffer"} <= kinds
    rx = next(o for o in out["objects"] if o["name"] == "rx_queue")
    assert rx["waiters"][0]["task"] == "comm" and rx["fields"]["length"] > rx["fields"]["messages"]
    timer = next(o for o in out["objects"] if o["kind"] == "timer")
    assert timer["fields"]["callback"] in demo._elf._symbols._functions or timer["fields"]["callback"]
    inv = out["contention"]["inversions"][0]
    assert inv["mutex"] == "log_mutex" and inv["holder"]["task"] == "logger" and inv["waiter"]["task"] == "sensor"
    assert out["heap"]["free"] < out["heap"]["total"] and out["heap"]["min_free"] <= out["heap"]["free"]
    assert out["halted"] and out["consistent"]
    sems = demo.rtos_objects(kinds=["semaphore"])["objects"]
    assert sems and all(o["kind"].endswith("semaphore") for o in sems)
    demo.state = "RUNNING"
    with pytest.raises(RuntimeError, match="live"):
        demo.rtos_objects()
    live = demo.rtos_objects(live=True)
    assert not live["consistent"] and live["notes"]


def test_task_backtrace_frames_are_in_the_elf(demo):
    bt = demo.rtos_backtrace("comm")
    assert bt["task"] == "comm" and bt["handle"] == HANDLES["comm"] and bt["state"] == "blocked"
    assert bt["frames"][-1]["function"] == "reset_handler" and all(f["file"] == "sram_demo.c" for f in bt["frames"])
    assert demo.rtos_backtrace(f"0x{HANDLES['IDLE']:08X}")["task"] == "IDLE"
    with pytest.raises(KeyError):
        demo.rtos_backtrace("nobody")


@pytest.mark.parametrize("method", ["pcsr", "itm"])
def test_profile_uses_the_real_profiler(demo, method):
    with pytest.raises(ValueError, match="core_clock_hz"):
        demo.profile_start(method="itm")
    started = demo.profile_start(method=method, rate_hz=2_000, core_clock_hz=84e6)
    assert started["running"] and started["plan"]["rate_hz"] > 0
    demo.now[0] = 1.0
    res = demo.profile_result()
    assert res["running"] and res["total"] > 500
    names = {r["function"] for r in res["rows"]}
    assert {"tick", "reset_handler"} <= names and all(0 <= r["ci"][0] <= r["fraction"] <= r["ci"][1] for r in res["rows"])
    lines = demo.profile_result(by="line", function="tick")["rows"]
    assert lines and all(r["function"] == "tick" and r["line"] for r in lines)
    cov = demo.profile_coverage()
    tick = next(f for f in cov["functions"] if f["function"] == "tick")
    assert cov["approximate"] and tick["lines_hit"] and ["sram_demo.c", 19] in tick["missed_lines"]
    demo.now[0] = 2.5
    stopped = demo.profile_stop()
    assert not stopped["running"] and stopped["rows"]
    assert not demo.profile_result()["running"]


def test_profile_window_and_exceptions(demo):
    demo.profile_start(method="pcsr", rate_hz=2_000)
    demo.now[0] = 2.0
    part = demo.profile_aggregate(t0=int(0.2 * HZ), t1=int(0.4 * HZ))
    assert part["window"]["time_unit"] == "timebase" and 300 < part["window"]["samples"] < 500
    with pytest.raises(RuntimeError, match="例外トレース"):
        demo.profile_exceptions()
    demo.profile_stop()
    demo.profile_start(method="itm", rate_hz=2_000, core_clock_hz=84e6)
    demo.now[0] = 4.0
    ex = demo.profile_exceptions()
    by = {e["name"]: e for e in ex["exceptions"]}
    assert set(by) == set(ISRS.values()) and by["SysTick"]["count"] > 1000 and ex["clock_hz"] == 84e6
    assert by["USART2"]["seconds"]["inclusive"]["max"] > by["SysTick"]["seconds"]["inclusive"]["mean"]


def test_recording_and_static_stack_go_to_the_real_probe(demo):
    found = demo.recording_find({"stream": "rtos", "kind": "run", "a": HANDLES["logger"]})
    assert found["count"] == 19
    q = demo.recording_query("SELECT COUNT(*) FROM events WHERE stream = 'rtos' AND kind = 'isr'")
    assert q["rows"][0][0] > 1000
    st = demo.stack_static()
    assert {f["function"] for f in st["functions"]} >= {"reset_handler", "tick"}
    assert next(f for f in st["functions"] if f["function"] == "tick")["calls"] == ["reached_milestone"]


def test_demo_starts_with_the_recording_open():
    probe = DemoProbe()
    fake_demo.start(probe)
    assert probe.recording_query("SELECT COUNT(*) FROM events")["rows"][0][0] > 0
