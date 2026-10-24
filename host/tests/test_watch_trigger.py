"""fprobe.watch_trigger（U43）: 変数の観測の条件つきの取得（始まり・止まり・プリトリガ・ポスト・上限）。"""

import pytest

from fprobe.watch_trigger import Capture, Condition, number_of
from value_kinds import ints, varray, vbool, venum, vfloat, vint, vna, vstruct

NAMES = {"speed", "mode"}


def cond(**spec):
    return Condition.parse(spec, NAMES)


def capture(start=None, stop=None, pre=0, post=None, max_samples=1000):
    return Capture(start, stop, pre, post, max_samples)


def test_conditions_are_validated():
    assert cond(variable="speed", op=">", value=10).describe() == {"variable": "speed", "op": ">", "value": 10,
                                                                    "edge": False}
    assert Condition.parse({"event": "xtrig"}, NAMES).event == "xtrig"
    for bad in ({"variable": "nope", "op": ">", "value": 1}, {"variable": "speed", "op": "=>", "value": 1},
                {"variable": "speed", "op": ">", "value": "x"}, {"variable": "speed", "op": ">"},
                {"event": "la"}, {"event": "xtrig", "variable": "speed"}):
        with pytest.raises(ValueError):
            Condition.parse(bad, NAMES)


def test_numbers_of_values():
    # 値の kind で決める（キーの有無で推さない）: 構造体のメンバー名が enum・value でも数にしない
    assert number_of(vbool(True)) == 1 and number_of(vfloat(2.5)) == 2.5 and number_of(venum("RUN", 1)) == 1
    assert number_of(vint(-3)) == -3 and number_of(vfloat("nan")) is None
    assert number_of(vstruct(enum=venum("A", 1), value=vint(1))) is None
    assert number_of(varray(ints(1))) is None and number_of(vna("optimized out")) is None


def test_start_on_a_value_with_pretrigger_and_stop_on_another():
    c = capture(cond(variable="speed", op=">=", value=100), cond(variable="speed", op="<", value=50), pre=20)
    for t, v in [(0, 10), (10, 20), (20, 30), (30, 100), (40, 120), (50, 40), (60, 200)]:
        c.feed(t, "speed", vint(v))
    assert c.done and c.reason == "stop" and (c.t_start, c.t_end) == (30, 50)
    assert [s[0] for s in c.samples] == [10, 20, 30, 40, 50]                      # 30 − 20 から
    assert c.samples[-1] == (50, "speed", vint(40))


def test_edge_waits_for_a_change():
    c = capture(cond(variable="speed", op=">", value=5, edge=True))
    c.feed(0, "speed", vint(10))                                                  # 最初から真: 始めない
    c.feed(1, "speed", vint(1))
    assert c.state == "armed"
    c.feed(2, "speed", vint(9))
    assert c.state == "capturing" and c.t_start == 2


def test_post_time_ends_the_capture_even_without_updates():
    c = capture(cond(variable="mode", op="==", value=2), post=100)
    c.feed(0, "mode", venum("RUN", 2))
    c.feed(50, "speed", vint(7))                                                  # 条件に無い変数も取る
    c.tick(99)
    assert c.state == "capturing"
    c.tick(150)
    assert c.done and c.reason == "post" and c.t_end == 100 and len(c.samples) == 2
    c.feed(160, "speed", vint(1))
    assert len(c.samples) == 2


def test_a_cross_trigger_event_starts_and_the_limit_ends():
    c = capture(Condition(event="xtrig"), pre=5, max_samples=3)
    c.feed(0, "speed", vint(1))
    c.feed(8, "speed", vint(2))
    c.event(10, "xtrig")
    assert c.state == "capturing" and c.t_start == 10 and c.samples == [(8, "speed", vint(2))]
    for t in (11, 12, 13):
        c.feed(t, "speed", vint(t))
    assert c.done and c.reason == "max_samples" and len(c.samples) == 3 and c.dropped == 1


def test_without_a_start_condition_it_takes_from_arming_and_can_be_cancelled():
    c = capture()
    c.feed(5, "speed", vint(1))
    assert c.state == "capturing" and c.t_start == 5
    c.cancel(9)
    assert c.done and c.reason == "cancelled" and c.t_end == 9


def test_a_stop_on_an_event():
    c = capture(None, Condition(event="xtrig"))
    c.feed(1, "speed", vint(1))
    c.event(4, "xtrig")
    assert c.done and c.reason == "stop" and c.t_end == 4
