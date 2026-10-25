"""規則の検査・故障注入の台本（U25）とデバッグの出来事のフック（U44）の実機試験: Primer 20K（fprobe）+ Nucleo F401RE に
RAM で走らせた sram_demo（counter が増え続け、256 回ごとに reached_milestone）。

- 故障注入: ホストの発火で、走っているターゲットの変数をハードウェアの割り込み書き込みで壊し、走り続けること・値が
  変わったことを判定する。注入の時刻（ハードウェアの時刻）が記録の stream "inject" に入る。最後に既定（停止）に戻る。
- 規則: DAP サンプラの読み（stream "sample"）に件数・値の規則を当て、状態遷移図と証跡を作る。
- フック: デーモン経由で、止まりの後に式を評価して印を残す（ブレークポイントで止まっても）、required の before で
  電圧を確かめてからリセットする。
"""

import json
import threading
import time
from pathlib import Path
from tests.hil.values import num

import pytest
import uvicorn

from fprobe.api.client import DaemonClient
from fprobe.api.daemon import create_app
from fprobe.mcp_server import Probe
from fprobe.xtrig import DHCSR, HALT
from fprobe import generated as g
from tests.hil.program import SERIAL, program

pytestmark = pytest.mark.hil
ELF = Path(__file__).resolve().parents[3] / "host" / "firmware" / "sram_demo" / "sram_demo.elf"


@pytest.fixture(scope="module")
def probe(tmp_path_factory):
    program("fprobe")
    import os
    os.environ["FPROBE_HOME"] = str(tmp_path_factory.mktemp("home"))
    p = Probe(SERIAL)
    p.target_connect("stm32f401xe", frequency_hz=4_000_000)
    p.program_ram(str(ELF))
    yield p
    if p._recorder is not None:
        p.recording_stop()
    p.target_reset()
    p.close()


def test_fault_injection_corrupts_a_running_variable_at_a_hardware_time(probe, tmp_path):
    probe.target_resume()
    probe.recording_start(name="inject")
    time.sleep(0.3)                                                    # 時計の対応付けの点を取る
    script = {"name": "hil", "requirements": {"REQ-F1": "変数が壊れても走り続ける"}, "trials": [
        {"id": "counter-jump", "requirement": "REQ-F1", "when": {"host": {"delay_s": 0.05}},
         "write": {"address": "counter", "value": 0x40000000}, "timeout_s": 2.0, "observe_s": 0.1,
         "expect": [{"state": "running"}, {"memory": {"address": "counter"}, "min": 0x40000000}]},
        {"id": "table-flip", "when": {"host": {}}, "write": {"address": "table", "flip": 1}, "timeout_s": 2.0, "observe_s": 0,
         "expect": [{"memory": {"address": "table"}, "equals": 0x11111110}],
         "restore": {"write": [{"address": "table", "value": 0x11111111}]}},
    ]}
    result = probe.inject_run(script=script)
    assert result["verdict"] == "pass", result
    first = result["trials"][0]
    assert first["injected"] and first["t"] > 0
    marks = probe.recording_find({"stream": "inject", "kind": "trial"})["events"]
    assert [m["t"] for m in marks] == [t["t"] for t in result["trials"]], marks
    with probe._lock:                                                  # 割り込み書き込みは既定（停止）へ戻った
        dev = probe.session.device
        assert dev.read(g.XTRIG_INJECT_ADDR, 2) == [DHCSR, HALT]
        assert dev.read32(g.XTRIG_ROUTE) >> g.XTRIG_ROUTE_TO_INJECT[0] & 0xF == 0
    report = probe.rules_report(path=str(tmp_path / "inject.md"))
    assert report["requirements"] == {"REQ-F1": "pass"}, report
    probe.recording_stop()


def test_rules_on_sampled_values_states_and_evidence(probe, tmp_path):
    probe.target_resume()
    probe.recording_start(name="rules")
    counter = probe._symbols.variable("counter").address
    probe.watch_set(["counter"], rate_hz=1000)
    time.sleep(1.0)
    probe.watch_stop()
    sample = {"stream": "sample", "a": counter}
    rules = {"name": "hil-rules", "requirements": {"REQ-R1": "counter は止まらず読める"}, "rules": [
        {"id": "sampled", "kind": "count", "event": sample, "min": 100, "requirement": "REQ-R1"},
        {"id": "period", "kind": "period", "event": sample, "min_us": 100, "max_us": 50_000},
        {"id": "small", "kind": "value", "event": sample, "field": "b", "max": 1, "severity": "warning"},
    ]}
    result = probe.rules_check(rules=rules)
    by = {r["id"]: r for r in result["rules"]}
    assert by["sampled"]["verdict"] == "pass" and by["period"]["verdict"] == "pass", result
    assert by["small"]["verdict"] == "warn" and result["marked"] == by["small"]["violations_total"]
    assert probe.recording_find({"stream": "rule"})["count"] == result["marked"]
    states = probe.rules_states(event=sample, field="b", dot_path=str(tmp_path / "counter.dot"))
    assert states["changes"] > 10 and (tmp_path / "counter.dot").read_text(encoding="utf-8").startswith("digraph")
    out = probe.rules_report(path=str(tmp_path / "rules.json"), format="json")
    assert json.loads((tmp_path / "rules.json").read_text(encoding="utf-8"))["requirements"]["REQ-R1"]["verdict"] == \
        "pass", out
    probe.recording_stop()


@pytest.fixture
def daemon_url(probe, tmp_path):
    app = create_app(probe, token=None, activity_dir=tmp_path / "activity", watch_s=0.1)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.01)
    yield f"ws://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}/api"
    server.should_exit = True
    thread.join(10)
    probe.hooks_remove(all=True)


def test_hooks_run_on_breakpoint_stops_and_guard_a_reset(daemon_url):
    ai = DaemonClient(daemon_url, "claude", "ai")
    ai.call("hooks.add", event="halt", phase="after", actions=[{"evaluate": "counter"}, {"mark": "stop at {where}"}])
    ai.call("hooks.add", event="reset", phase="before", actions=[{"call": "probe.voltage"}], required=True,
            name="電圧の確認")
    ai.call("debug.breakpoint.set", location="reached_milestone")
    stop = ai.call("debug.run_until_halt", timeout_s=3)
    assert stop["reason"] == "breakpoint", stop
    log = ai.call("hooks.log")["entries"]
    halt = [e for e in log if e["event"] == "halt"]
    assert len(halt) == 1 and halt[0]["results"][0]["ok"], log
    assert num(halt[0]["results"][0]["value"]) % 256 == 0                  # reached_milestone の時の counter
    assert "reached_milestone" in halt[0]["results"][1]["value"]
    ai.call("debug.breakpoint.clear", location="reached_milestone")
    ai.call("target.reset", halt=True)
    guard = [e for e in ai.call("hooks.log")["entries"] if e["event"] == "reset"]
    assert guard and guard[-1]["results"][0]["ok"], guard
    ai.close()
