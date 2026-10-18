"""サイクルの計算（U12）: DWT の CYCCNT（32 ビット）・イベントカウンタ（8 ビット）の差、CPU クロックの推定、
命令数、区間のサイクルの統計。入出力を持たない（読むのは本流の probe_cycles）。

- CPU クロック: DAP サンプラ（FPGA）が CYCCNT を周期的に読み、デバイスのタイムベースの時刻を付ける。その (時刻, CYCCNT)
  の直線の傾き（最小二乗）がクロック。サンプラが無ければ、ホストから 2 回読んだ値と、その前後のタイムベースの時刻で
  挟む（clock_from_two_reads。誤差は USB の往復の幅で決まる）。
- 命令数（ARMv7-M C1.8.1）: 命令 = CYCCNT − CPICNT − EXCCNT − SLEEPCNT − LSUCNT + FOLDCNT。
"""

import statistics
from dataclasses import dataclass, field

from .stats import Distribution

__all__ = ["ClockEstimate", "clock_from_two_reads", "counter_delta", "estimate_clock", "instructions",
           "interval_summary", "unwrap"]


def unwrap(values: list[int], bits: int = 32) -> list[int]:
    """折り返すカウンタの値の列を、増え続ける値にする（隣どうしの間に 1 周を超えて進まないこと）。"""
    if not values:
        return []
    modulo = 1 << bits
    out = [values[0]]
    for v in values[1:]:
        out.append(out[-1] + ((v - out[-1]) % modulo))
    return out


@dataclass
class ClockEstimate:
    clock_hz: float
    stderr_hz: float                       # 推定の標準誤差（2 点のときは読みの時刻の幅による上限）
    samples: int
    span_s: float
    max_residual_cycles: float = 0.0
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"clock_hz": self.clock_hz, "clock_mhz": self.clock_hz / 1e6, "stderr_hz": self.stderr_hz,
                "ppm": self.stderr_hz / self.clock_hz * 1e6 if self.clock_hz else None, "samples": self.samples,
                "span_s": self.span_s, "max_residual_cycles": self.max_residual_cycles, "warnings": list(self.warnings)}


def estimate_clock(samples: list[tuple[int, int]], timebase_hz: float) -> ClockEstimate:
    """(タイムベースの時刻, CYCCNT) の列から CPU クロック（最小二乗の傾き）。CYCCNT が止まった区間（コアの停止・
    クロックを止める眠り）があれば警告する（その間の分だけ見かけのクロックが下がる）。"""
    if len(samples) < 3:
        raise ValueError("CPU クロックの推定には 3 点以上の読みが要る")
    samples = sorted(samples)
    ts = [t for t, _ in samples]
    cs = unwrap([c for _, c in samples])
    xs = [(t - ts[0]) / timebase_hz for t in ts]
    ys = [c - cs[0] for c in cs]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        raise ValueError("読みの時刻がすべて同じ")
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    intercept = my - slope * mx
    residuals = [y - (intercept + slope * x) for x, y in zip(xs, ys)]
    n = len(xs)
    stderr = (sum(r * r for r in residuals) / (n - 2) / sxx) ** 0.5 if n > 2 else 0.0
    warnings = []
    stalled = sum(1 for a, b in zip(ys, ys[1:]) if a == b)
    if stalled:
        warnings.append(f"CYCCNT が {stalled} 回の読みの間で止まった（コアの停止・クロックを止める眠り）: その分だけ"
                        "見かけのクロックが下がる。走っている区間で測る")
    return ClockEstimate(slope, stderr, n, xs[-1], max(abs(r) for r in residuals), warnings)


def clock_from_two_reads(first: tuple[int, int, int], second: tuple[int, int, int],
                         timebase_hz: float) -> ClockEstimate:
    """2 回の読み（読む前のタイムベース, 読んだ後のタイムベース, CYCCNT）から。読みの時刻は前後の中点、誤差は幅の半分ずつ。"""
    (a0, a1, c0), (b0, b1, c1) = first, second
    span = ((b0 + b1) - (a0 + a1)) / 2 / timebase_hz
    if span <= 0:
        raise ValueError("2 回目の読みが 1 回目より後でない")
    delta = (c1 - c0) % (1 << 32)
    clock = delta / span
    width = ((a1 - a0) + (b1 - b0)) / 2 / timebase_hz
    return ClockEstimate(clock, clock * width / span, 2, span)


def counter_delta(start: int, end: int, overflows: int | None = None, bits: int = 8) -> int:
    """イベントカウンタ（bits ビット）の start → end の増分。overflows は間に受けた溢れのパケットの数（SWO）。
    無ければ 1 周未満とみなす。数えた溢れと値が合わない（パケットが欠けた）ときも 1 周未満の分だけ。"""
    modulo = 1 << bits
    if overflows is not None:
        delta = end - start + modulo * overflows
        if delta >= 0:
            return delta
    return (end - start) % modulo


def instructions(counts: dict[str, int]) -> int:
    """実行した命令の数（cycles と 5 つのカウンタの差から）。"""
    return counts["cycles"] - counts["cpi"] - counts["exc"] - counts["sleep"] - counts["lsu"] + counts["fold"]


def interval_summary(values: list[int], clock_hz: float | None = None) -> dict:
    """区間のサイクルの統計（clock_hz があれば µs も）。"""
    d = Distribution(values)
    out = {"count": d.count, "min": d.min, "max": d.max, "mean": d.mean, "median": d.median, "stdev": d.stdev}
    if clock_hz:
        out["time_us"] = {k: out[k] / clock_hz * 1e6 for k in ("min", "max", "mean", "median", "stdev")}
    return out
