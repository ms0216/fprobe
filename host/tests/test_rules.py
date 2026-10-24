"""fprobe.rules: 宣言的な規則を記録に当てる（違反・期待値と実測・派生イベント・状態遷移図・証跡）。"""

import json

import pytest

from fprobe.recording import Recorder, Recording
from fprobe.rules import RuleError, RuleSet, check, render_report, state_machine

HZ = 1_000_000                        # 1 タイムベース = 1 µs（数を読みやすく）


def _record(tmp_path, rows, name="r"):
    rec = Recorder(tmp_path / name, timebase_hz=HZ, meta={"name": name, "board": "fake", "design": "fprobe"})
    rec.add_many(rows)
    rec.close()
    return Recording.open(tmp_path / name)


def tp(function, t, kind="enter"):
    return ("testpoint", kind, t, 0, 0, {"function": function})


ISR = {"stream": "testpoint", "kind": "enter", "body": {"function": "isr"}}
ISR_END = {"stream": "testpoint", "kind": "exit", "body": {"function": "isr"}}


def test_a_rule_set_is_validated_with_clear_errors():
    with pytest.raises(RuleError, match="id"):
        RuleSet.parse({"rules": [{"kind": "never", "event": ISR}]})
    with pytest.raises(RuleError, match="kind"):
        RuleSet.parse({"rules": [{"id": "x", "kind": "sometimes", "event": ISR}]})
    with pytest.raises(RuleError, match="重複"):
        RuleSet.parse({"rules": [{"id": "x", "kind": "never", "event": ISR}, {"id": "x", "kind": "never", "event": ISR}]})
    with pytest.raises(RuleError, match="max_us"):
        RuleSet.parse({"rules": [{"id": "x", "kind": "interval", "start": ISR, "end": ISR_END}]})
    with pytest.raises(RuleError, match="未知"):
        RuleSet.parse({"rules": [{"id": "x", "kind": "never", "event": ISR, "evnt": 1}]})


def test_rule_sets_load_from_yaml_and_json_text():
    text = "name: t\nrules:\n  - id: f\n    kind: never\n    event: {stream: swo, kind: exception}\n"
    assert RuleSet.parse_text(text).rules[0].id == "f"
    assert RuleSet.parse_text(json.dumps({"rules": [{"id": "g", "kind": "never", "event": ISR}]})).rules[0].id == "g"


def test_interval_limits_find_late_responses_and_missing_ends(tmp_path):
    r = _record(tmp_path, [tp("isr", 100), tp("isr", 110, "exit"),          # 10 µs
                           tp("isr", 200), tp("isr", 260, "exit"),          # 60 µs: 遅い
                           tp("isr", 300), tp("isr", 400),                  # 終わりが来ないまま次の開始
                           tp("isr", 405, "exit")])
    rules = RuleSet.parse({"name": "lat", "rules": [{"id": "lat", "kind": "interval", "start": ISR, "end": ISR_END,
                                                     "max_us": 50, "requirement": "REQ-1"}]})
    result = check(r, rules)
    rule = result["rules"][0]
    assert rule["verdict"] == "fail" and result["verdict"] == "fail"
    assert rule["measured"]["count"] == 3 and rule["measured"]["max_us"] == 60 and rule["measured"]["unmatched"] == 1
    assert [(v["t"], v["kind"]) for v in rule["violations"]] == [(200, "too_long"), (300, "no_end")]
    assert rule["violations"][0]["t_end"] == 260 and rule["expected"] == {"max_us": 50}


def test_violations_are_cut_with_the_total_and_the_limit_used(tmp_path):
    r = _record(tmp_path, [tp("isr", t) for t in (0, 10, 20, 30)])
    rules = RuleSet.parse({"rules": [{"id": "n", "kind": "never", "event": ISR}]})
    rule = check(r, rules, limit=2)["rules"][0]
    assert len(rule["violations"]) == 2 and rule["violations_total"] == 4 and rule["violations_limit"] == 2


def test_period_with_tolerance(tmp_path):
    r = _record(tmp_path, [tp("isr", t) for t in (0, 1000, 2000, 3100, 4100)])
    rules = RuleSet.parse({"rules": [{"id": "p", "kind": "period", "event": ISR, "period_us": 1000,
                                      "tolerance_us": 50}]})
    rule = check(r, rules)["rules"][0]
    assert rule["measured"]["count"] == 4 and rule["measured"]["jitter_us"] == 100
    assert [(v["t"], v["value"]) for v in rule["violations"]] == [(2000, 1100)]


def test_sequence_steps_in_order_within_time_and_forbidden_events(tmp_path):
    req = {"stream": "uart", "kind": "line", "body": {"text": "req"}}
    ack = {"stream": "uart", "kind": "line", "body": {"text": "ack"}}
    err = {"stream": "uart", "kind": "line", "body": {"text": "err"}}

    def line(text, t):
        return ("uart", "line", t, 0, 0, {"text": text})
    r = _record(tmp_path, [line("req", 0), line("ack", 10),                 # 良い
                           line("req", 100), line("ack", 300),              # 遅すぎる
                           line("req", 400), line("err", 405), line("ack", 410)])   # 禁止が挟まる
    rules = RuleSet.parse({"rules": [{"id": "s", "kind": "sequence", "trigger": req, "steps": [ack],
                                      "within_us": 100, "forbid": [err]}]})
    rule = check(r, rules)["rules"][0]
    assert rule["measured"] == {"triggers": 3, "completed": 1, "failed": 2}
    assert [(v["t"], v["kind"]) for v in rule["violations"]] == [(100, "missing_step"), (400, "forbidden")]


def test_count_never_value_and_states(tmp_path):
    rows = [("swo", "exception", 50, 3, 0, {"name": "HardFault"})]
    rows += [("sample", "word", t, 0x20000000, v) for t, v in ((0, 0), (10, 1), (20, 2), (30, 0), (40, 2))]
    r = _record(tmp_path, rows)
    mode = {"stream": "sample", "a": 0x20000000}
    rules = RuleSet.parse({"rules": [
        {"id": "c", "kind": "count", "event": mode, "min": 1, "max": 3},
        {"id": "n", "kind": "never", "event": {"stream": "swo", "kind": "exception"}},
        {"id": "v", "kind": "value", "event": mode, "field": "b", "max": 1, "severity": "warning"},
        {"id": "st", "kind": "states", "event": mode, "field": "b", "allowed": [[0, 1], [1, 2], [2, 0]],
         "labels": {"0": "idle", "1": "run", "2": "stop"}},
    ]})
    result = check(r, rules)
    by = {x["id"]: x for x in result["rules"]}
    assert by["c"]["verdict"] == "fail" and by["c"]["measured"]["count"] == 5
    assert by["n"]["violations"][0]["t"] == 50
    assert by["v"]["verdict"] == "warn" and [v["value"] for v in by["v"]["violations"]] == [2, 2]
    assert [(v["t"], v["value"]) for v in by["st"]["violations"]] == [(40, "idle -> stop")]
    assert result["counts"] == {"pass": 0, "fail": 3, "warn": 1, "no_data": 0}


def test_no_data_is_not_a_pass_unless_allowed(tmp_path):
    r = _record(tmp_path, [tp("other", 0)])
    rules = RuleSet.parse({"rules": [{"id": "p", "kind": "period", "event": ISR, "min_us": 1, "max_us": 2},
                                     {"id": "q", "kind": "period", "event": ISR, "min_us": 1, "max_us": 2,
                                      "allow_empty": True},
                                     {"id": "n", "kind": "never", "event": ISR}]})
    by = {x["id"]: x["verdict"] for x in check(r, rules)["rules"]}
    assert by == {"p": "no_data", "q": "pass", "n": "pass"}


def test_violations_are_marked_as_derived_events_and_replaced_on_recheck(tmp_path):
    r = _record(tmp_path, [tp("isr", 0), tp("isr", 100, "exit")])
    rules = RuleSet.parse({"name": "set1", "rules": [{"id": "lat", "kind": "interval", "start": ISR,
                                                      "end": ISR_END, "max_us": 50}]})
    check(r, rules, mark=True)
    check(r, rules, mark=True)
    marks = r.events(streams=["rule"])
    assert len(marks) == 1 and marks[0].kind == "violation" and marks[0].t == 0 and marks[0].t_end == 100
    assert marks[0].body["rule"] == "lat" and marks[0].body["ruleset"] == "set1"
    fixed = RuleSet.parse({"name": "set1", "rules": [{"id": "lat", "kind": "interval", "start": ISR,
                                                      "end": ISR_END, "max_us": 500}]})
    check(r, fixed, mark=True)
    assert r.events(streams=["rule"]) == []                                 # 直したら印も消える


def test_a_baseline_recording_is_compared_rule_by_rule(tmp_path):
    now = _record(tmp_path, [tp("isr", 0), tp("isr", 30, "exit")], "now")
    base = _record(tmp_path, [tp("isr", 0), tp("isr", 10, "exit")], "base")
    rules = RuleSet.parse({"rules": [{"id": "lat", "kind": "interval", "start": ISR, "end": ISR_END, "max_us": 50}]})
    rule = check(now, rules, baseline=base)["rules"][0]
    assert rule["baseline"]["measured"]["max_us"] == 10 and rule["baseline"]["delta"]["max_us"] == 20


def test_time_window_limits_the_check(tmp_path):
    r = _record(tmp_path, [tp("isr", 0), tp("isr", 100, "exit"), tp("isr", 1000), tp("isr", 1001, "exit")])
    rules = RuleSet.parse({"rules": [{"id": "lat", "kind": "interval", "start": ISR, "end": ISR_END, "max_us": 50}]})
    assert check(r, rules, t0=500)["rules"][0]["verdict"] == "pass"


def test_state_machine_counts_dwell_and_writes_dot(tmp_path):
    rows = [("sample", "word", t, 0x20000000, v) for t, v in ((0, 0), (10, 1), (40, 0), (50, 1), (60, 0))]
    r = _record(tmp_path, rows)
    sm = state_machine(r, {"stream": "sample", "a": 0x20000000}, "b", labels={"0": "idle", "1": "run"})
    assert sm["states"]["run"]["entries"] == 2 and sm["states"]["run"]["dwell_us"]["max"] == 30
    assert sm["states"]["idle"]["dwell_us"]["total"] == 20                    # 最後の idle は終わりが分からない
    assert {(x["from"], x["to"]): x["count"] for x in sm["transitions"]} == {("idle", "run"): 2, ("run", "idle"): 2}
    assert sm["dot"].startswith("digraph") and '"idle" -> "run"' in sm["dot"]


def test_state_field_from_body_path(tmp_path):
    rows = [("rtt", "line", 0, 0, 0, {"m": {"s": "A"}}), ("rtt", "line", 5, 0, 0, {"m": {"s": "B"}})]
    sm = state_machine(_record(tmp_path, rows), {"stream": "rtt"}, "body.m.s")
    assert [(x["from"], x["to"]) for x in sm["transitions"]] == [("A", "B")]


def test_the_evidence_report_maps_requirements_to_verdicts(tmp_path):
    r = _record(tmp_path, [tp("isr", 0), tp("isr", 100, "exit")])
    rules = RuleSet.parse({"name": "set1", "requirements": {"REQ-1": "応答は 50 µs 以内"},
                           "rules": [{"id": "lat", "kind": "interval", "start": ISR, "end": ISR_END, "max_us": 50,
                                      "requirement": "REQ-1"}]})
    result = check(r, rules)
    md = render_report("markdown", rules=result, recording=r)
    assert "REQ-1" in md and "応答は 50 µs 以内" in md and "fail" in md and "lat" in md
    data = json.loads(render_report("json", rules=result, recording=r))
    assert data["requirements"]["REQ-1"]["verdict"] == "fail" and data["recording"]["name"] == "r"
