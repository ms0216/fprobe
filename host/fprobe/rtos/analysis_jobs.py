"""タスクの時間軸の解析（U09）: ジョブ（出来事で区切る）・タスクの区間の要約・横取りを除いた実行時間。

土台（Intervals・窓ごとの負荷）は analysis_series.py。公開の名前は rtos.analysis から。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .analysis_series import BLOCK_KINDS, Intervals, _dist, _dist_us, runs_by_task
from .timeline import Timeline, name_of

__all__ = ["Job", "KernelEvent", "find_jobs", "job_summary", "run_summary", "run_time_in"]


# --- ジョブ -------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class KernelEvent:
    """カーネルの出来事（U10 のトレースフック）: ready（実行可能になった）/ block・delay・suspend・delete（でなくなった）。"""
    t: int
    kind: str
    handle: int


@dataclass
class Job:
    """タスクの 1 回の仕事: 起きて（release）・動き始め（start）・待つまで（end）。exec_time は横取り・割り込みを除く。
    end_reason: blocked（待った）/ truncated（抜け・観測の
    端で終わりが分からない。統計に入れない）。"""
    handle: int
    task: str
    start: int
    end: int
    exec_time: int
    preemptions: int
    end_reason: str
    release: int | None = None

    @property
    def wall(self) -> int:
        return self.end - self.start

    @property
    def response(self) -> int | None:
        return None if self.release is None else self.end - self.release

    def to_dict(self) -> dict:
        out = {"task": self.task, "handle": self.handle, "start": self.start, "end": self.end, "exec": self.exec_time,
               "wall": self.wall, "preemptions": self.preemptions, "end_reason": self.end_reason}
        if self.release is not None:
            out.update(release=self.release, response=self.response)
        return out


def find_jobs(tl: Timeline, idle: Iterable[int] = (), isr: Iterable[tuple[int, int]] = (),
              events: Iterable[KernelEvent] | None = None) -> list[Job]:
    """カーネルの出来事（ready / block）でタスクの区間をジョブにまとめる（応答時間も分かる）。出来事の無いタスクの
    ジョブは作らない（次に動いたタスクの優先度から横取りか待ちかを推定しない。区間の事実は run_summary）。"""
    idle = set(idle)
    isr_iv = Intervals(isr)
    runs = sorted(tl.runs, key=lambda r: r.t0)
    by_task: dict[int, list[KernelEvent]] = {}
    for e in events or ():
        by_task.setdefault(e.handle, []).append(e)
    jobs: list[Job] = []
    for handle in {r.handle for r in runs} - idle:
        if handle in by_task:
            jobs += _jobs_from_events(handle, runs, sorted(by_task[handle], key=lambda e: e.t), isr_iv, tl)
    return sorted(jobs, key=lambda j: (j.start, j.task))


def run_summary(tl: Timeline, idle: Iterable[int] = (), isr: Iterable[tuple[int, int]] = (),
                hz: float | None = None) -> dict[str, dict]:
    """タスクごとの区間（続けて動いた間）の事実: 回数・自分の時間（割り込みを除く）・周期（始めの間隔）の分布、最長の回、
    次に動いたタスクの回数（followed_by。抜けで切れたら "(gap)"、観測の端なら "(end)"）。なぜ切り替わったかは推定しない。"""
    idle = set(idle)
    isr_iv = Intervals(isr)
    gaps = sorted((g.t0, g.t1) for g in tl.gaps)
    runs = sorted(tl.runs, key=lambda r: r.t0)
    per: dict[int, list[tuple]] = {}
    for i, r in enumerate(runs):
        if r.handle in idle:
            continue
        nxt = runs[i + 1] if i + 1 < len(runs) else None
        if nxt is None:
            after = "(end)"
        elif any(r.t1 <= g0 <= nxt.t0 or g0 <= r.t1 < g1 for g0, g1 in gaps):
            after = "(gap)"
        else:
            after = nxt.task
        per.setdefault(r.handle, []).append((r, r.duration - isr_iv.overlap(r.t0, r.t1), after))
    out = {}
    for rows in per.values():
        task = rows[0][0].task
        starts = [r.t0 for r, _, _ in rows]
        followed: dict[str, int] = {}
        for _, _, after in rows:
            followed[after] = followed.get(after, 0) + 1
        worst, own, _ = max(rows, key=lambda x: x[1])
        row = {"handle": rows[0][0].handle, "runs": len(rows), "exec": _dist([o for _, o, _ in rows]),
               "period": _dist([b - a for a, b in zip(starts, starts[1:])]),
               "worst_exec": {"start": worst.t0, "end": worst.t1, "exec": own}, "followed_by": followed}
        for key in ("exec", "period"):
            if row[key] is not None and hz:
                row[f"{key}_us"] = _dist_us(row[key], hz)
        out[task] = row
    return out


def _jobs_from_events(handle: int, runs, events: list[KernelEvent], isr_iv: Intervals, tl: Timeline) -> list[Job]:
    mine = [r for r in runs if r.handle == handle]
    task = mine[0].task if mine else name_of({}, handle)
    out: list[Job] = []
    release: int | None = None
    for e in events:
        if e.kind == "ready" and release is None:
            release = e.t
        elif e.kind in BLOCK_KINDS and release is not None:
            inside = [r for r in mine if r.t1 > release and r.t0 < e.t]
            if inside:
                own = sum(min(r.t1, e.t) - max(r.t0, release) - isr_iv.overlap(max(r.t0, release), min(r.t1, e.t))
                          for r in inside)
                out.append(Job(handle, task, max(inside[0].t0, release), e.t, own, len(inside) - 1, "blocked", release))
            release = None
    if release is not None:
        inside = [r for r in mine if r.t1 > release]
        if inside:
            own = sum(r.duration for r in inside)
            out.append(Job(handle, task, max(inside[0].t0, release), inside[-1].t1, own, len(inside) - 1, "truncated",
                           release))
    return out


def job_summary(jobs: Iterable[Job], hz: float | None = None) -> dict[str, dict]:
    """タスクごと: 終わりの分かったジョブの数・実行時間・経過時間・周期（始めの間隔）・応答時間の分布と、最悪の回。"""
    per: dict[str, list[Job]] = {}
    for j in jobs:
        per.setdefault(j.task, []).append(j)
    out = {}
    for task, js in per.items():
        js.sort(key=lambda j: j.start)
        done = [j for j in js if j.end_reason != "truncated"]
        starts = [j.start for j in js]
        periods = [b - a for a, b in zip(starts, starts[1:])]
        responses = [j.response for j in done if j.response is not None]
        row: dict = {"handle": js[0].handle, "jobs": len(done),
                     "truncated": sum(1 for j in js if j.end_reason == "truncated"),
                     "preemptions": sum(j.preemptions for j in done),
                     "exec": _dist([j.exec_time for j in done]), "wall": _dist([j.wall for j in done]),
                     "period": _dist(periods), "response": _dist(responses) if responses else None}
        if done:
            worst = max(done, key=lambda j: j.exec_time)
            row["worst_exec"] = {"start": worst.start, "end": worst.end, "exec": worst.exec_time, "wall": worst.wall}
            longest = max(done, key=lambda j: j.wall)
            row["worst_wall"] = {"start": longest.start, "end": longest.end, "exec": longest.exec_time,
                                 "wall": longest.wall}
            if responses:
                slow = max((j for j in done if j.response is not None), key=lambda j: j.response)
                row["worst_response"] = {"release": slow.release, "end": slow.end, "response": slow.response}
        for key in ("exec", "wall", "period", "response"):
            if row[key] is not None and hz:
                row[f"{key}_us"] = _dist_us(row[key], hz)
        out[task] = row
    return out


# --- 横取りを除いた区間 ----------------------------------------------------------------------------------

def _running_at(tl: Timeline, t: int) -> int | None:
    for r in tl.runs:
        if r.t0 <= t < r.t1:
            return r.handle
    return None


def run_time_in(tl: Timeline, pairs: Iterable[tuple[int, int]], task: int | None = None,
                isr: Iterable[tuple[int, int]] = (), hz: float | None = None) -> dict:
    """2 点の組（開始の時刻, 経過）ごとに、タスク（省けば開始の時刻に動いていたタスク）が実際に CPU を使った時間
    （ほかのタスク・割り込みの時間を除く）。経過時間と自分の時間の分布。時間軸の外・抜けにかかる組は incomplete。"""
    per = runs_by_task(tl)
    isr_iv = Intervals(isr)
    gaps = Intervals((g.t0, g.t1) for g in tl.gaps)
    names = {r.handle: r.task for r in tl.runs}
    rows = []
    for t, d in pairs:
        handle = task if task is not None else _running_at(tl, t)
        own = 0
        if handle is not None and handle in per:
            for a, b in per[handle].pairs:
                lo, hi = max(a, t), min(b, t + d)
                if hi > lo:
                    own += (hi - lo) - isr_iv.overlap(lo, hi)
        row = {"t": t, "task": name_of(names, handle), "wall": d, "own": own, "preempted": d - own}
        if t < tl.t0 or t + d > tl.t1 or gaps.overlap(t, t + d):
            row["incomplete"] = True
        rows.append(row)
    complete = [r for r in rows if not r.get("incomplete")]
    out = {"count": len(complete), "incomplete": len(rows) - len(complete),
           "wall": _dist([r["wall"] for r in complete]), "own": _dist([r["own"] for r in complete]),
           "intervals": rows}
    if hz:
        out["wall_us"], out["own_us"] = _dist_us(out["wall"], hz), _dist_us(out["own"], hz)
    return out
