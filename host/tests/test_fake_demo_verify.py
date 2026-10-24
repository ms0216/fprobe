"""fake_demo の試す窓（故障注入・規則・フック）のデモ（fake_demo_verify.VerifyDemo）。"""

import pytest

import fake_demo
from fake_demo import COUNTER, DemoProbe


@pytest.fixture
def demo():
    probe = DemoProbe()
    fake_demo.start(probe)
    yield probe
    if probe._elf._recorder is not None:
        probe.recording_stop()


def _trial(**over):
    return {"id": "flip", "when": {"host": {"delay_s": 0}}, "write": {"address": "counter", "flip": 1},
            "timeout_s": 0.5, "observe_s": 0.01, **over}


def test_inject_writes_the_demo_ram_and_gives_a_time_inside_the_recording(demo):
    before = demo.counter
    out = demo.inject_run(script={"name": "demo", "requirements": {"R1": "x"},
                                  "trials": [_trial(requirement="R1", expect=[{"expression": "counter", "equals": before ^ 1}])]})
    trial = out["trials"][0]
    assert trial["verdict"] == "pass" and trial["injected"] and trial["address"] == COUNTER
    span = demo._elf._view.span()
    assert span[0] <= trial["t"] < span[1]
    assert demo.counter == before ^ 1


def test_the_real_script_checks_run_and_untriggered_and_bad_writes_are_shown(demo):
    with pytest.raises(ValueError, match="timeout_s"):
        demo.inject_run(script={"trials": [{k: v for k, v in _trial().items() if k != "timeout_s"}]})
    out = demo.inject_run(script={"trials": [
        _trial(id="other", when={"dap_match": {"address": 0x20000000}}, timeout_s=0.05),
        _trial(id="outside", write={"address": 0x10000000, "value": 1}),
    ]})
    assert [t["verdict"] for t in out["trials"]] == ["not_triggered", "inject_error"]


def test_rules_check_and_report_use_the_real_probe_on_the_demo_recording(demo, tmp_path):
    rules = {"name": "demo", "requirements": {"R1": "no lost events"},
             "rules": [{"id": "lost", "kind": "never", "requirement": "R1", "event": {"stream": "annotation", "kind": "events_lost"}}]}
    out = demo.rules_check(rules=rules, mark=False)
    assert out["rules"][0]["verdict"] == "fail" and out["rules"][0]["violations"][0]["t"] > 0
    demo.inject_run(script={"trials": [_trial()]})
    report = demo.rules_report(str(tmp_path / "r.json"), "json")
    assert report["requirements"]["R1"] == "fail" and (tmp_path / "r.json").is_file()
    states = demo.rules_states({"stream": "target", "kind": "state"}, "body.state")
    assert set(states["states"]) == {"RUNNING", "HALTED"}


def test_hooks_are_the_real_registry_the_daemon_runs(demo):
    hook = demo.hooks_add("halt", "after", [{"mark": "stopped at {where}"}], name="m")
    assert demo.hook_table is not None
    assert [h["id"] for h in demo.hooks_list()["hooks"]] == [hook["id"]]
    assert demo.hooks_remove(id=hook["id"]) == {"removed": 1}
