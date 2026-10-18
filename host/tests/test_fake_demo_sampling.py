"""fake_demo_sampling.SamplingDemo: Data Sampling・Dashboard・Power のデモの値（watch.*・target_voltage）。

値の形は本物の Probe（fprobe.probe_watch・mcp_server.target_voltage）と同じ。走行中は時間とともに値が動く。
"""

import time

import pytest

from fake_demo import DemoProbe


@pytest.fixture
def demo():
    d = DemoProbe()
    yield d
    d.watch_stop()


def wait_for(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_watch_set_describes_like_the_real_probe(demo):
    r = demo.watch_set(["counter", "sensors[0].value", "active->value", "0x20000400:4"], rate_hz=1000)
    by = {w["name"]: w for w in r["watches"]}
    assert by["counter"] == {"name": "counter", "address": "0x200000D4", "size": 4, "type": "volatile uint32_t",
                             "method": "sampler"}
    assert by["sensors[0].value"]["type"] == "float" and by["sensors[0].value"]["method"] == "sampler"
    assert by["active->value"]["method"] == "host" and by["active->value"]["address"] is None   # ポインタをたどる
    assert by["0x20000400:4"]["type"] is None
    assert r["rate_hz"] == 1000 and r["slots"] and all(s["address"].startswith("0x") for s in r["slots"])


def test_bad_expressions_are_marked_and_the_rest_are_sampled_like_the_real_probe(demo):
    r = demo.watch_set(["counter", "no_such", "1 + 2"], rate_hz=100)
    assert [w["name"] for w in r["watches"]] == ["counter"]
    assert [x["expression"] for x in r["rejected"]] == ["no_such", "1 + 2"]
    status = demo.watch_status()
    assert [x["expression"] for x in status["rejected"]] == ["no_such", "1 + 2"]
    assert status["since_start_s"] >= 0 and "device" in status


def test_too_many_locations_are_refused_like_the_sampler(demo):
    with pytest.raises(ValueError):
        demo.watch_set([f"0x{0x20000000 + 0x100 * i:08X}:4" for i in range(9)])


def test_values_move_with_time_while_running(demo):
    demo.target_resume()
    demo.watch_set(["counter", "sensors[0].value", "sensors[1].value"], rate_hz=1000)
    assert wait_for(lambda: all(v["value"] is not None for v in demo.watch_values()["values"]))
    first = {v["name"]: v for v in demo.watch_values()["values"]}
    time.sleep(0.15)
    later = {v["name"]: v for v in demo.watch_values()["values"]}
    assert later["counter"]["value"] > first["counter"]["value"]                 # 4 / ms で増える
    assert later["counter"]["time_us"] > first["counter"]["time_us"]
    assert isinstance(later["sensors[0].value"]["value"], float)
    series = demo.watch_series("sensors[0].value", last=1000)
    assert series["name"] == "sensors[0].value" and len(series["samples"]) >= 5
    times = [s["time_us"] for s in series["samples"]]
    assert times == sorted(times)
    assert len({round(s["value"]) for s in series["samples"]}) > 1               # 波形（一定ではない）


def test_values_hold_while_halted(demo):
    demo.target_halt()
    demo.watch_set(["counter"])
    assert wait_for(lambda: len(demo.watch_series("counter")["samples"]) >= 3)
    assert len({s["value"] for s in demo.watch_series("counter")["samples"]}) == 1
    assert demo.watch_values()["values"][0]["value"] == demo.counter


def test_status_reports_rounds_and_overruns(demo):
    demo.watch_set(["counter"], rate_hz=1000)
    assert wait_for(lambda: demo.watch_status()["rounds"] > 0)
    s = demo.watch_status()
    assert s["watches"][0]["name"] == "counter" and s["rate_hz"] == 1000 and s["overruns"] == 0
    assert s["errors"] == 0 and s["capture"] is None and s["chase_errors"] == {}
    demo.watch_set(["counter"], rate_hz=10_000_000)                              # 速すぎる: 周期を飛ばす
    assert wait_for(lambda: demo.watch_status()["overruns"] > 0)
    stopped = demo.watch_stop()
    assert stopped["busy"] is False
    assert demo.watch_status()["watches"] == []


def test_trigger_captures_from_the_condition(demo):
    demo.target_resume()
    demo.watch_set(["counter", "sensors[1].value"], rate_hz=1000)
    armed = demo.watch_trigger(start={"variable": "counter", "op": ">", "value": 0}, post_s=0.1, pre_s=0.01)
    assert armed["state"] in ("armed", "capturing") and "samples" not in armed
    done = demo.watch_capture(wait_s=2.0)
    assert done["state"] == "done" and done["reason"] == "post"
    assert done["samples"]["counter"] and done["t_end_us"] > done["t_start_us"]
    assert demo.watch_status()["capture"] == {"state": "done", "reason": "post"}


def test_write_while_running_changes_the_value(demo):
    demo.target_resume()
    r = demo.watch_write("sensors[0].gain", 50)
    assert r == {"expression": "sensors[0].gain", "address": "0x20000402", "type": "uint16_t", "value": 50,
                 "atomic": True}
    demo.target_halt()
    assert demo.evaluate("sensors[0].gain")["value"] == 50                       # 止めて読んでも同じ RAM
    raw = demo.watch_write("0x20000460:4", 9600)
    assert raw["value"] == 9600 and raw["type"] is None
    assert demo.watch_write("config.verbose", False)["value"] is False


def test_target_voltage_moves_slowly_around_3v3(demo):
    readings = [demo.target_voltage() for _ in range(3)]
    for r in readings:
        assert set(r) == {"volts", "over_range", "live", "samples", "errors"}
        assert r["live"] is True and r["over_range"] is False
        assert 2.9 < r["volts"] < 3.35
    assert readings[-1]["samples"] >= readings[0]["samples"]


def test_recording_voltage_is_found(demo):
    import fake_demo
    fake_demo.start(demo)                                       # デモの記録（aux の電圧は 1.3 s の辺りで下がる）
    found = demo.recording_find({"stream": "aux", "kind": "mv"})
    assert found["count"] == 200 and min(e["b"] for e in found["events"]) < 3100
