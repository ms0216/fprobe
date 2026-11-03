"""2 つの記録（親・子のボード）を、子の記録の同期の組（stream sync）で親の時間軸 1 本に合成する
（docs/superpowers/specs/2026-10-22-modular-design.md §3.3、time-accuracy 設計書 §13 の B4）。

新しい記録: 親の出来事・波形はそのまま。子の出来事は stream を "<子の名前>.<stream>" にし、時刻を親の刻みに換算する。換算は
両側の 2 組の窓を通る直線の範囲（fprobe.timesync.neighbour_converter。仮定は「隣り合う組の間で 2 枚の時計の比が一定」だけ）で、
範囲 [t_lo, t_hi] は組の窓（子が辺を取り込む位相の幅 = 申告の sync_rx の span）と比の不確かさを全部含む（段階 S3 の前は、手で
書いた量子化 1 刻み + 子の不確かさだけで、位相の幅・比の不確かさが入っていなかった）。行の時刻は範囲の中点を刻みに丸めた値
（置き場所の決まり。推定ではない）、uncertainty はその時刻から範囲の両端までの遠い方 + 子の不確かさ（範囲を覆う ±）、body に
範囲（sync_window_ticks）と最も近い組までの子の刻み（sync_nearest_pair_ticks）。組の区間の外（終わりの時刻が外に出るものも）は
入れずに数える。子の波形は、記録の波形のかたまりにボードの区別が無いので、合成の記録の下のボードごとの記録 boards/<子の名前> に
親の時刻に換算して入れる（かたまり全体が組の区間の中のものだけ。外は数える）。
組の窓の段数（RX・位相の幅・TX）は記録の meta の timing（回路の申告）から取り、無い古い記録は生成した表の値（結果の link）。
配線の遅れは測っていないので、使う側が渡したときだけ足す（meta と結果に足したかを書く）。
"""

import bisect
import dataclasses
import math
from pathlib import Path

from .recording import Recorder, Recording
from .timesync import NEIGHBOUR_ASSUMPTION, link_from_timing, neighbour_converter, segments

__all__ = ["merge"]

NEAREST_KEY = "sync_nearest_pair_ticks"
WINDOW_KEY = "sync_window_ticks"


def merge(parent: Path, child: Path, out: Path, child_name: str, cable_delay_ticks: int | None = None,
          gap_ticks: int | None = None) -> dict:
    p, c = Recording.open(parent), Recording.open(child)
    link = link_from_timing(c.meta.get("timing"), p.meta.get("timing"))
    pairs = sorted((e.t, e.body["parent"]) for e in c.events(streams=["sync"]) if e.kind == "pair")
    delay = cable_delay_ticks or 0
    segs: list[tuple[int, int, object]] = []                       # (区間の最初の子の時刻, 最後, 換算)
    for seg in segments(pairs, gap_ticks):
        if len({local for local, _ in seg}) >= 2:
            segs.append((seg[0][0], seg[-1][0], neighbour_converter(seg, link, delay)))
    locals_ = [local for local, _ in pairs]

    def segment_of(t: int):
        for first, last, conv in segs:
            if first <= t <= last:
                return conv
        return None

    def nearest(t: int) -> int:
        k = bisect.bisect_left(locals_, t)
        return min(abs(t - locals_[j]) for j in (k - 1, k) if 0 <= j < len(locals_))

    widths: list[float] = []
    unmapped = 0

    def at(conv, t: int) -> tuple[int, float, list[float]] | None:
        """子の時刻 t → (行の時刻, その時刻から範囲の端までの遠い方, [t_lo, t_hi])。範囲が出なければ None。"""
        r = conv(t)
        if r["t_lo"] is None:
            return None
        row = math.floor((r["t_lo"] + r["t_hi"]) / 2 + 0.5)
        return row, max(row - r["t_lo"], r["t_hi"] - row), [r["t_lo"], r["t_hi"]]

    hz = p.meta["timebase_hz"]
    rec = Recorder(out, timebase_hz=hz, meta={**p.meta, "merged": {"parent": str(parent), "children": [{
        "name": child_name, "source": str(child), "pairs": len(pairs), "segments": len(segs),
        "waveforms": f"boards/{child_name}", "link": dataclasses.asdict(link),
        "cable_delay_ticks": cable_delay_ticks, "cable_delay_included": cable_delay_ticks is not None}]}})
    rec.add_many((e.stream, e.kind, e.t, e.a, e.b, e.body, e.t_end, e.uncertainty) for e in p.iter_events())
    for t0, _, tr in p.waveforms():
        rec.add_waveform(tr, t0)
    rows, outside = [], 0
    for e in c.iter_events():
        conv = segment_of(e.t)
        if conv is None or (e.t_end is not None and segment_of(e.t_end) is not conv):
            outside += 1
            continue
        start = at(conv, e.t)
        end = None if e.t_end is None else at(conv, e.t_end)
        if start is None or (e.t_end is not None and end is None):
            unmapped += 1
            continue
        t, half, window = start
        widths.append(window[1] - window[0])
        extra = {NEAREST_KEY: nearest(e.t), WINDOW_KEY: window}
        body = e.body
        if body is None:
            body = extra
        elif isinstance(body, dict):
            body = {**body, **extra}
        rows.append((f"{child_name}.{e.stream}", e.kind, t, e.a, e.b, body, None if end is None else end[0],
                     half + (e.uncertainty or 0)))
    rec.add_many(rows)
    board = Recorder(out / "boards" / child_name, timebase_hz=hz, meta={"board": child_name, "source": str(child)})
    waveforms = waveforms_outside = 0
    for t0, tps, tr in c.waveforms():
        t_end = t0 + round(tr.length * tps)
        conv = segment_of(t0)
        if conv is None or segment_of(t_end) is not conv:
            waveforms_outside += 1
            continue
        a, b = at(conv, t0), at(conv, t_end)
        if a is None or b is None:
            waveforms_outside += 1
            continue
        mid0 = (a[2][0] + a[2][1]) / 2                         # 1 サンプルの刻みは、そのかたまりの両端の範囲の中点から
        mid1 = (b[2][0] + b[2][1]) / 2
        period = (mid1 - mid0) / tr.length if tr.length else tps
        board.add_waveform(dataclasses.replace(tr, sample_period_s=period / hz), a[0])
        waveforms += 1
    board.close()
    rec.close()
    p.close()
    c.close()
    gaps = [(b[0] - a[0], a[1] + link.tx) for a, b in zip(pairs, pairs[1:])]
    gap_max, gap_at = max(gaps, default=(None, None))
    return {"pairs": len(pairs), "converted": len(rows), "outside": outside, "unmapped": unmapped, "segments": len(segs),
            "pair_gap_max_ticks": gap_max, "pair_gap_max_at": gap_at,       # 子の刻み、その前の組の親の時刻
            "child_waveforms": waveforms, "child_waveforms_outside": waveforms_outside,
            "cable_delay_included": cable_delay_ticks is not None,
            "child_sync": c.meta.get("sync"),                          # 子の CSR sync の数（CRC 異常）。無ければ None
            "conversion": "neighbouring_pairs",                     # 両側の 2 組の窓を通る直線の範囲
            "link": dataclasses.asdict(link),
            "uncertainty": {"assumption": NEIGHBOUR_ASSUMPTION, "phase_span_ticks": link.span,
                            "window_max_ticks": max(widths, default=None),
                            "rounding": "row_time_is_the_window_midpoint_rounded"}}
