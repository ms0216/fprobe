"""fprobe.clockmap: 「すべての窓を通る直線の範囲」（time-accuracy 設計書 §4.6・§13）。"""

import random
from fractions import Fraction

from fprobe.clockmap import ASSUMPTION, ClockMap, pair, segments_by_gap


def test_exact_points_on_a_line_give_a_single_line():
    m = ClockMap([pair(x, 100 + 3 * x) for x in (0, 10, 25, 40)])
    assert m.feasible and m.slope_range() == (3, 3)
    r = m.to_probe(20)
    assert (r["t_lo"], r["t_hi"]) == (160, 160) and r["assumption"] == ASSUMPTION and r["inside"]
    t = m.to_target(160)
    assert (t["x_lo"], t["x_hi"]) == (20, 20)


def test_windows_give_the_range_every_fitting_line_allows():
    # y の窓の幅 1（[y, y + 1]）、x は点。間の点の範囲は窓の幅、外へ延ばすと広がる
    m = ClockMap([pair(0, (0, 1)), pair(100, (200, 201))])
    lo, hi = m.slope_range()
    assert (lo, hi) == (Fraction(199, 100), Fraction(201, 100))
    mid = m.to_probe(50)
    assert (mid["t_lo"], mid["t_hi"]) == (100, 101)
    out = m.to_probe(200)
    assert out["t_lo"] == 399 and out["t_hi"] == 402 and not out["inside"]


def test_the_true_line_is_always_inside_the_range():
    rng = random.Random(1)
    for _ in range(30):
        a, b = rng.uniform(-50, 50), rng.uniform(0.5, 3)
        pairs = []
        for x in sorted(rng.sample(range(0, 10_000), 12)):
            y = a + b * x
            off = rng.random()                              # 本当の値は窓 [lo, lo + 1) のどこか
            pairs.append(pair(x, (Fraction(y - off), Fraction(y - off) + 1)))
        m = ClockMap(pairs)
        assert m.feasible
        for x in (0, 1234, 5000, 9999):
            r = m.to_probe(x)
            assert r["t_lo"] <= Fraction(a + b * x) <= r["t_hi"]


def test_a_broken_assumption_is_reported_not_hidden():
    # 比が途中で変わる（3 → 1）: どの直線も全部の窓を通らない
    pairs = [pair(0, (0, 1)), pair(100, (300, 301)), pair(200, (400, 401))]
    m = ClockMap(pairs)
    assert m.feasible is False and m.first_conflict == 2
    assert m.to_probe(50)["t_lo"] is None and "assumption_broken" in m.to_probe(50)["unknown"]


def test_one_pair_leaves_the_slope_undetermined_unless_given():
    m = ClockMap([pair(10, (5, 6))])
    assert m.feasible is None and m.slope_range() is None
    assert m.to_probe(20)["unknown"] == ["slope_undetermined"]
    given = ClockMap([pair(10, (5, 6))], slope=(2, 2))            # 使う側が明示した比
    r = given.to_probe(20)
    assert (r["t_lo"], r["t_hi"]) == (25, 26) and given.summary()["slope_given"] == [2.0, 2.0]


def test_target_windows_are_supported():
    # 対象の時計の値が窓（前後の読み）で、プローブの時刻が点（印の辺）
    m = ClockMap([pair((0, 2), 100), pair((50, 52), 200)])
    lo, hi = m.slope_range()
    assert lo == Fraction(100, 52) and hi == Fraction(100, 48)
    r = m.to_probe((1, 1))
    assert r["t_lo"] <= 102 and r["t_hi"] >= 98


def test_segments_are_split_only_where_the_caller_says():
    ps = [pair(x, x) for x in (0, 1, 2, 100, 101)]
    assert [len(s) for s in segments_by_gap(ps, None)] == [5]
    assert [len(s) for s in segments_by_gap(ps, 10)] == [3, 2]
