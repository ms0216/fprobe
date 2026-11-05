"""変数の値ごとの続いた時間（durations）と、始まりから終わりまでの取引の集計（transactions）。記録の読みだけで、
回数・時間・分布・欠けを事実として返す（判定はしない）。下請けは correlate_support。"""

import statistics
from bisect import bisect_left
from typing import Any

from .correlate_cond import ConditionError, parse_condition
from .correlate_support import _gaps, _holes, _instants, _overlaps, _range
from .correlate_vars import _Variables


def durations(rec: Any, name: str, t0: int | None = None, t1: int | None = None, limit: int = 20,
              gap_s: float | None = None) -> dict:
    """変数 name の値ごとの回数・続いた時間（両端の見えている区間だけ。秒の最小・中央値・90 % 点・最大）・範囲の中で
    その値だった時間の割合（share。最初の値を見てから範囲の終わりまでのうち）。値は列挙の名前か数の文字。続いた時間の
    合計の大きい順に limit 個（値の総数は values_total、切ったかは truncated、使った limit は limit）。
    例: FPGA の state が BUSY のまま続く時間と、MCU の待ち時間を比べる。"""
    lo, hi = _range(rec, t0, t1)
    hz = rec.meta["timebase_hz"]
    vars_ = _Variables(rec, lo, hi, gap_s=gap_s)
    series = vars_.series(name)
    holes = _holes(vars_, [name])
    label = {v: k for k, v in (series.enum or {}).items()}
    spans: dict[int, list[float]] = {}
    held: dict[int, int] = {}
    excluded = 0
    ends = series.times[1:] + [hi]
    for i, (t, v, end) in enumerate(zip(series.times, series.values, ends)):
        hidden = _overlaps(holes, t - 1, end)                # 欠けにかかる・欠けの直後に見えた: いつ変わったか分からない
        held[v] = held.get(v, 0) + (0 if hidden else end - t)
        if 0 < i < len(series.times) - 1:                    # 両端の見えている区間
            if hidden:
                excluded += 1
            else:
                spans.setdefault(v, []).append((end - t) / hz)
    observed = sum(held.values())                            # 欠けにかからない時間（割合の分母）
    for v in (series.enum or {}).values():                   # 定義にあるが出なかった列挙の値も 0 で（出なかったと分かる）
        held.setdefault(v, 0)
    ranked = sorted(held, key=lambda v: -held[v])[:limit]
    return {"variable": name, "range": [lo, hi], "changes": max(0, len(series.times) - 1), "excluded": excluded,
            "gaps": _gaps(rec, vars_, {name}, lo, hi),
            "values_total": len(held), "truncated": len(ranked) < len(held), "limit": limit,
            "values": {label.get(v, str(v)): {"count": len(spans.get(v, [])), "share": held[v] / observed if observed else None,
                                              "duration_s": _quantiles(spans.get(v, []))} for v in ranked}}


def transactions(rec: Any, start: str, by: str | None = None, counts: list[str] | None = None,
                 effect: str | None = None, t0: int | None = None, t1: int | None = None, rows: int = 20,
                 by_at: str = "start", effect_lag_s: float = 0.0, gap_s: float | None = None) -> dict:
    """記録を区間（リクエスト等の 1 回の取引）に切って数える: 区間 = start（瞬間の項のある条件）が成り立ってから次に
    成り立つまで（最後は範囲の終わりまで）。区間ごとに by（変数）の開始時の値・counts の各条件が区間の中で成り立った
    回数・effect が区間の中で起きたか（by_at = "end" なら by は区間の終わり = 次の始めの直前の値。ファームウェアが要求の
    最後に書く値で分けるとき）。by の値ごとに区間の数・effect の割合・counts の平均（groups。by の値の順）、
    最初の rows 区間の中身（rows。欠けで除いた区間は入れない。入れうる区間の数は rows_total、切ったかは rows_truncated、
    使った rows は rows_limit）。例: start "state changed and state=BUSY"、by "left_hi"、counts ["restart changed
    and state=BUSY"]、effect "errors changed" → B が大きい要求だけ再試行され、再試行された要求だけ誤る、が 1 回で分かる。"""
    if by_at not in ("start", "end"):
        raise ValueError("by_at は start（区間の始めの値）か end（区間の終わり = 次の始めの直前の値）")
    lo, hi = _range(rec, t0, t1)
    counts = list(counts or [])
    start_cond = parse_condition(start)
    conds = [parse_condition(c) for c in counts]
    effect_cond = parse_condition(effect) if effect else None
    vars_ = _Variables(rec, lo, hi, gap_s=gap_s)
    starts = [t for t in _instants(start_cond, vars_) if lo <= t < hi]
    if not starts:
        raise ConditionError(f"区間の始め {start!r} が範囲の中で一度も成り立たない")
    ends = starts[1:] + [hi]
    hits = [_instants(c, vars_) for c in conds]
    hz = rec.meta["timebase_hz"]
    lag = round(effect_lag_s * hz)                           # 結果を lag だけ前の時刻で区間に割り当てる
    effects = [t - lag for t in _instants(effect_cond, vars_)] if effect_cond else []
    series = vars_.series(by) if by else None
    label = {v: k for k, v in ((series.enum or {}) if series else {}).items()}

    def inside(times: list[int], a: int, b: int) -> int:
        return bisect_left(times, b) - bisect_left(times, a)

    names = {t.name for c in [start_cond, *conds, *([effect_cond] if effect_cond else [])] for t in c.terms} | ({by} if by else set())
    holes = _holes(vars_, sorted(names))
    by_var = {n: _holes(vars_, [n]) for n in sorted(names)}
    excluded_by: dict[str, int] = {}
    out_rows, groups, excluded, listed = [], {}, 0, 0
    offsets, lengths = [], []                                # 結果の始めからの時間・区間の長さ（秒）
    split = {True: [0, [0] * len(conds)], False: [0, [0] * len(conds)]}   # 結果が起きた区間・起きなかった区間
    for a, b in zip(starts, ends):
        if _overlaps(holes, a - 1, b):                       # 欠けにかかる・欠けの直後に見えた始まり: 回数・結果が不確か
            excluded += 1
            for n, h in by_var.items():
                if _overlaps(h, a - 1, b):
                    excluded_by[n] = excluded_by.get(n, 0) + 1
            continue
        value = (series.at(a) if by_at == "start" else series.at(b - 1)) if series else None
        key = None if value is None else label.get(value, str(value))
        n = [inside(h, a, b) for h in hits]
        hit = inside(effects, a, b) > 0 if effect_cond else None
        lengths.append((b - a) / hz)
        if hit:
            offsets.append((effects[bisect_left(effects, a)] - a) / hz)
        listed += 1
        if len(out_rows) < rows:
            out_rows.append({"t": a, "end": b, "by": key, "counts": n, "effect": hit})
        g = groups.setdefault(value, {"by": key, "count": 0, "effects": 0, "sums": [0] * len(n)})
        g["count"] += 1
        g["effects"] += bool(hit)
        g["sums"] = [x + y for x, y in zip(g["sums"], n)]
        if effect_cond:
            split[hit][0] += 1
            split[hit][1] = [x + y for x, y in zip(split[hit][1], n)]
    out_groups = []
    for value in sorted(groups, key=lambda v: (v is None, v if v is not None else 0)):
        g = groups[value]
        out_groups.append({"by": g["by"], "count": g["count"],
                           "effect_rate": g["effects"] / g["count"] if effect_cond else None,
                           "counts": {c: x / g["count"] for c, x in zip(counts, g["sums"])}})
    return {"start": start_cond.text, "by": by, "effect": effect_cond.text if effect_cond else None,
            "range": [lo, hi], "timebase_hz": hz, "transactions": len(starts), "excluded": excluded,
            "excluded_by": excluded_by, "length_s": _quantiles(lengths),
            "effect_offset_s": _quantiles(offsets) if effect_cond else None, "effect_lag_s": effect_lag_s,
            "warnings": [f"データの欠けにかかる {excluded} 区間を除いた（"
                         + ", ".join(f"{n} {k}" for n, k in excluded_by.items()) + "。excluded_by・gaps）"] if excluded else [],
            "gaps": _gaps(rec, vars_, names, lo, hi),
            "groups": out_groups, "rows": out_rows,
            "rows_total": listed, "rows_truncated": len(out_rows) < listed, "rows_limit": rows,
            "by_effect": {k: {"count": c, "counts": {name: x / c if c else None for name, x in zip(counts, sums)}}
                          for k, (c, sums) in (("with", split[True]), ("without", split[False]))} if effect_cond else None}


def _quantiles(xs: list[float]) -> dict | None:
    if not xs:
        return None
    s = sorted(xs)
    return {"min": s[0], "median": statistics.median(s), "p90": s[min(len(s) - 1, int(0.9 * len(s)))], "max": s[-1]}
