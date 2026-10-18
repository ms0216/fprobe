"""縮図の事前計算（fprobe.recording_summary）: 区間の分解と、段ごとの件数の表。"""

import random
import sqlite3

import pytest

from fprobe.recording_summary import FAN, SummaryGrid, bin_counts, decompose


def _covered(raw, bins, w0):
    """分解の結果が覆う区間の列（試験用に展開）。"""
    spans = list(raw)
    for level, lo, hi in bins:
        w = w0 * FAN ** level
        spans.append((lo * w, hi * w))
    return sorted(spans)


@pytest.mark.parametrize("seed", range(20))
def test_decompose_covers_exactly_once(seed):
    rng = random.Random(seed)
    w0 = 1 << rng.randrange(0, 6)
    t0 = rng.randrange(0, 10 ** 7)
    t1 = t0 + rng.randrange(1, 10 ** 7)
    raw, bins = decompose(t0, t1, w0, levels=6)
    spans = _covered(raw, bins, w0)
    assert spans[0][0] == t0 and spans[-1][1] == t1
    for (a0, a1), (b0, b1) in zip(spans, spans[1:]):
        assert a1 == b0                                            # 隙間も重なりも無い
    for a, b in raw:
        assert b - a < w0                                          # 生で数えるのは最も細かい区間より短い端だけ
    per_level = {}
    for level, lo, hi in bins:
        per_level[level] = per_level.get(level, 0) + 1
        if level < 5:
            assert hi - lo < 2 * FAN                               # 段ごとに端の数個だけ（最上段を除く）
    assert all(n <= 2 for n in per_level.values())


def test_decompose_short_range_is_raw():
    assert decompose(5, 9, 16) == ([(5, 9)], [])


def test_decompose_aligned_range_uses_coarse_bins():
    w0 = 4
    raw, bins = decompose(0, w0 * FAN ** 2, w0, levels=4)
    assert raw == [] and bins == [(2, 0, 1)]


def test_bin_counts_and_grid_count_match_raw():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE events (t INTEGER, stream TEXT)")
    rng = random.Random(1)
    rows = [(rng.randrange(0, 200_000), rng.choice("abc")) for _ in range(5000)]
    db.executemany("INSERT INTO events VALUES (?, ?)", rows)
    grid = SummaryGrid(w0=8, levels=5)
    table = bin_counts(db, grid)
    assert set(table) == set(range(5))
    for level, rows_ in table.items():
        assert sum(n for (_, _, n) in rows_) == 5000
    lookup = {(lvl, s, b): n for lvl, rs in table.items() for s, b, n in rs}

    def bins_count(level, lo, hi, streams):
        return {s: sum(n for (lv, s2, b), n in lookup.items() if lv == level and s2 == s and lo <= b < hi)
                for s in "abc" if streams is None or s in streams}

    def raw_count(a, b, streams):
        out = {}
        for t, s in rows:
            if a <= t < b and (streams is None or s in streams):
                out[s] = out.get(s, 0) + 1
        return out

    for _ in range(30):
        t0 = rng.randrange(0, 200_000)
        t1 = t0 + rng.randrange(1, 100_000)
        want = raw_count(t0, t1, None)
        got = grid.count(t0, t1, bins_count, raw_count, None)
        assert {k: v for k, v in got.items() if v} == want
