"""相関の分析の下請け（correlate / correlate_durations が共有する）: 条件が成り立った時刻（_instants・_holding）、
範囲の解決（_range）、値の取れていない区間（_holes・_overlaps・_gaps）。記録の読みだけで、判定はしない。"""

from bisect import bisect_left
from typing import Any

from .correlate_cond import EDGES, Condition, ConditionError
from .correlate_vars import _Series, _Variables, _target
from .la_wave import _np

UNSURE_REASONS = ("pin_skew_unknown",)   # gaps のうち値の欠けではないもの（値はあり、時刻の一部が分からない）


def _instants(cond: Condition, vars_: _Variables) -> list[int]:
    """条件が成り立った時刻（瞬間の項のどれかが起きた時刻で、ほかの項も満たすもの）。"""
    edges = [(t, vars_.series(t.name)) for t in cond.terms if t.op in EDGES]
    states = [(vars_.series(t.name), _target(vars_.series(t.name), t.value, t.name)) for t in cond.terms if t.op == "="]
    lists = sorted((s.instants(t.op) for t, s in edges), key=len)
    first, others = lists[0], [set(x) for x in lists[1:]]            # 瞬間の項が 1 つなら集合を作らない
    if others:
        first = [t for t in first if all(t in st for st in others)]
    return _holding(first, states) if states else first


_ARRAYS: dict[int, tuple[list[int], object]] = {}


def _array_of(values: list[int]):
    """瞬間の並び（_Series.instants の覚えたリスト）の numpy の配列。同じリストは一度だけ作る（直近の数個だけ覚える）。"""
    key = id(values)
    hit = _ARRAYS.get(key)
    if hit is not None and hit[0] is values:
        return hit[1]
    if len(_ARRAYS) > 8:
        _ARRAYS.clear()
    arr = _np.asarray(values, dtype=_np.int64)
    _ARRAYS[key] = (values, arr)
    return arr


def _holding(instants: list[int], states: list[tuple["_Series", int]]) -> list[int]:
    """時刻順の instants のうち、その時刻の値がどの (並び, 値) とも一致するもの（並びの最後の変化 ≤ t の値）。
    瞬間が数百万でも（ピンのクロック等）、1 つずつ二分探索しない: numpy があればまとめて、無ければ 1 回の走査で。"""
    if _np is not None:
        arr = _array_of(instants)
        keep = _np.ones(len(arr), dtype=bool)
        for series, value in states:
            st, vals = series.arrays()
            if not len(st):                              # 値を 1 件も持たない変数: どの時刻も一致しない（numpy 無しと同じ）
                keep[:] = False
                continue
            idx = _np.searchsorted(st, arr, side="right") - 1
            valid = idx >= 0
            keep &= valid & (vals[_np.where(valid, idx, 0)] == value)
        return arr[keep].tolist()
    out = []
    pos = [-1] * len(states)
    for t in instants:
        ok = True
        for k, (series, value) in enumerate(states):
            j, times = pos[k], series.times
            while j + 1 < len(times) and times[j + 1] <= t:
                j += 1
            pos[k] = j
            if j < 0 or series.values[j] != value:
                ok = False
        if ok:
            out.append(t)
    return out


def _range(rec: Any, t0: int | None, t1: int | None) -> tuple[int, int]:
    span = rec.span()
    if span is None:
        raise ConditionError("記録が空")
    return (span[0] if t0 is None else max(span[0], t0)), (span[1] + 1 if t1 is None else min(span[1] + 1, t1))


def _holes(vars_: "_Variables", names: list[str]) -> list[tuple[int, int]]:
    """変数の値が無い区間（読みの空白・ピンの無い区間）を重なりをまとめて時刻順に。"""
    spans = sorted((g["t0"], g["t1"]) for n in names for g in vars_.series(n).gaps if g["t1"] > g["t0"])
    out: list[tuple[int, int]] = []
    for a, b in spans:
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def _overlaps(holes: list[tuple[int, int]], a: float, b: float) -> bool:
    i = bisect_left(holes, (b,)) - 1                          # b より前に始まる最後の欠け
    return i >= 0 and holes[i][1] > a


def _gaps(rec: Any, vars_: "_Variables", names: set[str], lo: int, hi: int) -> list[dict]:
    """使った変数の欠け（MCU の読みの空白・読めなかった語・ピンの無い区間・FPGA の欠落）と、記録の取り込みの失敗・
    長期の記録の間引き。時刻順。黙って数え落とさないため（reason は画面で訳す）。値はあるが時刻の一部が分からない区間
    （pin_skew_unknown: ピンの ch のずれが分からずずらさずに置いた）も時刻を示す（結果からは外さない）。"""
    out, fpga = [], False
    for n in sorted(names):
        kind = vars_.resolve(n)[0]
        fpga |= kind == "fpga"
        series = vars_.series(n)
        out += [{**g, "variable": n} for g in [*series.gaps, *series.unsure]]
    if fpga:
        out += vars_.fpga_lost
    out += [{"reason": "events_error", "t0": e.t, "t1": e.t, "error": (e.body or {}).get("error")}
            for e in rec.iter_events(lo, hi, ["annotation"], kinds=["events_error"])]
    out += [{"reason": "recording_dropped", "t0": max(lo, g["t0"]), "t1": min(hi, g["t1"])}
            for g in rec.coverage(lo, hi)["gaps"]]
    return sorted(out, key=lambda g: (g["t0"], g["reason"]))
