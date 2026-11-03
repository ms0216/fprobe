"""perf_budget.within: 速さの約束の試験の測り方（上限を超えた回だけ測り直し、最良で比べる）。"""

import pytest
from perf_budget import within


def fake_clock(monkeypatch, durations):
    """run を呼ぶたびに durations の秒だけ進む時計。"""
    import perf_budget
    now = [0.0]
    steps = iter(durations)
    monkeypatch.setitem(perf_budget.CLOCKS, "cpu", lambda: now[0])

    def run():
        now[0] += next(steps)
        return now[0]
    return run


def test_a_run_within_the_budget_is_measured_once(monkeypatch):
    run = fake_clock(monkeypatch, [0.5, 9.0])
    seconds, _ = within(1.0, run)
    assert seconds == 0.5


def test_slow_runs_are_measured_again_and_the_best_is_compared(monkeypatch):
    run = fake_clock(monkeypatch, [3.0, 0.9, 5.0])
    seconds, value = within(1.0, run)
    assert seconds == pytest.approx(0.9) and value == pytest.approx(3.9)


def test_every_run_over_the_budget_fails_with_all_the_times(monkeypatch):
    run = fake_clock(monkeypatch, [3.0, 2.0, 4.0])
    with pytest.raises(AssertionError, match="3.000, 2.000, 4.000"):
        within(1.0, run)


def test_setup_is_not_measured_and_feeds_the_run(monkeypatch):
    import perf_budget
    now = [0.0]
    monkeypatch.setitem(perf_budget.CLOCKS, "cpu", lambda: now[0])

    def setup():
        now[0] += 100.0                                  # 準備にかかった時間は数えない
        return 7

    def run(x):
        now[0] += 0.25
        return x * 2
    assert within(1.0, run, setup=setup) == (0.25, 14)


def test_the_clock_is_named():
    with pytest.raises(ValueError):
        within(1.0, lambda: None, clock="sundial")


def test_every_test_that_measures_a_budget_with_within_is_marked_perf():
    """perf_budget.within で時間の約束を測る試験は @pytest.mark.perf を付ける（既定の実行から外して別に走らせるため）。
    付け忘れると、全体の実行の負荷で揺れる試験が既定の実行に残る。"""
    import ast
    from pathlib import Path
    unmarked = []
    for path in sorted(Path(__file__).parent.glob("test_*.py")):
        if path.name == Path(__file__).name:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
                calls_within = any(isinstance(c, ast.Call) and getattr(c.func, "id", None) == "within" for c in ast.walk(node))
                marked = any(ast.unparse(d) == "pytest.mark.perf" for d in node.decorator_list)
                if calls_within and not marked:
                    unmarked.append(f"{path.name}::{node.name}")
    assert not unmarked, f"within を使うのに perf の印が無い: {unmarked}"
