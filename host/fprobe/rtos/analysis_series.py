"""タスクの時間軸の解析（U09）の土台: 区間（Intervals）・割り込みの区間・窓ごとの負荷・スタックとヒープの推移・
割り込みとタスクの合成。入出力なし（時刻はすべてタイムベース。hz を渡すと µs も付ける）。

ジョブは analysis_jobs.py、優先度の逆転は analysis_inversion.py。公開の名前は rtos.analysis から。
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from ..itm import Overflow, Packet
from ..profile.exceptions import Activation, ExceptionState, exception_name
from ..profile.stats import Distribution
from .timeline import Timeline

__all__ = ["BLOCK_KINDS", "Intervals", "MemorySlot", "exception_activations", "execution_profile", "isr_intervals",
           "load_series", "memory_series", "runs_by_task"]

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
            windows.append({"t0": w0, "t1": w1, "covered": 0, "load": None, "isr": None, "other": None, "tasks": {}})
            continue
        idle_time = sum(v for h, v in time[k].items() if h in idle)
        busy_time = sum(v for h, v in time[k].items() if h not in idle)
        row = {"t0": w0, "t1": w1, "covered": covered, "load": max(0.0, 1 - idle_time / covered),
               "isr": isr_time / covered,
               # その他: 負荷のうちタスクにも割り込みにも入らない分（整数のタイムベースで数えるので丸めの誤差が無い）
               "other": (covered - idle_time - isr_time - busy_time) / covered,
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
    点は全体の時間を points 個の窓に分け、窓ごとに、その窓に入ったサンプルの最悪の値（スタックは最大、ヒープは最小）を、
    そのサンプルの時刻で（窓の始めの時刻にしない）。
    サンプルの無い窓には点を置かない（値が次のサンプルまで続いたとは仮定しない）。"""
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
        buckets: dict[int, tuple[int, int]] = {}            # 窓 → (そのサンプルの時刻, 値)
        for t, value in series:                            # 窓に入ったサンプルだけ（前の値を続けて入れない）
            k = (t - t_first) // width
            if k not in buckets or pick(buckets[k][1], value) != buckets[k][1]:
                buckets[k] = (t, value)
        points_out = [[t, v] for _, (t, v) in sorted(buckets.items())]       # 点の時刻 = その値を読んだサンプルの時刻
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
