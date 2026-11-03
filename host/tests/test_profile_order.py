"""プロファイルの行の並び（order）。並べ替えは API が持つ（画面は返った順のまま出す）。"""
import pytest

from fprobe.profile.aggregate import order_rows


ROWS = [{"line": 30, "samples": 9}, {"line": None, "samples": 5}, {"line": 10, "samples": 4}, {"line": 30, "samples": 2}]


def test_samples_keeps_the_order_the_top_rows_were_chosen_in():
    assert order_rows(ROWS, "samples", "line") == ROWS


def test_line_puts_the_chosen_rows_in_line_order_keeping_samples_order_for_the_same_line():
    assert [(r["line"], r["samples"]) for r in order_rows(ROWS, "line", "line")] == [(None, 5), (10, 4), (30, 9), (30, 2)]


def test_line_order_is_only_for_tables_with_lines_and_unknown_orders_are_refused():
    with pytest.raises(ValueError, match="by = line / instruction"):
        order_rows(ROWS, "line", "function")
    with pytest.raises(ValueError, match="order は"):
        order_rows(ROWS, "address", "line")
