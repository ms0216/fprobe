"""fprobe.profile.stats: 割合の信頼区間と、時間の統計・分布・外れ値。"""

import pytest

from fprobe.profile.stats import Distribution, wilson_interval


def test_wilson_known_values():
    # 教科書の値（95%、z = 1.959964）
    lo, hi = wilson_interval(5, 10)
    assert lo == pytest.approx(0.2366, abs=1e-4) and hi == pytest.approx(0.7634, abs=1e-4)
    lo, hi = wilson_interval(0, 10)
    assert lo == 0 and hi == pytest.approx(0.2775, abs=1e-4)
    lo, hi = wilson_interval(10, 10)
    assert lo == pytest.approx(0.7225, abs=1e-4) and hi == pytest.approx(1.0)


def test_wilson_narrows_with_samples_and_confidence():
    w100 = wilson_interval(30, 100)
    w10000 = wilson_interval(3000, 10000)
    assert w10000[1] - w10000[0] < (w100[1] - w100[0]) / 5
    assert wilson_interval(30, 100, 0.99)[1] > w100[1]


def test_wilson_without_samples_is_unknown():
    assert wilson_interval(0, 0) == (0.0, 1.0)
    with pytest.raises(ValueError):
        wilson_interval(3, 2)


def test_distribution_summary():
    d = Distribution([10, 20, 30, 40, 50])
    assert (d.count, d.min, d.max, d.mean, d.median) == (5, 10, 50, 30, 30)
    assert d.stdev == pytest.approx(14.142, abs=1e-3)              # 母標準偏差（Recording.intervals と同じ）
    assert d.quantile(0.9) == pytest.approx(46)
    assert d.quantile(0.0) == 10 and d.quantile(1.0) == 50


def test_histogram_bins_cover_everything():
    values = list(range(100))
    bins = Distribution(values).histogram(10)
    assert len(bins) == 10
    assert sum(b["count"] for b in bins) == 100
    assert bins[0]["lo"] == 0 and bins[-1]["hi"] == 99
    assert all(b["count"] == 10 for b in bins)


def test_histogram_of_equal_values_is_one_bin():
    bins = Distribution([7, 7, 7]).histogram(10)
    assert bins == [{"lo": 7, "hi": 7, "count": 3}]


def test_outliers_iqr_and_zscore():
    values = [100] * 20 + [101] * 20 + [99] * 20 + [500]
    d = Distribution(values)
    out = d.outliers("iqr")
    assert [i for i, _ in out] == [60]
    assert out[0][1] > 1                                             # 柵からの距離（四分位範囲の倍数）
    many = [100 + (i % 7) for i in range(200)] + [1000]
    assert [i for i, _ in Distribution(many).outliers("zscore")] == [200]
    with pytest.raises(ValueError):
        d.outliers("nope")


def test_empty_distribution():
    d = Distribution([])
    assert d.count == 0 and d.histogram(5) == [] and d.outliers() == []
    assert d.as_dict() == {"count": 0}
