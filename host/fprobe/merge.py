"""2 つの記録（親・子のボード）を、子の記録の同期の組（stream sync）で親の時間軸 1 本に合成する
（docs/superpowers/specs/2026-10-22-modular-design.md §3.3）。

新しい記録: 親の出来事・波形はそのまま。子の出来事は stream を "<子の名前>.<stream>" にし、時刻を親の刻みに換算して、
uncertainty に換算の不確かさ（組の量子化 1 刻み。換算は両側の組の間の直線 = 内挿で、2 枚の時計の速さの比の
揺れを組ごとに追う）、body に最も近い組までの子の刻み
（sync_nearest_pair_ticks。内挿の距離）を入れる。組の区間の外（終わりの時刻が外に出るものも）は推定せず入れずに数える。
子の波形は、記録の波形のかたまりにボードの区別が無いので、合成の記録の下のボードごとの記録 boards/<子の名前> に
親の時刻に換算して入れる（かたまり全体が組の区間の中のものだけ。外は数える）。
配線の遅れは測っていないので、使う側が渡したときだけ足す（meta と結果に足したかを書く）。
"""

import bisect
import dataclasses
from pathlib import Path

from . import generated as g
from .recording import Recorder, Recording
from .timesync import ParentTime, fit_parent, interpolator, segments

__all__ = ["merge"]

QUANTIZATION_TICKS = 1
NEAREST_KEY = "sync_nearest_pair_ticks"


def merge(parent: Path, child: Path, out: Path, child_name: str, cable_delay_ticks: int | None = None,
          gap_ticks: int | None = None) -> dict:
    p, c = Recording.open(parent), Recording.open(child)
    pairs = sorted((e.t, e.body["parent"]) for e in c.events(streams=["sync"]) if e.kind == "pair")
    fits: list[tuple[int, int, ParentTime]] = []                  # (区間の最初の子の時刻, 最後, 直線)
    interps = {}                                                   # id(直線) → その区間の内挿
    for seg in segments(pairs, gap_ticks):
        fit = fit_parent(seg)
        if fit is not None:
            fits.append((seg[0][0], seg[-1][0], fit))
            interps[id(fit)] = interpolator(seg)
    locals_ = [local for local, _ in pairs]
    delay = cable_delay_ticks or 0

    def segment_of(t: int) -> ParentTime | None:
        for first, last, fit in fits:
            if first <= t <= last:
                return fit
        return None

    def at(fit: ParentTime, t: int) -> int:
        return round(interps[id(fit)](t)) + delay

    def nearest(t: int) -> int:
        k = bisect.bisect_left(locals_, t)
        return min(abs(t - locals_[j]) for j in (k - 1, k) if 0 <= j < len(locals_))

    gaps = [(b[0] - a[0], a[1] + g.SYNC_TX_LATENCY_TICKS) for a, b in zip(pairs, pairs[1:])]
    gap_max, gap_at = max(gaps, default=(None, None))
    hz = p.meta["timebase_hz"]
    rec = Recorder(out, timebase_hz=hz, meta={**p.meta, "merged": {"parent": str(parent), "children": [{
        "name": child_name, "source": str(child), "pairs": len(pairs), "segments": len(fits),
        "waveforms": f"boards/{child_name}",
        "cable_delay_ticks": cable_delay_ticks, "cable_delay_included": cable_delay_ticks is not None}]}})
    rec.add_many((e.stream, e.kind, e.t, e.a, e.b, e.body, e.t_end, e.uncertainty) for e in p.iter_events())
    for t0, _, tr in p.waveforms():
        rec.add_waveform(tr, t0)
    rows, outside = [], 0
    for e in c.iter_events():
        fit = segment_of(e.t)
        if fit is None or (e.t_end is not None and segment_of(e.t_end) is not fit):
            outside += 1
            continue
        distance = nearest(e.t)
        body = e.body
        if body is None:
            body = {NEAREST_KEY: distance}
        elif isinstance(body, dict):
            body = {**body, NEAREST_KEY: distance}
        rows.append((f"{child_name}.{e.stream}", e.kind, at(fit, e.t), e.a, e.b, body,
                     None if e.t_end is None else at(fit, e.t_end),
                     QUANTIZATION_TICKS + (e.uncertainty or 0)))
    rec.add_many(rows)
    board = Recorder(out / "boards" / child_name, timebase_hz=hz, meta={"board": child_name, "source": str(child)})
    waveforms = waveforms_outside = 0
    for t0, tps, tr in c.waveforms():
        fit = segment_of(t0)
        if fit is None or segment_of(t0 + round(tr.length * tps)) is not fit:
            waveforms_outside += 1
            continue
        t_end = t0 + round(tr.length * tps)                       # 1 サンプルの刻みは、そのかたまりの両端の換算から
        span = interps[id(fit)](t_end) - interps[id(fit)](t0)
        board.add_waveform(dataclasses.replace(tr, sample_period_s=(span / tr.length if tr.length else tps * fit.ratio) / hz),
                           at(fit, t0))
        waveforms += 1
    board.close()
    rec.close()
    p.close()
    c.close()
    return {"pairs": len(pairs), "converted": len(rows), "outside": outside, "segments": len(fits),
            "pair_gap_max_ticks": gap_max, "pair_gap_max_at": gap_at,       # 子の刻み、その前の組の親の時刻
            "child_waveforms": waveforms, "child_waveforms_outside": waveforms_outside,
            "cable_delay_included": cable_delay_ticks is not None,
            "child_sync": c.meta.get("sync"),                          # 子の CSR sync の数（CRC 異常）。無ければ None
            "conversion": "neighbouring_pairs",                     # 両側の組の間の直線（内挿）
            "uncertainty": {"quantization_ticks": QUANTIZATION_TICKS,
                            "residual_max_ticks": max((f.residual_max for _, _, f in fits), default=None)}}
