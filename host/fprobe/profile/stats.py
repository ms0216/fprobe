"""統計の部品: 値の分布（統計・箱）。

割合の信頼区間（Wilson）は「各サンプルが独立な二項の試行」という仮定の上の推定なので本体に置かない
（plugins.heuristics.wilson_interval。点検 3 回目の H1）。

仕様: docs/superpowers/specs/2026-10-17-f5-profile-design.md の 3 節・5 節。
"""

import math
import statistics
from collections.abc import Sequence

__all__ = ["Distribution"]


class Distribution:
    """値（時間など）の分布。標準偏差は母標準偏差（Recording.intervals と同じ）。"""

    def __init__(self, values: Sequence[float]):
        self.values = list(values)
        self._sorted = sorted(self.values)

    @property
    def count(self) -> int:
        return len(self.values)

    @property
    def min(self) -> float:
        return self._sorted[0]

    @property
    def max(self) -> float:
        return self._sorted[-1]

    @property
    def mean(self) -> float:
        return statistics.fmean(self.values)

    @property
    def stdev(self) -> float:
        return statistics.pstdev(self.values)

    @property
    def median(self) -> float:
        return self.quantile(0.5)

    def quantile(self, q: float) -> float:
        """分位（並べた値の線形補間。q = 0 で最小、1 で最大）。"""
        if not self._sorted:
            raise ValueError("値が無い")
        if not 0 <= q <= 1:
            raise ValueError(f"分位は 0〜1: {q}")
        position = q * (len(self._sorted) - 1)
        low = math.floor(position)
        high = min(low + 1, len(self._sorted) - 1)
        value = self._sorted[low] + (self._sorted[high] - self._sorted[low]) * (position - low)
        return value

    def histogram(self, bins: int = 20) -> list[dict]:
        """最小〜最大を等しい幅の bins 個の箱に分ける（最後の箱は最大を含む）。全部同じ値なら箱は 1 つ。"""
        if bins < 1:
            raise ValueError("箱の数は 1 以上")
        if not self._sorted:
            return []
        lo, hi = self.min, self.max
        if lo == hi:
            return [{"lo": lo, "hi": hi, "count": self.count}]
        width = (hi - lo) / bins
        counts = [0] * bins
        for v in self._sorted:
            counts[self.bin_of(v, bins)] += 1
        return [{"lo": lo + k * width, "hi": hi if k == bins - 1 else lo + (k + 1) * width, "count": c}
                for k, c in enumerate(counts)]

    def bin_of(self, value: float, bins: int = 20) -> int:
        """値が入る histogram(bins) の箱の番号（分布の値を渡す。全部同じ値なら 0）。"""
        lo, hi = self.min, self.max
        if lo == hi:
            return 0
        return min(int((value - lo) / ((hi - lo) / bins)), bins - 1)

    def as_dict(self) -> dict:
        if not self.values:
            return {"count": 0}
        return {"count": self.count, "min": self.min, "max": self.max, "mean": self.mean, "stdev": self.stdev,
                "median": self.median, "p90": self.quantile(0.9), "p99": self.quantile(0.99)}
