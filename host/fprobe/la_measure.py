"""ロジアナの測定（Saleae の Measurement 拡張の上位）: 範囲の中の、チャンネルごとの周波数・周期・デューティ・パルス幅の
統計と、チャンネル間のエッジの時間差（遅れ・セットアップ / ホールド）。

    measure(tr, start=0, end=None, channels=None, relations=None)        # 捕捉（Transitions）。位置はサンプル番号
    measure_recording(recording, t0, t1, channels, relations)            # 記録の波形。位置はタイムベース

- 範囲 [start, end) の中の変化（start ちょうどの変化は範囲の始まりの値として扱い、数えない）だけを使う。
  周期（立ち上がり → 次の立ち上がり）・high（立ち上がり → 立ち下がり）・low（立ち下がり → 立ち上がり）は、両端が範囲の中に
  ある完全なものだけ。範囲の端で切れたものは数えない（推定しない）。
- 記録の波形は、かたまり（捕捉）ごとに測ってから合わせる。かたまりの間は観測していないので、かたまりをまたぐ周期・時間差は
  作らない。
- 時刻の分解能は 1 サンプル（各エッジ ±1 サンプル）。結果の resolution_ns。
- 統計は件数・最小・最大・平均・標準偏差（母）と、最小・最大の位置（min_at / max_at。at_unit がサンプルかタイムベースか）。

チャンネル間（relations の要素）:
- {"type": "delay", "from": ch, "from_edge": "rise"|"fall"|"any", "to": ch, "to_edge": …}: from のエッジから、次の from の
  エッジまでに来た最初の to のエッジまでの時間（recording.intervals と同じ組の規則）。来なければ unmatched。
- {"type": "setup_hold", "clock": ch, "clock_edge": "rise"|"fall", "data": ch か [ch, …], "setup_min_ns", "hold_min_ns"}:
  クロックのエッジごとに、セットアップ = その時刻以前の最後のデータの変化から、ホールド = その時刻以後の最初のデータの変化まで
  （同じサンプルの変化は両方 0。範囲の中に変化が無い側は数えない）。最小値を与えれば違反の一覧。
"""

from bisect import bisect_left, bisect_right
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
import math
import statistics
from typing import Any

from .la import Transitions

__all__ = ["VIOLATION_LIMIT", "measure", "measure_recording"]

VIOLATION_LIMIT = 100
EDGES = ("rise", "fall", "any")


# --- 統計の積み上げ ---------------------------------------------------------------------------------

@dataclass
class _Series:
    """時間（秒）と位置の列。かたまりをまたいで足す。"""
    values: list[float] = field(default_factory=list)
    places: list[float] = field(default_factory=list)

    def add(self, seconds: float, at: float) -> None:
        self.values.append(seconds)
        self.places.append(at)

    def stats(self) -> dict:
        if not self.values:
            return {"count": 0}
        lo = min(range(len(self.values)), key=self.values.__getitem__)
        hi = max(range(len(self.values)), key=self.values.__getitem__)
        return {"count": len(self.values), "min_ns": self.values[lo] * 1e9, "max_ns": self.values[hi] * 1e9,
                "mean_ns": statistics.fmean(self.values) * 1e9, "stdev_ns": statistics.pstdev(self.values) * 1e9,
                "min_at": self.places[lo], "max_at": self.places[hi]}


@dataclass
class _Channel:
    initial: int | None = None
    rises: int = 0
    falls: int = 0
    high_s: float = 0.0                 # 範囲で high だった時間
    span_s: float = 0.0                 # 範囲の長さ
    period: _Series = field(default_factory=_Series)
    high: _Series = field(default_factory=_Series)
    low: _Series = field(default_factory=_Series)
    duty: list[float] = field(default_factory=list)     # 完全な周期ごとの high の割合
    period_high_s: float = 0.0
    period_s: float = 0.0

    def report(self) -> dict:
        mean = statistics.fmean(self.period.values) if self.period.values else None
        return {"initial": self.initial, "rises": self.rises, "falls": self.falls,
                "high_ratio": self.high_s / self.span_s if self.span_s else None,
                "frequency_hz": 1 / mean if mean else None,
                "duty": self.period_high_s / self.period_s if self.period_s else None,
                "duty_min": min(self.duty) if self.duty else None, "duty_max": max(self.duty) if self.duty else None,
                "period": self.period.stats(), "high": self.high.stats(), "low": self.low.stats()}


@dataclass
class _Delay:
    spec: dict
    series: _Series = field(default_factory=_Series)
    unmatched: int = 0

    def report(self) -> dict:
        return {**self.spec, **self.series.stats(), "unmatched": self.unmatched}


@dataclass
class _SetupHold:
    spec: dict
    setup: _Series = field(default_factory=_Series)
    hold: _Series = field(default_factory=_Series)
    violations: list[dict] = field(default_factory=list)
    violation_count: int = 0

    def report(self) -> dict:
        out = {**self.spec, "setup": self.setup.stats(), "hold": self.hold.stats()}
        if self.spec.get("setup_min_ns") is not None or self.spec.get("hold_min_ns") is not None:
            out.update(violation_count=self.violation_count, violations=self.violations)
        return out


# --- 1 つの捕捉（かたまり）の変化 -----------------------------------------------------------------------

class _Edges:
    """範囲 (start, end) のチャンネルごとのエッジ（サンプル番号と、立ち上がりか）。"""

    def __init__(self, tr: Transitions, start: int, end: int, channels: set[int]):
        self.start, self.end = start, end
        first = max(bisect_right(tr.starts, start) - 1, 0)
        last = bisect_left(tr.starts, end)
        self.initial = {ch: (tr.values[first] >> ch & 1) if len(tr.values) else 0 for ch in channels}
        self.at: dict[int, list[int]] = {ch: [] for ch in channels}
        self.rising: dict[int, list[bool]] = {ch: [] for ch in channels}
        mask = sum(1 << ch for ch in channels)
        previous = tr.values[first] if len(tr.values) else 0
        for i in range(first + 1, last):
            value, s = tr.values[i], tr.starts[i]
            changed = (value ^ previous) & mask
            while changed:
                bit = changed & -changed
                ch = bit.bit_length() - 1
                self.at[ch].append(s)
                self.rising[ch].append(bool(value & bit))
                changed ^= bit
            previous = value

    def of(self, ch: int, edge: str) -> list[int]:
        if edge == "any":
            return self.at[ch]
        want = edge == "rise"
        return [s for s, r in zip(self.at[ch], self.rising[ch]) if r == want]


class _Measurement:
    def __init__(self, channels: Sequence[int] | None, relations: Sequence[dict] | None, channel_count: int):
        self.explicit = channels is not None
        self.channels = list(range(channel_count)) if channels is None else list(dict.fromkeys(channels))
        for ch in self.channels:
            if not 0 <= ch < channel_count:
                raise ValueError(f"チャンネル {ch} は無い（0〜{channel_count - 1}）")
        self.relations = [self._relation(r, channel_count) for r in relations or []]
        self.per = {ch: _Channel() for ch in self.channels}
        self.resolution_s = 0.0

    @staticmethod
    def _relation(r: dict, channel_count: int):
        kind = r.get("type")

        def channel(name: str) -> int:
            ch = r.get(name)
            if not isinstance(ch, int) or not 0 <= ch < channel_count:
                raise ValueError(f"{kind} の {name} はチャンネル番号（0〜{channel_count - 1}）")
            return ch

        def edge(name: str, default: str, allowed=EDGES) -> str:
            e = r.get(name, default)
            if e not in allowed:
                raise ValueError(f"{kind} の {name} は {' / '.join(allowed)}")
            return e
        if kind == "delay":
            return _Delay({"type": kind, "from": channel("from"), "from_edge": edge("from_edge", "any"),
                           "to": channel("to"), "to_edge": edge("to_edge", "any")})
        if kind == "setup_hold":
            data = r.get("data")
            data = [data] if isinstance(data, int) else data
            if not data or not all(isinstance(d, int) and 0 <= d < channel_count for d in data):
                raise ValueError(f"setup_hold の data はチャンネル番号か、その列（0〜{channel_count - 1}）")
            return _SetupHold({"type": kind, "clock": channel("clock"),
                               "clock_edge": edge("clock_edge", "rise", ("rise", "fall")), "data": list(data),
                               "setup_min_ns": r.get("setup_min_ns"), "hold_min_ns": r.get("hold_min_ns")})
        raise ValueError(f"知らない relation の type {kind!r}（delay / setup_hold）")

    def _needed(self) -> set[int]:
        out = set(self.channels)
        for r in self.relations:
            s = r.spec
            out |= {s["from"], s["to"]} if s["type"] == "delay" else {s["clock"], *s["data"]}
        return out

    def add(self, tr: Transitions, start: int, end: int, locate: Callable[[int], float]) -> None:
        """1 つの捕捉の範囲 [start, end)（サンプル）を測って積む。locate はサンプル番号 → 報告する位置。"""
        dt = tr.sample_period_s
        self.resolution_s = max(self.resolution_s, dt)
        edges = _Edges(tr, start, end, self._needed())
        for ch in self.channels:
            self._channel(self.per[ch], edges, ch, dt, locate)
        for r in self.relations:
            if isinstance(r, _Delay):
                self._delay(r, edges, dt, locate)
            else:
                self._setup_hold(r, edges, dt, locate)

    def _channel(self, c: _Channel, edges: _Edges, ch: int, dt: float, locate) -> None:
        at, rising, level = edges.at[ch], edges.rising[ch], edges.initial[ch]
        if c.initial is None:
            c.initial = level
        c.span_s += (edges.end - edges.start) * dt
        previous = edges.start
        for s, r in zip(at, rising):
            if not r:                                   # 立ち下がり: それまで high
                c.high_s += (s - previous) * dt
            previous = s
        if (edges.initial[ch] if not at else rising[-1]):
            c.high_s += (edges.end - previous) * dt
        c.rises += sum(rising)
        c.falls += len(rising) - sum(rising)
        for i in range(1, len(at)):                     # 完全なパルス（両端の変化が範囲の中）
            (c.high if rising[i - 1] else c.low).add((at[i] - at[i - 1]) * dt, locate(at[i - 1]))
        rises = [i for i, r in enumerate(rising) if r]
        for a, b in zip(rises, rises[1:]):              # 立ち上がり → 次の立ち上がり（間に立ち下がりが 1 つ）
            period = (at[b] - at[a]) * dt
            high = (at[a + 1] - at[a]) * dt
            c.period.add(period, locate(at[a]))
            c.duty.append(high / period)
            c.period_high_s += high
            c.period_s += period

    @staticmethod
    def _delay(r: _Delay, edges: _Edges, dt: float, locate) -> None:
        s = r.spec
        starts, ends = edges.of(s["from"], s["from_edge"]), edges.of(s["to"], s["to_edge"])
        j = 0
        for i, a in enumerate(starts):
            limit = starts[i + 1] if i + 1 < len(starts) else None
            while j < len(ends) and ends[j] < a:
                j += 1
            if j < len(ends) and (limit is None or ends[j] <= limit):
                r.series.add((ends[j] - a) * dt, locate(a))
                j += 1
            else:
                r.unmatched += 1

    @staticmethod
    def _setup_hold(r: _SetupHold, edges: _Edges, dt: float, locate) -> None:
        s = r.spec
        changes = sorted(set().union(*(edges.at[d] for d in s["data"])))
        setup_min, hold_min = s["setup_min_ns"], s["hold_min_ns"]
        for c in edges.of(s["clock"], s["clock_edge"]):
            where = locate(c)
            violation: dict[str, Any] = {}
            before = bisect_right(changes, c) - 1
            if before >= 0:
                setup = (c - changes[before]) * dt
                r.setup.add(setup, where)
                if setup_min is not None and setup * 1e9 < setup_min:
                    violation["setup_ns"] = setup * 1e9
            after = bisect_left(changes, c)
            if after < len(changes):
                hold = (changes[after] - c) * dt
                r.hold.add(hold, where)
                if hold_min is not None and hold * 1e9 < hold_min:
                    violation["hold_ns"] = hold * 1e9
            if violation:
                r.violation_count += 1
                if len(r.violations) < VIOLATION_LIMIT:
                    r.violations.append({"at": where, **violation})

    def report(self, unit: str) -> dict:
        channels = {ch: c.report() for ch, c in self.per.items()
                    if self.explicit or c.rises or c.falls}
        return {"at_unit": unit, "resolution_ns": self.resolution_s * 1e9, "channels": channels,
                "relations": [r.report() for r in self.relations]}


# --- 入口 --------------------------------------------------------------------------------------------

def measure(tr: Transitions, start: int = 0, end: int | None = None, channels: Sequence[int] | None = None,
            relations: Sequence[dict] | None = None) -> dict:
    """捕捉の範囲 [start, end)（サンプル番号。end 省略で最後まで）を測る。位置（*_at・violations の at）はサンプル番号。"""
    end = tr.length if end is None else min(end, tr.length)
    start = max(0, start)
    if end <= start:
        raise ValueError(f"測る範囲が空（start={start}, end={end}）")
    m = _Measurement(channels, relations, tr.channels)
    m.add(tr, start, end, lambda s: s)
    out = m.report("sample")
    out.update(start=start, end=end)
    return out


def measure_recording(recording, t0: int | None = None, t1: int | None = None,
                      channels: Sequence[int] | None = None, relations: Sequence[dict] | None = None) -> dict:
    """記録の波形の時刻の範囲 [t0, t1)（タイムベース。省略で全体）を、かたまりごとに測って合わせる。位置はタイムベース。"""
    if t0 is not None and t1 is not None and t1 <= t0:
        raise ValueError(f"測る範囲が空（t0={t0}, t1={t1}）")
    chunks = list(recording.waveforms(t0, t1))
    if not chunks:
        raise ValueError("範囲に波形が無い")
    m = _Measurement(channels, relations, max(tr.channels for _, _, tr in chunks))
    for base, tps, tr in chunks:
        s0 = 0 if t0 is None else max(0, math.ceil((t0 - base) / tps))
        s1 = tr.length if t1 is None else min(tr.length, math.ceil((t1 - base) / tps))
        if s1 > s0:
            m.add(tr, s0, s1, lambda s, base=base, tps=tps: base + round(s * tps))
    out = m.report("timebase")
    out.update(t0=t0, t1=t1, chunks=len(chunks))
    return out
