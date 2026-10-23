"""タスクの時間軸の解析（U09）: 窓ごとの負荷・ジョブ・横取りを除いた区間・スタックとヒープの推移・割り込みとタスクの合成・
優先度の逆転の区間。入出力なし（時刻はすべてタイムベース。hz を渡すと µs も付ける）。

仕様: docs/superpowers/specs/2026-10-18-u08-u09-rtos-objects-timeline-design.md の 2 節。

    load_series(tl, window, idle={idle_tcb})                   # 窓ごとの CPU 負荷・タスクごとの割合
    job_summary(find_jobs(tl, idle=..., events=...))           # ジョブ（出来事で区切る）の実行時間・周期・応答時間
    run_summary(tl, idle=...)                                   # タスクの区間の回数・時間・周期・次に動いたタスク
    run_time_in(tl, pairs)                                     # 2 点の間で、そのタスクが実際に使った時間
    memory_series(samples, slots)                              # 保存された SP・ヒープの残量の推移
    execution_profile(tl, activations, idle=...)               # スレッド・割り込み・アイドルの割合
    inversion_episodes(tl, samples, watches, priorities)       # ミューテックスの持ち主・待ちの先頭の語から逆転の区間

割り込みの区間（isr）は方法 itm の例外トレースから（`exception_activations`）。無ければ割り込みはタスクの時間に含まれる。
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from ..itm import Overflow, Packet
from ..profile.exceptions import Activation, ExceptionState, exception_name
from ..profile.stats import Distribution
from .timeline import Timeline, name_of

__all__ = ["BLOCK_KINDS", "Intervals", "Job", "KernelEvent", "MemorySlot", "MutexWatch", "exception_activations",
           "execution_profile", "find_jobs", "inversion_episode", "inversion_episodes", "inversion_summary",
           "isr_intervals", "job_summary", "load_series", "memory_series", "run_summary", "run_time_in", "runs_by_task"]

BLOCK_KINDS = ("block", "delay", "suspend", "delete")      # 実行可能でなくなった出来事（U10 のトレースフック）


# --- 部品 ----------------------------------------------------------------------------------------------

class Intervals:
    """重ならない区間 [t0, t1) の集まり（並べ直してつなぐ）。overlap は二分探索と累積和で。"""

    def __init__(self, pairs: Iterable[tuple[int, int]]):
        merged: list[list[int]] = []
        for t0, t1 in sorted(p for p in pairs if p[1] > p[0]):
            if merged and t0 <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], t1)
            else:
                merged.append([t0, t1])
        self.pairs = [(a, b) for a, b in merged]
        self._starts = [a for a, _ in self.pairs]
        self._cum = [0]
        for a, b in self.pairs:
            self._cum.append(self._cum[-1] + b - a)

    def overlap(self, t0: int, t1: int) -> int:
        if t1 <= t0 or not self.pairs:
            return 0
        i = max(bisect.bisect_right(self._starts, t0) - 1, 0)
        j = bisect.bisect_left(self._starts, t1)
        if i >= j:
            return 0
        total = self._cum[j] - self._cum[i]
        a, b = self.pairs[i]
        total -= max(0, min(b, t0) - a)                       # 最初の区間の t0 より前
        a, b = self.pairs[j - 1]
        total -= max(0, b - max(a, t1))                       # 最後の区間の t1 より後
        return total


def isr_intervals(activations: Iterable[Activation]) -> list[tuple[int, int]]:
    """割り込みの回の区間（入れ子はまとめる）: CPU が何かの例外を処理していた時間。"""
    return Intervals((a.start, a.end) for a in activations).pairs


def exception_activations(packets: Iterable[tuple[int, Packet]]) -> tuple[list[Activation], int, int]:
    """時刻付きの ITM のパケット（itm_packets の結果。SWO のバイトのデバイスの時刻）から、例外の回。
    戻り値: (回, 並びが合わなかった数, あふれの数)。"""
    state = ExceptionState()
    out: list[Activation] = []
    overflows = 0
    for t, p in packets:
        if isinstance(p, Overflow):
            overflows += 1
        out += state.feed(p, t)
    return out, state.inconsistencies, overflows


def _us(value: float, hz: float | None) -> float | None:
    return None if not hz else value * 1e6 / hz


def _dist(values: Sequence[float]) -> dict:
    return Distribution(values).as_dict()


def _dist_us(stats: dict, hz: float | None) -> dict | None:
    if not hz or not stats.get("count"):
        return None
    return {k: (v * 1e6 / hz if k != "count" else v) for k, v in stats.items()}


def runs_by_task(tl: Timeline) -> dict[int, Intervals]:
    per: dict[int, list[tuple[int, int]]] = {}
    for r in tl.runs:
        per.setdefault(r.handle, []).append((r.t0, r.t1))
    return {h: Intervals(p) for h, p in per.items()}


# --- 窓ごとの負荷 ----------------------------------------------------------------------------------------

def load_series(tl: Timeline, window: int, idle: Iterable[int] = (), isr: Iterable[tuple[int, int]] = (),
                hz: float | None = None) -> dict:
    """窓（window タイムベース）ごとの、情報のある時間（抜けを除く）に対する割合: タスクごと（割り込みの時間を除く）・
    割り込み・負荷（= 1 − アイドルの割合。割り込みとカーネルの切り替えは負荷に入る）。"""
    if window <= 0:
        raise ValueError("窓の幅は正")
    if tl.t1 <= tl.t0 or not tl.runs:
        return {"window": window, "window_us": _us(window, hz), "windows": [], "peak": None, "mean_load": None}
    idle = set(idle)
    gaps = Intervals((g.t0, g.t1) for g in tl.gaps)
    isr_iv = Intervals(isr)
    names = {r.handle: r.task for r in tl.runs}
    count = max(1, math.ceil((tl.t1 - tl.t0) / window))
    time: list[dict[int, int]] = [{} for _ in range(count)]
    for r in tl.runs:
        k = max(0, (r.t0 - tl.t0) // window)
        while k < count:
            w0 = tl.t0 + k * window
            if w0 >= r.t1:
                break
            a, b = max(r.t0, w0), min(r.t1, w0 + window)
            if b > a:
                own = (b - a) - isr_iv.overlap(a, b)
                time[k][r.handle] = time[k].get(r.handle, 0) + own
            k += 1
    windows = []
    for k in range(count):
        w0 = tl.t0 + k * window
        w1 = min(w0 + window, tl.t1)
        covered = (w1 - w0) - gaps.overlap(w0, w1)
        isr_time = isr_iv.overlap(w0, w1)
        if covered <= 0:
            windows.append({"t0": w0, "t1": w1, "covered": 0, "load": None, "isr": None, "tasks": {}})
            continue
        idle_time = sum(v for h, v in time[k].items() if h in idle)
        row = {"t0": w0, "t1": w1, "covered": covered, "load": max(0.0, 1 - idle_time / covered),
               "isr": isr_time / covered,
               "tasks": {names[h]: v / covered for h, v in sorted(time[k].items(), key=lambda x: -x[1]) if v}}
        windows.append(row)
    loads = [w for w in windows if w["load"] is not None]
    peak = max(loads, key=lambda w: w["load"]) if loads else None
    total_covered = sum(w["covered"] for w in loads)
    out = {"window": window, "window_us": _us(window, hz), "windows": windows,
           "peak": None if peak is None else {"t0": peak["t0"], "t1": peak["t1"], "load": peak["load"]},
           "mean_load": sum(w["load"] * w["covered"] for w in loads) / total_covered if total_covered else None,
           "idle_tasks": sorted({names[h] for h in idle if h in names})}
    return out


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


# --- スタック・ヒープの推移 ------------------------------------------------------------------------------

@dataclass(frozen=True)
class MemorySlot:
    """サンプラで読む語の意味。kind stack: タスクの保存された SP（base〜end のスタック）。kind heap: ヒープの残量。"""
    kind: str
    name: str
    handle: int | None = None
    base: int | None = None
    end: int | None = None
    total: int | None = None


def memory_series(samples: Iterable[tuple[int, int, int]], slots: Mapping[int, MemorySlot], points: int = 200,
                  hz: float | None = None) -> dict:
    """(時刻, 番地, 値) のサンプルから、タスクごとのスタックの使用量（end − 保存された SP）とヒープの残量の推移。
    点は全体の時間を points 個の窓に分け、窓ごとに最悪の値（スタックは最大、ヒープは最小）。値は次のサンプルまで続く。"""
    rows = sorted((t, a, v) for t, a, v in samples if a in slots)
    out: dict = {"stacks": {}, "heap": None}
    if not rows:
        return out
    t_first, t_last = rows[0][0], rows[-1][0]
    width = max(1, math.ceil((t_last - t_first + 1) / max(points, 1)))
    per: dict[int, list[tuple[int, int]]] = {}
    for t, a, v in rows:
        slot = slots[a]
        value = (slot.end - v) if slot.kind == "stack" and slot.end is not None else v
        per.setdefault(a, []).append((t, value))
    for address, series in per.items():
        slot = slots[address]
        pick = max if slot.kind == "stack" else min
        buckets: dict[int, int] = {}
        current = None
        k_last = (t_last - t_first) // width
        i = 0
        for k in range(k_last + 1):
            w1 = t_first + (k + 1) * width
            values = [] if current is None else [current]
            while i < len(series) and series[i][0] < w1:
                current = series[i][1]
                values.append(current)
                i += 1
            if values:
                buckets[k] = pick(values)
        points_out = [[t_first + k * width, v] for k, v in sorted(buckets.items())]
        values = [v for _, v in series]
        extreme_t, extreme_v = pick(series, key=lambda x: x[1])
        if slot.kind == "stack":
            size = None if slot.end is None or slot.base is None else slot.end - slot.base
            row = {"handle": slot.handle, "size": size, "max_used": max(values), "min_used": min(values),
                   "last_used": values[-1], "max_at": extreme_t, "points": points_out,
                   "overflow": size is not None and max(values) > size}
            out["stacks"][slot.name] = row
        else:
            out["heap"] = {"total": slot.total, "min_free": min(values), "max_free": max(values),
                           "last_free": values[-1], "min_at": extreme_t, "points": points_out}
    out["points_width"] = width
    out["points_width_us"] = _us(width, hz)
    return out


# --- 割り込みとタスクの合成 ------------------------------------------------------------------------------

def execution_profile(tl: Timeline, activations: Iterable[Activation] | None, idle: Iterable[int] = (),
                      names: Mapping[int, str] | None = None, hz: float | None = None) -> dict:
    """情報のある時間に対する、スレッド（タスクごと。割り込みの時間を除く）・割り込み（例外ごとの自分の時間）・アイドル・
    どれでもない時間（切り替えの合間・情報の無い隙間）の割合。activations が None なら割り込みは分からない（タスクに含む）。"""
    idle = set(idle)
    acts = list(activations) if activations is not None else []
    isr_iv = Intervals((a.start, a.end) for a in acts)
    covered = tl.covered()
    threads: dict[str, dict] = {}
    thread_total = idle_total = 0
    for r in tl.runs:
        own = r.duration - isr_iv.overlap(r.t0, r.t1)
        row = threads.setdefault(r.task, {"handle": r.handle, "time": 0, "idle": r.handle in idle})
        row["time"] += own
        if r.handle in idle:
            idle_total += own
        else:
            thread_total += own
    interrupts: dict[str, dict] | None = None
    isr_total = 0
    if activations is not None:
        interrupts = {}
        for a in acts:
            row = interrupts.setdefault(exception_name(a.number, names), {"count": 0, "time": 0})
            row["count"] += 1
            row["time"] += a.self_time
        isr_total = sum(row["time"] for row in interrupts.values())
        for row in interrupts.values():
            row["share"] = row["time"] / covered if covered else None
    for row in threads.values():
        row["share"] = row["time"] / covered if covered else None
    out = {"covered": covered, "threads": threads, "interrupts": interrupts,
           "share": None if not covered else {
               "thread": thread_total / covered, "interrupt": isr_total / covered, "idle": idle_total / covered,
               "unattributed": max(0.0, (covered - thread_total - isr_total - idle_total) / covered)}}
    if hz:
        out["covered_us"] = _us(covered, hz)
    return out


# --- 優先度の逆転 --------------------------------------------------------------------------------------

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
                       priorities: Mapping[int, int], order: int = 1, names: Mapping[int, str] | None = None,
                       hz: float | None = None) -> dict:
    """持ち主の元の優先度より高い待ち手がいる区間（優先度の逆転）。その間に動いた、持ち主と待ち手の間の優先度の
    タスクの時間（interference: 継承が効いていれば 0。正なら際限の無い逆転の印）と、持ち主が動いた時間。
    サンプラの周期より短い逆転は見えないことがある（回数は下限、端は ± 周期）。"""
    if not watches:
        return {"available": False, "episodes": [], "count": 0,
                "reason": "ミューテックスの語を読んでいない（rtos_timeline_start の mutexes）。トレースフックの出来事"
                          "（U10）も無いので、逆転は推測しない"}
    names = dict(names or {})
    names.update({r.handle: r.task for r in tl.runs})
    per = runs_by_task(tl)

    def urgency(h: int | None) -> int | None:
        p = priorities.get(h) if h is not None else None
        return None if p is None else p * order
    episodes = []
    rows = sorted(samples)
    edge = int(tl.resolution // 2)          # 区間の端（サンプルの中点）の不確かさ: 確かな範囲はその分だけ内側
    for w in watches:
        holder = head = None
        opened: tuple[int, int, int] | None = None              # (始め, 持ち主, 待ち手)
        seen = 0                                                # 逆転を最後に見たサンプルの時刻
        for t, a, v in rows:
            if a == w.holder_word:
                holder = v or None
            elif a == w.head_word:
                head = None if v in (w.empty, 0) else v - w.delta
            else:
                continue
            hu, wu = urgency(holder), urgency(head)
            inverted = hu is not None and wu is not None and holder != head and wu > hu
            key = (holder, head)
            if opened is not None and (not inverted or (opened[1], opened[2]) != key):
                episodes.append(inversion_episode(w.name, opened, t, per, urgency, names,
                                                  certain=(opened[0] + edge, seen - edge)))
                opened = None
            if inverted and opened is None:
                opened = (t, holder, head)
            if inverted:
                seen = t
        if opened is not None:
            episodes.append(inversion_episode(w.name, opened, tl.t1, per, urgency, names, truncated=True,
                                              certain=(opened[0] + edge, seen - edge)))
    out = inversion_summary(episodes, hz)
    out["notes"] = ["サンプラの周期より短い逆転は見えないことがある（回数は下限、区間の端は ± 周期）"]
    return out


def inversion_summary(episodes: list[dict], hz: float | None = None) -> dict:
    """逆転の区間の列の要約（回数・際限の無い回数・合計・長さの分布・最長）。"""
    episodes.sort(key=lambda e: e["t0"])
    durations = [e["duration"] for e in episodes]
    out = {"available": True, "count": len(episodes), "unbounded": sum(1 for e in episodes if e["unbounded"]),
           "total": sum(durations), "duration": _dist(durations), "episodes": episodes,
           "max": max(episodes, key=lambda e: e["duration"]) if episodes else None}
    if hz:
        out["duration_us"] = _dist_us(out["duration"], hz)
    return out


def inversion_episode(mutex: str, opened: tuple[int, int, int], t1: int, per: dict[int, Intervals], urgency, names,
             truncated: bool = False, certain: tuple[int, int] | None = None) -> dict:
    """逆転の 1 区間: 持ち主・待ち手、その間に動いた中間の優先度のタスクの時間（interference）、持ち主が動いた時間。
    certain: サンプルで見た区間のとき、逆転が確かに続いていた範囲（初めて見た時刻〜最後に見た時刻を、タスクの区間の
    端の不確かさだけ内側に縮めたもの）。interference はその中で数える（外では、逆転の前後に中間のタスクが動いたのかも
    しれない。サンプラは 1 巡回の中で語を順に読むので、タスクの語とミューテックスの語の時刻もずれる）。"""
    t0, holder, waiter = opened
    hu, wu = urgency(holder), urgency(waiter)
    interference = {}
    for h, iv in per.items():
        u = urgency(h)
        if h not in (holder, waiter) and u is not None and hu < u < wu:
            a, b = certain or (t0, t1)
            spent = iv.overlap(a, b) if a < b else 0
            if spent:
                interference[name_of(names, h)] = spent
    holder_time = per[holder].overlap(t0, t1) if holder in per else 0
    out = {"mutex": mutex, "holder": name_of(names, holder), "waiter": name_of(names, waiter), "t0": t0, "t1": t1,
           "duration": t1 - t0, "holder_time": holder_time, "interference": interference,
           "unbounded": bool(interference)}
    if truncated:
        out["truncated"] = True
    return out
