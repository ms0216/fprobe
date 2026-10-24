"""サイクルの計算（U12）: DWT の CYCCNT（32 ビット）・イベントカウンタ（8 ビット）の差、CPU クロックの推定、
命令数、区間のサイクルの統計。入出力を持たない（読むのは本流の probe_cycles）。

- CPU クロック: DAP サンプラ（FPGA）が CYCCNT を周期的に読み、デバイスのタイムベースの時刻を付ける。その (時刻, CYCCNT)
  の直線の傾き（最小二乗）がクロック。サンプラが無ければ、ホストから 2 回読んだ値と、その前後のタイムベースの時刻で
  挟む（clock_from_two_reads。読みの前後の時刻で挟んだ範囲 clock_min_hz〜clock_max_hz を返す）。
  直線の傾きの標準誤差は出さない（残差が独立・等分散という仮定の上の推定。点検 3 回目の ?7。残差の事実 =
  最大・二乗平均は返す。仮定を承知で使うなら plugins.heuristics.clock_stderr）。
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
    samples: int
    span_s: float
    max_residual_cycles: float | None = None   # 直線からの残差の最大（最小二乗のとき。2 回の読みでは None）
    residual_rms_cycles: float | None = None   # 残差の二乗平均の平方根（同上）
    clock_min_hz: float | None = None          # 読みの前後の時刻で挟んだ範囲（2 回の読みのとき。直線では None）
    clock_max_hz: float | None = None
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"clock_hz": self.clock_hz, "clock_mhz": self.clock_hz / 1e6, "samples": self.samples,
                "span_s": self.span_s, "max_residual_cycles": self.max_residual_cycles,
                "residual_rms_cycles": self.residual_rms_cycles, "clock_min_hz": self.clock_min_hz,
                "clock_max_hz": self.clock_max_hz, "warnings": list(self.warnings)}


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
    warnings = []
    stalled = sum(1 for a, b in zip(ys, ys[1:]) if a == b)
    if stalled:
        warnings.append(f"CYCCNT が {stalled} 回の読みの間で止まった（コアの停止・クロックを止める眠り）: その分だけ"
                        "見かけのクロックが下がる。走っている区間で測る")
    rms = (sum(r * r for r in residuals) / n) ** 0.5
    return ClockEstimate(slope, n, xs[-1], max(abs(r) for r in residuals), rms, warnings=warnings)


def clock_from_two_reads(first: tuple[int, int, int], second: tuple[int, int, int],
                         timebase_hz: float) -> ClockEstimate:
    """2 回の読み（読む前のタイムベース, 読んだ後のタイムベース, CYCCNT）から。clock_hz は読みの時刻を前後の中点と
    したときの値、clock_min_hz〜clock_max_hz は読みが前後のどこで起きても入る範囲（進みを最も長い間 b1 − a0 と最も
    短い間 b0 − a1 で割る。最も短い間が 0 以下なら上は None）。CYCCNT の 1 周未満の進みとみなす。"""
    (a0, a1, c0), (b0, b1, c1) = first, second
    span = ((b0 + b1) - (a0 + a1)) / 2 / timebase_hz
    if span <= 0:
        raise ValueError("2 回目の読みが 1 回目より後でない")
    delta = (c1 - c0) % (1 << 32)
    longest, shortest = (b1 - a0) / timebase_hz, (b0 - a1) / timebase_hz
    return ClockEstimate(delta / span, 2, span, clock_min_hz=delta / longest,
                         clock_max_hz=delta / shortest if shortest > 0 else None)


def counter_delta(start: int, end: int, overflows: int | None = None, bits: int = 8) -> tuple[int, bool]:
    """イベントカウンタ（bits ビット）の start → end の増分と、溢れを数えて決めたか（counted）。overflows は間に受けた
    溢れのパケットの数（SWO）。overflows が無い・数えた溢れと値が合わない（パケットが欠けた）ときは 1 周未満の分と
    counted = False（何周したかは測れていない）。"""
    modulo = 1 << bits
    if overflows is not None:
        delta = end - start + modulo * overflows
        if delta >= 0:
            return delta, True
    return (end - start) % modulo, False


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
