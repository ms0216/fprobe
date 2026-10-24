"""fprobe.profile.stats: 時間の統計・分布。"""

import importlib

import pytest

import fprobe.profile
import fprobe.profile.stats
from fprobe.profile.stats import Distribution


def test_no_confidence_interval_in_the_body():
    # 割合の信頼区間（Wilson）は独立な二項の試行という仮定の上の推定: 本体に置かない（plugins.heuristics.wilson_interval）
    assert not hasattr(fprobe.profile.stats, "wilson_interval") and not hasattr(fprobe.profile, "wilson_interval")
    with pytest.raises(ImportError):
        importlib.import_module("fprobe.profile.aliasing")


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


def test_no_outlier_rule_of_thumb():
    # 外れ値の経験則（Tukey の 1.5 IQR・3σ）は持たない（CLAUDE.md）。長い順は呼び手が値を並べて見る
    assert not hasattr(Distribution([1, 2, 3]), "outliers")


def test_empty_distribution():
    d = Distribution([])
    assert d.count == 0 and d.histogram(5) == []
    assert d.as_dict() == {"count": 0}
