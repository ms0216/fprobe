"""タスクの時間軸の解析（U09）: 優先度の逆転の区間（ミューテックスの持ち主・待ちの先頭の語から）。

土台（Intervals）は analysis_series.py。公開の名前は rtos.analysis から。
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from .analysis_series import Intervals, _dist, _dist_us, runs_by_task
from .timeline import Timeline, name_of

__all__ = ["MutexWatch", "inversion_episode", "inversion_episodes", "inversion_summary"]


@dataclass(frozen=True)
class MutexWatch:
    """サンプラで読むミューテックスの 2 語: 持ち主（値 = TCB）と待ちの先頭（値 − delta = 最も高い待ち手の TCB。
    値が empty か 0 なら待ち手なし）。"""
    name: str
    holder_word: int
    head_word: int
    delta: int
    empty: int


def inversion_episodes(tl: Timeline, samples: Iterable[tuple[int, int, int]], watches: Sequence[MutexWatch],
                       priorities: Mapping[int, int], order: int, names: Mapping[int, str] | None = None,
                       hz: float | None = None) -> dict:
    """持ち主の元の優先度より高い待ち手がいる区間（優先度の逆転）。その間に動いた、持ち主と待ち手の間の優先度の
    タスクの時間（interference。interfered はそれが正か）と、持ち主が動いた時間。
    サンプラの周期より短い逆転は見えないことがある（回数は下限、端は ± 周期）。"""
    if not watches:
        return {"available": False, "episodes": [], "count": 0,
                "reason": "ミューテックスの語を読んでいない（rtos_timeline_start の mutexes）。トレースフックの出来事"
                          "（U10）も無いので、逆転は推測しない"}
    names = dict(names or {})
    names.update({r.handle: r.task for r in tl.runs})
    per = runs_by_task(tl)
    edges: dict[int, list] = {}             # タスクごとの区間（端の不確かさ = 実際のサンプルの間隔の半分つき）
    for r in tl.runs:
        edges.setdefault(r.handle, []).append(r)

    def urgency(h: int | None) -> int | None:
        p = priorities.get(h) if h is not None else None
        return None if p is None else p * order
    episodes = []
    rows = sorted(samples)
    for w in watches:
        holder = head = None
        holder_t = head_t = None                                # それぞれの語を最後に読んだ時刻
        opened: tuple[int, int, int] | None = None              # (始め, 持ち主, 待ち手)
        seen = 0                                                # 逆転を最後に見たサンプルの時刻
        reads: dict = {}                                        # 開いた区間の、組にした読みの時刻の事実

        def close(t1: int, **kw) -> None:
            ep = inversion_episode(w.name, opened, t1, per, urgency, names, certain=(opened[0], seen), edges=edges,
                                   **kw)
            # 2 つの語は別々の時刻に読んだ（同時の状態とは限らない）: 組にした読みの時刻の差の最大と、開いた読み
            ep.update(reads)
            if hz:
                ep["pair_read_span_us"] = ep["pair_read_span"] * 1e6 / hz
            episodes.append(ep)
        for t, a, v in rows:
            if a == w.holder_word:
                holder, holder_t = v or None, t
            elif a == w.head_word:
                head, head_t = (None if v in (w.empty, 0) else v - w.delta), t
            else:
                continue
            hu, wu = urgency(holder), urgency(head)
            inverted = hu is not None and wu is not None and holder != head and wu > hu
            key = (holder, head)
            if opened is not None and (not inverted or (opened[1], opened[2]) != key):
                close(t)
                opened = None
            if inverted and opened is None:
                opened = (t, holder, head)
                reads = {"opened_reads": {"holder_t": holder_t, "head_t": head_t}, "pair_read_span": 0}
            if inverted:
                seen = t
                reads["pair_read_span"] = max(reads["pair_read_span"], abs(holder_t - head_t))
        if opened is not None:
            close(tl.t1, truncated=True)
    out = inversion_summary(episodes, hz)
    out["notes"] = ["サンプラの周期より短い逆転は見えないことがある（回数は下限、区間の端は ± 周期）"]
    return out


def _overlap(a0: int, a1: int, b0: int, b1: int) -> int:
    return max(0, min(a1, b1) - max(a0, b0))


def inversion_summary(episodes: list[dict], hz: float | None = None) -> dict:
    """逆転の区間の列の要約（回数・中間の優先度のタスクが動いた回数 interfered・合計・長さの分布・最長）。"""
    episodes.sort(key=lambda e: e["t0"])
    durations = [e["duration"] for e in episodes]
    out = {"available": True, "count": len(episodes), "interfered": sum(1 for e in episodes if e["interfered"]),
           "total": sum(durations), "duration": _dist(durations), "episodes": episodes,
           "max": max(episodes, key=lambda e: e["duration"]) if episodes else None}
    if hz:
        out["duration_us"] = _dist_us(out["duration"], hz)
    return out


def inversion_episode(mutex: str, opened: tuple[int, int, int], t1: int, per: dict[int, Intervals], urgency, names,
             truncated: bool = False, certain: tuple[int, int] | None = None,
             edges: dict[int, list] | None = None) -> dict:
    """逆転の 1 区間: 持ち主・待ち手、その間に動いた中間の優先度のタスクの時間（interference）、持ち主が動いた時間。
    certain: サンプルで見た区間のとき、逆転を見ていた範囲（初めて見た時刻〜最後に見た時刻）。interference はその中で
    数える（外では、逆転の前後に中間のタスクが動いたのかもしれない）。edges: タスクごとの区間（Run。端の不確かさ
    uncertainty = その切り替えの前後のサンプルの間隔の半分）。あれば各区間を、範囲をその区間の不確かさだけ内側に
    縮めて数える（区間の端が範囲の端の外にずれているかもしれない分。間隔の中央値で一律に縮めない）。"""
    t0, holder, waiter = opened
    hu, wu = urgency(holder), urgency(waiter)
    interference = {}
    for h, iv in per.items():
        u = urgency(h)
        if h not in (holder, waiter) and u is not None and hu < u < wu:
            a, b = certain or (t0, t1)
            if edges is not None and certain is not None:
                # 両端とも観測の端・抜けで切った区間（uncertainty None）の端はサンプルの時刻そのもので、切り替えの
                # 時刻の幅は無い（縮めない）
                spent = sum(_overlap(r.t0, r.t1, a + math.ceil(r.uncertainty or 0), b - math.ceil(r.uncertainty or 0))
                            for r in edges.get(h, ()))
            else:
                spent = iv.overlap(a, b) if a < b else 0
            if spent:
                interference[name_of(names, h)] = spent
    holder_time = per[holder].overlap(t0, t1) if holder in per else 0
    out = {"mutex": mutex, "holder": name_of(names, holder), "waiter": name_of(names, waiter), "t0": t0, "t1": t1,
           "duration": t1 - t0, "holder_time": holder_time, "interference": interference,
           "interfered": bool(interference)}
    if truncated:
        out["truncated"] = True
    return out
