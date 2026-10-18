"""2 点間の時間とばらつき（DT+ の 2 点間の時間・Tracealyzer の Timing Metrics）。

    report = measure(recording, {"stream": "uart", "kind": "rx", "b": 0x53}, {"stream": "uart", "kind": "tx"})
    report.as_dict()        # 一覧・最小・最大・平均・σ・分位・箱・外れ値（開始の時刻へ跳べる）

組の規則は fprobe.recording.Recording.intervals と同じ（開始ごとに、次の開始までに来た最初の終了。来なければ捨てる）。
仕様: docs/superpowers/specs/2026-10-17-f5-profile-design.md の 5 節。
"""

from collections.abc import Iterable
from dataclasses import dataclass

from .stats import Distribution

__all__ = ["IntervalReport", "interval_report", "measure", "pair_times"]


def pair_times(starts: Iterable[int], ends: Iterable[int]) -> list[tuple[int, int]]:
    """時刻の列どうしを (開始の時刻, 時間) の組にする（記録を使わない入力向け。Recording.intervals と同じ規則）。"""
    s, e = sorted(starts), sorted(ends)
    pairs, j = [], 0
    for i, t in enumerate(s):
        limit = s[i + 1] if i + 1 < len(s) else None
        while j < len(e) and e[j] < t:
            j += 1
        if j < len(e) and (limit is None or e[j] <= limit):
            pairs.append((t, e[j] - t))
            j += 1
    return pairs


@dataclass
class IntervalReport:
    pairs: list[tuple[int, int]]                 # (開始の時刻, 時間)。時刻・時間はタイムベース
    timebase_hz: float | None = None
    bins: int = 20
    outlier_rule: str = "iqr"

    @property
    def distribution(self) -> Distribution:
        return Distribution([d for _, d in self.pairs])

    def outliers(self) -> list[dict]:
        """外れ値（点数の大きい順）。t は開始の時刻（記録・タイムラインのその時刻へ跳ぶ鍵）。"""
        return [{"t": self.pairs[i][0], "t_end": self.pairs[i][0] + self.pairs[i][1], "duration": self.pairs[i][1],
                 "score": score} for i, score in self.distribution.outliers(self.outlier_rule)]

    def as_dict(self) -> dict:
        d = self.distribution
        out: dict = {"count": d.count, "unit": "ticks", "timebase_hz": self.timebase_hz}
        if d.count:
            stats = d.as_dict()
            out.update(stats)
            out["min_at"] = next(t for t, v in self.pairs if v == d.min)
            out["max_at"] = next(t for t, v in self.pairs if v == d.max)
            out["histogram"] = d.histogram(self.bins)
            out["outliers"] = self.outliers()
            if self.timebase_hz:
                out["seconds"] = {k: stats[k] / self.timebase_hz
                                  for k in ("min", "max", "mean", "stdev", "median", "p90", "p99")}
        out["intervals"] = [list(p) for p in self.pairs]
        return out


def interval_report(pairs: Iterable[tuple[int, int]], timebase_hz: float | None = None, *, bins: int = 20,
                    outliers: str = "iqr") -> IntervalReport:
    """(開始の時刻, 時間) の組から統計を作る。"""
    if outliers not in ("iqr", "zscore"):
        raise ValueError(f"外れ値の規則は iqr か zscore: {outliers!r}")
    return IntervalReport([(int(t), d) for t, d in pairs], timebase_hz, bins, outliers)


def measure(recording, start: dict, end: dict, *, bins: int = 20, outliers: str = "iqr") -> IntervalReport:
    """共通の時間軸の記録（fprobe.recording.Recording）の 2 点間の時間。条件は Recording.find と同じ形。"""
    pairs = recording.intervals(start, end)["intervals"]
    return interval_report(pairs, recording.meta.get("timebase_hz"), bins=bins, outliers=outliers)
