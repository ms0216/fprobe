"""FPGA の観測の値（fpga_obs_read の items）で条件の成立を数える（fprobe.obs_condition）。
数える範囲は使う側が渡した値の並びそのもの（暗黙の窓・暗黙の件数を持たない）。"""

import pytest

from fprobe.correlate import ConditionError
from fprobe.obs_condition import count_condition


def v(t, fields, group=0):
    return {"kind": "value", "time_us": t, "group": group, "fields": fields}


def test_counts_where_the_terms_hold_against_the_previous_value_and_reports_the_span():
    items = [v(0, {"state": "IDLE", "restart": 0}), v(100, {"state": "BUSY", "restart": 0}),
             v(200, {"state": "BUSY", "restart": 1}), v(300, {"state": "DONE", "restart": 1}),
             v(400, {"state": "IDLE", "restart": 0}), {"kind": "gap", "time_us": 450, "group": 0, "lost": 3},
             v(500, {"state": "BUSY", "restart": 1}), v(1_000_000, {"state": "IDLE", "restart": 1})]
    out = count_condition(items, "restart changed and state=BUSY")
    # 200 と 500（450 の切れ目の後も直前の値と比べる。切れ目は数と失った変化の数で示す）
    assert out == {"condition": "restart changed and state=BUSY", "count": 2, "values": 7,
                   "t0_us": 0, "t1_us": 1_000_000, "span_s": 1.0, "gaps": 1, "hidden": 0, "lost": 3, "untimed": 0, "unnamed": 0}


def test_values_left_out_of_the_count_are_counted():
    # 時刻の決まらない値・信号の定義の無い値は数えない。外した数を返す（欠けを明示。?36）
    items = [v(0, {"flag": 0}), {"kind": "value", "time_us": None, "group": 0, "fields": {"flag": 1}},
             {"kind": "value", "time_us": 5, "group": 0, "fields": None}, v(10, {"flag": 1})]
    out = count_condition(items, "flag changed")
    assert out["count"] == 1 and out["values"] == 2 and out["untimed"] == 1 and out["unnamed"] == 1


def test_rose_and_fell_and_the_fpga_prefix():
    items = [v(0, {"restart": 0}), v(10, {"restart": 1}), v(20, {"restart": 0}), v(30, {"restart": 1}),
             v(40, {"restart": 1})]
    assert count_condition(items, "restart rose")["count"] == 2
    assert count_condition(items, "fpga.restart fell")["count"] == 1


def test_compares_with_the_previous_value_of_the_same_group():
    items = [v(0, {"flag": 0}), v(10, {"other": 5}, 1), v(20, {"flag": 0}), v(30, {"other": 6}, 1),
             {"kind": "gap", "time_us": 35, "group": 0}, v(40, {"flag": 1}), v(50, {"flag": 0})]
    assert count_condition(items, "flag changed")["count"] == 2


def test_no_values_is_not_zero_but_unknown_span():
    # 値が無いのを「変化なし」とみなさない: 数えた値の数 0・範囲 null をそのまま返す
    out = count_condition([], "state changed")
    assert out["count"] == 0 and out["values"] == 0 and out["t0_us"] is None and out["span_s"] is None


def test_a_condition_without_an_instant_is_refused():
    with pytest.raises(ConditionError):
        count_condition([v(0, {"state": "BUSY"})], "state=BUSY")


def test_the_probe_counts_over_the_last_items_the_caller_names(monkeypatch):
    from fprobe.mcp_server import Probe
    p = Probe("unused")
    seen = {}

    def read(last=2000, since=None):
        seen["last"] = last
        return {"items": [v(0, {"s": 0}), v(10, {"s": 1})]}

    monkeypatch.setattr(p, "fpga_obs_read", read)
    out = p.fpga_obs_count(conditions=["s rose", "s=1"], last=50)
    assert seen["last"] == 50
    assert out["last"] == 50 and out["conditions"][0]["count"] == 1
    assert out["conditions"][1]["condition"] == "s=1" and "error" in out["conditions"][1]   # 読めない条件は理由を添える
