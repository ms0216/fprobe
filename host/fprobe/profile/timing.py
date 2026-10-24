"""2 点間の時間とばらつき（DT+ の 2 点間の時間・Tracealyzer の Timing Metrics）。

    report = measure(recording, {"stream": "uart", "kind": "rx", "b": 0x53}, {"stream": "uart", "kind": "tx"})
    report.as_dict()        # 一覧・最小・最大・平均・σ・分位・箱・長い順の上位（開始の時刻へ跳べる）

組の規則は fprobe.recording.Recording.intervals と同じ（開始ごとに、次の開始までに来た最初の終了。来なければ捨てる）。
仕様: docs/superpowers/specs/2026-10-17-f5-profile-design.md の 5 節。
"""

from collections.abc import Iterable
from dataclasses import dataclass

from .stats import Distribution

__all__ = ["IntervalReport", "interval_report", "limit_intervals", "measure", "pair_times"]


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
    longest_n: int = 10                          # 長い順に返す数（外れ値の経験則は使わない）

    @property
    def distribution(self) -> Distribution:
        return Distribution([d for _, d in self.pairs])

    def longest(self) -> list[dict]:
        """長い順の上位 longest_n 件（同じ長さなら早い順）。t は開始の時刻（記録・タイムラインのその時刻へ跳ぶ鍵）、
        bin は histogram の箱の番号。"""
        top = sorted(self.pairs, key=lambda p: (-p[1], p[0]))[:self.longest_n]
        d = self.distribution
        return [{"t": t, "t_end": t + dur, "duration": dur, "bin": d.bin_of(dur, self.bins)} for t, dur in top]

    def as_dict(self) -> dict:
        d = self.distribution
        out: dict = {"count": d.count, "unit": "ticks", "timebase_hz": self.timebase_hz}
        if d.count:
            stats = d.as_dict()
            out.update(stats)
            out["min_at"] = next(t for t, v in self.pairs if v == d.min)
            out["max_at"] = next(t for t, v in self.pairs if v == d.max)
            out["histogram"] = d.histogram(self.bins)
            out["longest"] = self.longest()
            if self.timebase_hz:
                out["seconds"] = {k: stats[k] / self.timebase_hz
                                  for k in ("min", "max", "mean", "stdev", "median", "p90", "p99")}
        out["intervals"] = [list(p) for p in self.pairs]
        out["interval_bins"] = [d.bin_of(v, self.bins) for _, v in self.pairs]   # intervals と同じ並びの箱の番号
        return out


def limit_intervals(report: dict, limit: int) -> dict:
    """IntervalReport.as_dict() の生の組（intervals と、同じ並びの interval_bins）を先頭 limit 件に切る（切る前の数は
    intervals_total）。統計・箱・長い順の上位は全部の組のまま。"""
    if len(report.get("intervals", ())) > limit:
        report["intervals_total"] = len(report["intervals"])
        report["intervals"] = report["intervals"][:limit]
        report["interval_bins"] = report["interval_bins"][:limit]
    return report


def interval_report(pairs: Iterable[tuple[int, int]], timebase_hz: float | None = None, *, bins: int = 20,
                    longest: int = 10) -> IntervalReport:
    """(開始の時刻, 時間) の組から統計を作る（longest = 長い順に返す数）。"""
    if longest < 0:
        raise ValueError(f"longest は 0 以上: {longest}")
    return IntervalReport([(int(t), d) for t, d in pairs], timebase_hz, bins, longest)


def measure(recording, start: dict, end: dict, *, bins: int = 20, longest: int = 10) -> IntervalReport:
    """共通の時間軸の記録（fprobe.recording.Recording）の 2 点間の時間。条件は Recording.find と同じ形。"""
    pairs = recording.intervals(start, end)["intervals"]
    return interval_report(pairs, recording.meta.get("timebase_hz"), bins=bins, longest=longest)
