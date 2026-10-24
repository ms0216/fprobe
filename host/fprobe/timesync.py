"""ボードの間の時刻の同期: 子の記録の組（stream sync、kind pair。子の時刻・親の時刻）から、子の時刻を親の時刻に換算する
（docs/superpowers/specs/2026-10-22-modular-design.md §3.3）。

組は線の同じ辺を両方の時計で見たもの。FPGA の遅れ（親: 送る時刻を決めてから線が 1 になるまで TX、子: 線が 1 になってから
記録するまで RX。どちらも回路で決まる定数）を引いてから、最小二乗の直線で合わせる。子は非同期の辺を自分の時計で取り込む
ので、辺は RX の分より 0 以上 1 刻み未満前のどこか（SYNC_RX_PHASE_SPAN_TICKS。どこかは測っていない）: 合わせには最も前
（RX + 幅）を使い、換算した親の時刻は「t − 幅 より後、t 以前」（phase_span_ticks）。不確かさは測った事実だけを返す
（時刻の量子化 1 刻み・位相の幅・組の残差の最大・最も近い組までの刻み）。配線の遅れは測っていないので None（使う側が明示して足す）。
"""

import bisect
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from . import generated as g
from .fpga_obs import fit_line

# 子の時刻 → 線の辺（最も前）: 回路の遅れ（周期）と、非同期の辺を取り込む位相の幅
_RX = g.SYNC_RX_LATENCY_TICKS + g.SYNC_RX_PHASE_SPAN_TICKS

__all__ = ["ParentTime", "fit_parent", "interpolator", "segments", "to_parent"]


@dataclass(frozen=True)
class ParentTime:
    """親の時刻 = parent0 + ratio × (子の時刻 - local0)。"""
    local0: int
    parent0: float
    ratio: float
    residual_max: float          # 組の、直線からのずれの最大（親の刻み）
    pairs: int


def fit_parent(pairs: list[tuple[int, int]]) -> ParentTime | None:
    """組 (子の刻み, 親の刻み) の直線。2 組未満は傾きが決まらないので None。"""
    if len(pairs) < 2:
        return None
    edges = [(local - _RX, parent + g.SYNC_TX_LATENCY_TICKS) for local, parent in pairs]
    local0, parent0, ratio = fit_line(edges)
    residual = max(abs(parent - (parent0 + ratio * (local - local0))) for local, parent in edges)
    return ParentTime(local0, parent0, ratio, residual, len(pairs))


def to_parent(fit: ParentTime, local: int, pair_locals: Sequence[int]) -> dict:
    """子の時刻 local を親の刻みに。pair_locals は合わせに使った組の子の時刻（最も近い組までの刻みを返す）。"""
    return {"t": round(fit.parent0 + fit.ratio * (local - fit.local0)),
            "quantization_ticks": 1,
            "phase_span_ticks": g.SYNC_RX_PHASE_SPAN_TICKS,
            "residual_max_ticks": fit.residual_max,
            "nearest_pair_ticks": min(abs(local - p) for p in pair_locals),
            "cable_delay": None}


def segments(pairs: list[tuple[int, int]], gap_ticks: int | None) -> list[list[tuple[int, int]]]:
    """子の時刻の順の組を、間が gap_ticks（使う側が明示。None なら分けない）を超える所で区間に分ける（線が抜けた等）。"""
    out: list[list[tuple[int, int]]] = []
    for pair in sorted(pairs):
        if out and (gap_ticks is None or pair[0] - out[-1][-1][0] <= gap_ticks):
            out[-1].append(pair)
        else:
            out.append([pair])
    return out


def interpolator(pairs: list[tuple[int, int]]) -> Callable[[float], float]:
    """両側の組の間の直線（内挿）で子の時刻を親の刻みに。2 枚の時計の速さの比が記録の間に変わっても（温度等）、
    組の分解能で合う（全体に 1 本の直線 fit_parent では、比の揺れの分だけ場所によってずれる）。
    組の範囲の外は、端の 2 組の直線をそのまま延ばす（範囲の外を使うかは呼ぶ側が決める）。2 組以上が要る。"""
    edges = sorted((local - _RX, parent + g.SYNC_TX_LATENCY_TICKS) for local, parent in pairs)
    if len(edges) < 2:
        raise ValueError("内挿には組が 2 つ以上要る")
    xs = [x for x, _ in edges]

    def to(local: float) -> float:
        i = min(max(bisect.bisect_right(xs, local) - 1, 0), len(edges) - 2)
        (x0, y0), (x1, y1) = edges[i], edges[i + 1]
        return y0 + (y1 - y0) * (local - x0) / (x1 - x0)
    return to
