"""統計の部品: 割合の信頼区間（Wilson）と、値の分布（統計・箱・外れ値）。

仕様: docs/superpowers/specs/2026-10-17-f5-profile-design.md の 3 節・5 節。
"""

import math
import statistics
from collections.abc import Sequence

__all__ = ["Distribution", "wilson_interval"]


def _z(confidence: float) -> float:
    if not 0 < confidence < 1:
        raise ValueError(f"信頼度は 0 と 1 の間: {confidence}")
    return statistics.NormalDist().inv_cdf(0.5 + confidence / 2)


def wilson_interval(hits: int, total: int, confidence: float = 0.95) -> tuple[float, float]:
    """二項の割合 hits / total の Wilson の区間。サンプルが少なくても、割合が 0 や 1 でも [0, 1] の外へ出ない。
    total が 0 なら何も分からない: (0, 1)。"""
    if not 0 <= hits <= total:
        raise ValueError(f"数が合わない: {hits} / {total}")
    if total == 0:
        return 0.0, 1.0
    z = _z(confidence)
    p = hits / total
    z2n = z * z / total
    center = (p + z2n / 2) / (1 + z2n)
    half = z / (1 + z2n) * math.sqrt(p * (1 - p) / total + z2n / (4 * total))
    low = 0.0 if hits == 0 else max(0.0, center - half)                # 端は丸めの誤差を残さない
    high = 1.0 if hits == total else min(1.0, center + half)
    return low, high


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
            counts[min(int((v - lo) / width), bins - 1)] += 1
        return [{"lo": lo + k * width, "hi": hi if k == bins - 1 else lo + (k + 1) * width, "count": c}
                for k, c in enumerate(counts)]

    def outliers(self, rule: str = "iqr") -> list[tuple[int, float]]:
        """外れ値の (値の位置, 点数) を点数の大きい順に。
        "iqr": Tukey の柵（第 1・第 3 四分位から四分位範囲の 1.5 倍の外）。点数 = 柵からの距離 / 四分位範囲
        （四分位範囲が 0 なら値の単位 1 で割る）。"zscore": 平均から 3 σ の外。点数 = |z|。"""
        if rule not in ("iqr", "zscore"):
            raise ValueError(f"外れ値の規則は iqr か zscore: {rule!r}")
        if self.count < 2:
            return []
        out = []
        if rule == "iqr":
            q1, q3 = self.quantile(0.25), self.quantile(0.75)
            iqr = q3 - q1
            low, high = q1 - 1.5 * iqr, q3 + 1.5 * iqr
            scale = iqr if iqr > 0 else 1.0
            for i, v in enumerate(self.values):
                if v > high:
                    out.append((i, (v - high) / scale))
                elif v < low:
                    out.append((i, (low - v) / scale))
        else:
            mean, sd = self.mean, self.stdev
            if sd == 0:
                return []
            for i, v in enumerate(self.values):
                z = abs(v - mean) / sd
                if z > 3:
                    out.append((i, z))
        return sorted(out, key=lambda item: -item[1])

    def as_dict(self) -> dict:
        if not self.values:
            return {"count": 0}
        return {"count": self.count, "min": self.min, "max": self.max, "mean": self.mean, "stdev": self.stdev,
                "median": self.median, "p90": self.quantile(0.9), "p99": self.quantile(0.99)}
