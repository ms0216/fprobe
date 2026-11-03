"""ボードの間の時刻の同期: 子の記録の組（stream sync、kind pair。子の時刻・親の時刻）から、子の時刻を親の時刻に換算する
（docs/superpowers/specs/2026-10-22-modular-design.md §3.3、time-accuracy 設計書 §4.6・§13）。

組は線の同じ辺を両方の時計で見たもの。辺の瞬間は、子の時計では「子の時刻 − RX 以上 + 位相の幅未満」（非同期の辺を子の時計で
取り込む。時刻の約束は gateware/fprobe_gw/timing.py の冒頭）、親の時計では「親の時刻 + TX」ちょうど（親が決めた周期から線が 1 に
なる端子の辺まで。PYNQ-Z2 のように端子の前に出力の FF のあるボードは、その段も TX に入る: 段階 S3 の R9）。RX・位相の幅・TX は
回路の申告（TLV TIMING の sync_rx・sync_tx。記録の meta の timing）から取り、申告の無い古い記録は生成した表の値
（fprobe.generated。その版の回路の値で、端子の段を含まない）を使って、どちらを使ったかを返す。

換算は、組の窓をすべて通る直線の範囲（fprobe.clockmap。仮定は「区間の中で 2 つの時計の比が一定」だけ）。最小二乗は使わない
（§6 の判断 4、段階 S3）。結果は範囲 [t_lo, t_hi]（親の刻み）で、窓の幅（位相の幅・量子化）と比の不確かさを全部含む。
配線の遅れは測っていないので、使う側が明示して足す（cable_delay_ticks。暗黙の既定にしない）。
"""

from __future__ import annotations

import bisect
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from . import generated as g
from .clockmap import ASSUMPTION, ClockMap, Pair, pair

__all__ = ["NEIGHBOUR_ASSUMPTION", "ParentTime", "SyncLink", "edge_pair", "fit_parent", "link_from_timing",
           "neighbour_converter", "segments", "to_parent"]

NEIGHBOUR_ASSUMPTION = "constant_ratio_between_neighbouring_pairs"


@dataclass(frozen=True)
class SyncLink:
    """組の窓の作り方（回路の申告）: 子の辺 ∈ [子の時刻 − rx, 子の時刻 − rx + span]、親の辺 = 親の時刻 + tx。"""
    rx: int
    span: int
    tx: int
    source: str                 # "TIMING"（記録の meta の申告）/ "generated"（申告の無い古い記録: この版の表の値）


DEFAULT_LINK = SyncLink(g.SYNC_RX_LATENCY_TICKS, g.SYNC_RX_PHASE_SPAN_TICKS, g.SYNC_TX_LATENCY_TICKS, "generated")


def link_from_timing(child_timing: dict | None, parent_timing: dict | None) -> SyncLink:
    """記録の meta の timing（情報源 → {"delay", "span", ...}。probe_recording が caps.timing から書く）から。子の sync_rx と親の
    sync_tx の両方が申告されていればそれ（tx = −delay: 時刻 = 端子の辺 + delay）、無ければ DEFAULT_LINK。"""
    rx = (child_timing or {}).get("sync_rx")
    tx = (parent_timing or {}).get("sync_tx")
    if not rx or not tx or rx.get("delay") is None or rx.get("span") is None or tx.get("delay") is None:
        return DEFAULT_LINK
    return SyncLink(rx["delay"], rx["span"], -tx["delay"], "TIMING")


def edge_pair(local: int, parent: int, link: SyncLink = DEFAULT_LINK, cable_delay_ticks: int = 0) -> Pair:
    """組 (子の時刻, 親の時刻) → 窓の組（x = 子の時計、y = 親の時計）。配線の遅れ（使う側が明示）は子が辺を見るのを遅らせる:
    子の辺の瞬間 = 親の辺 + 配線。"""
    return pair((local - link.rx, local - link.rx + link.span), parent + link.tx + cable_delay_ticks)


@dataclass(frozen=True)
class ParentTime:
    """区間の組全部を通る直線の範囲（傾き = 親の刻み / 子の刻み）。"""
    map: ClockMap
    pairs: int
    link: SyncLink

    @property
    def slope(self) -> tuple[float, float] | None:
        s = self.map.slope_range()
        return None if s is None else (float(s[0]), float(s[1]))

    @property
    def feasible(self) -> bool | None:
        return self.map.feasible


def fit_parent(pairs: list[tuple[int, int]], link: SyncLink = DEFAULT_LINK, cable_delay_ticks: int = 0) -> ParentTime | None:
    """組 (子の刻み, 親の刻み) の全部を通る直線の範囲。子の時刻が 1 種類以下（傾きが決まらない）なら None。"""
    if len({local for local, _ in pairs}) < 2:
        return None
    m = ClockMap([edge_pair(local, parent, link, cable_delay_ticks) for local, parent in sorted(pairs)])
    return ParentTime(m, len(pairs), link)


def _window(result: dict, local: int, pair_locals: Sequence[int], link: SyncLink, assumption: str) -> dict:
    lo, hi = result["t_lo"], result["t_hi"]
    return {"t_lo": None if lo is None else float(lo), "t_hi": None if hi is None else float(hi),
            "t": None if lo is None else float((lo + hi) / 2),        # 置き場所（範囲の中点。推定ではない）
            "phase_span_ticks": link.span, "link": link.source,
            "nearest_pair_ticks": min((abs(local - p) for p in pair_locals), default=None),
            "inside": result.get("inside"), "assumption": assumption, "unknown": list(result["unknown"]),
            "cable_delay": None}


def to_parent(fit: ParentTime, local: int, pair_locals: Sequence[int]) -> dict:
    """子の時刻 local（その値になる子の時計の辺）を親の刻みの範囲に（区間の全部の組を通る直線の範囲）。返す: t_lo・t_hi
    （両端を含む）、t（中点）、phase_span_ticks、link、nearest_pair_ticks、inside（組の範囲の中か）、assumption、unknown、
    cable_delay（None = 測っていない）。仮定が破れた・傾きが決まらないなら t_lo・t_hi は None で unknown に理由。"""
    return _window(fit.map.to_probe(local), local, pair_locals, fit.link, ASSUMPTION)


def segments(pairs: list[tuple[int, int]], gap_ticks: int | None) -> list[list[tuple[int, int]]]:
    """子の時刻の順の組を、間が gap_ticks（使う側が明示。None なら分けない）を超える所で区間に分ける（線が抜けた等）。"""
    out: list[list[tuple[int, int]]] = []
    for p in sorted(pairs):
        if out and (gap_ticks is None or p[0] - out[-1][-1][0] <= gap_ticks):
            out[-1].append(p)
        else:
            out.append([p])
    return out


def neighbour_converter(pairs: list[tuple[int, int]], link: SyncLink = DEFAULT_LINK,
                        cable_delay_ticks: int = 0) -> Callable[[int], dict]:
    """両側の 2 組だけを通る直線の範囲で子の時刻を親の刻みに（仮定は「隣り合う 2 組の間で比が一定」だけ。2 枚の時計の速さの
    比が記録の間に変わっても（温度等）、区間の全部の組を 1 本の直線に通す fit_parent より弱い仮定で済む）。組の範囲の外は、
    端の 2 組の直線の範囲を延ばす（inside = False。外を使うかは呼ぶ側が決める）。子の時刻が違う組が 2 つ以上要る。
    同じ子の時刻の組は、窓が同じなので 1 つにする（親の時刻が違えば、両方の窓を通す直線が無いことがある: 隣の組の範囲で返す）。"""
    by_local: dict[int, int] = {}
    for local, parent in sorted(pairs):
        by_local.setdefault(local, parent)
    items = sorted(by_local.items())
    if len(items) < 2:
        raise ValueError("換算には子の時刻が違う組が 2 つ以上要る")
    xs = [local for local, _ in items]
    cache: dict[int, ClockMap] = {}

    def at(local: int) -> dict:
        i = min(max(bisect.bisect_right(xs, local) - 1, 0), len(items) - 2)
        m = cache.get(i)
        if m is None:
            m = cache[i] = ClockMap([edge_pair(*items[i], link, cable_delay_ticks),
                                     edge_pair(*items[i + 1], link, cable_delay_ticks)])
        r = _window(m.to_probe(local), local, (xs[i], xs[i + 1]), link, NEIGHBOUR_ASSUMPTION)
        r["inside"] = xs[0] <= local <= xs[-1]
        return r
    return at

