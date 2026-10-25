"""不具合の再現試験（修正済み。台帳: docs/reports/bugs/correlate-recording.md）。
直ったら strict の xfail が通って失敗する = マークを外す合図。領域: correlate / ConditionWatcher / 条件の解釈。"""

import pytest

from fprobe.correlate import (ConditionError, ConditionWatcher, _Series, _Variables, _instants, _target, correlate,
                              durations, parse_condition)
from fprobe.recording import Recorder, Recording

ADDR = 0x20000000
SIGNALS = {"groups": {"0": [{"name": "flag", "lsb": 0, "width": 1}]}}


def _recording(tmp_path, rows, names=None, meta=None):
    rec = Recorder(tmp_path / "r", timebase_hz=1000.0,
                   meta={"fpga.signals": SIGNALS, "sample.names": names or {f"0x{ADDR:08X}": "cnt"}, **(meta or {})})
    rec.add_many(rows)
    rec.close()
    return Recording.open(tmp_path / "r")


def test_state_term_on_variable_without_values_counts_zero(tmp_path):
    rows = [("fpga", "value", 10, 0, 0), ("fpga", "value", 20, 0, 1), ("fpga", "value", 30, 0, 0)]
    rec = _recording(tmp_path, rows)                           # cnt は名前だけあって、読みが 1 件も無い
    out = correlate(rec, "flag changed and cnt=1", ["flag changed"], window_s=0.01)
    assert out["effect"]["count"] == 0


def test_watcher_equals_term_uses_value_carried_from_previous_range(tmp_path):
    rows = [("sample", "word", 11, ADDR, 0), ("sample", "word", 15, ADDR, 1),
            ("fpga", "value", 10, 0, 1), ("fpga", "value", 18, 0, 1), ("fpga", "value", 19, 0, 1)]
    rec = _recording(tmp_path, rows)
    text = "cnt changed and flag=1"
    whole = _instants(parse_condition(text), _Variables(rec, 0, 20))
    assert whole == [15]
    w = ConditionWatcher(text)
    assert w.scan(rec, 0, 12) + w.scan(rec, 12, 20) == whole    # 少しずつ調べても一度に調べたのと同じ時刻


def test_two_failed_reads_do_not_blank_out_everything_between(tmp_path):
    names = {f"0x{ADDR:08X}": "cnt", "0x20000004": "ev"}
    rows = []
    for t in range(0, 1000, 10):
        rows.append(("sample", "word", t, ADDR, t // 100))
        rows.append(("sample", "word", t, 0x20000004, (t + 50) // 100))
    rows += [("sample", "word", 5, ADDR, 0, {"ack": 2}), ("sample", "word", 995, ADDR, 0, {"ack": 2})]
    rec = _recording(tmp_path, rows, names)
    out = correlate(rec, "ev changed", ["cnt changed"], window_s=0.2)
    assert out["causes"][0]["judged"] == 8                      # 失敗は 2 回だけ。窓に失敗の欠けが掛かる効果 2 件（t=50, 150）だけが外れる
    assert durations(rec, "cnt")["excluded"] == 0


def test_and_inside_quoted_name_is_not_a_separator():
    c = parse_condition('"R and D" changed')
    assert [(t.name, t.op) for t in c.terms] == [("R and D", "changed")]


def test_equals_value_with_leading_zero_is_decimal():
    assert _target(_Series([0], [8]), "08", "cnt") == 8


def test_duplicate_mcu_name_is_ambiguous(tmp_path):
    names = {"0x20000000": "x", "0x20000004": "x"}
    rows = [("sample", "word", 1, 0x20000000, 1), ("sample", "word", 2, 0x20000000, 2),
            ("sample", "word", 1, 0x20000004, 7), ("sample", "word", 2, 0x20000004, 7)]
    rec = _recording(tmp_path, rows, names)
    with pytest.raises(ConditionError):
        correlate(rec, "x changed", ["x changed"], window_s=0.01)


def test_pin_name_conflict_between_pins_and_channel_names_is_ambiguous(tmp_path):
    rows = [("sample", "word", 1, ADDR, 0), ("sample", "word", 2, ADDR, 1)]
    rec = _recording(tmp_path, rows, meta={"pins": ["a", "b"], "channel_names": ["b", "a"]})
    with pytest.raises(ConditionError):
        _Variables(rec, 0, 3).resolve("a")
