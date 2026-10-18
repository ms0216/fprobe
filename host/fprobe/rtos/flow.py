"""カーネルの出来事の解析（U10）: Communication Flow・ブロック時間・キュー使用率・優先度の逆転の正確な区間・割り込みの区間・
応答時間のための出来事・フックの負荷。入出力なし（時刻はタイムベース）。入力は fprobe.rtos.kernel_trace の KernelTrace。

仕様: docs/superpowers/specs/2026-10-18-u10-u16-trace-hooks-user-events-design.md の 2 節。

    communication_flow(trace, names, objects)          # タスク・割り込み・オブジェクトの有向グラフ（DOT も）
    blocking(trace, names, objects, t_end)             # 待ちの区間（block / delay / suspend → ready）と結果
    queue_usage(trace, names, objects, t1)             # キュー・セマフォのメッセージ数の推移と使用率
    inversions_from_ops(tl, trace, priorities)         # 持ち主と待ち手を出来事で追う逆転の区間（取りこぼし無し）
    isr_activations(trace)                             # isr_enter / isr_exit → Activation
    job_events(trace, task_events)                     # find_jobs（応答時間）に渡す ready / block の出来事
    overhead(trace, duration, timebase_hz, baud, core_clock_hz)   # 語の数・SWO の帯域・CPU の負荷の見積もり
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from ..profile.exceptions import Activation, exception_name
from .analysis import KernelEvent, inversion_episode, inversion_summary, runs_by_task
from .kernel_trace import PAYLOAD_CYCLES, RECORD_CYCLES, WORD_CYCLES, KernelOp, KernelTrace, object_table
from .timeline import TaskEvent, Timeline, name_of

__all__ = ["blocking", "communication_flow", "inversions_from_ops", "isr_activations", "job_events", "overhead",
           "queue_usage"]

_SUCCESS = frozenset({"send", "receive", "peek", "stream_send", "stream_receive"})
_FAILED = frozenset({"send_failed", "receive_failed"})
_QUEUE_OPS = frozenset({"send", "send_failed", "receive", "receive_failed", "peek"})
POINTS = 500                           # キューの推移の点の既定の上限


def _object_name(table: Mapping[int, dict], handle: int) -> str:
    info = table.get(handle)
    return info["name"] if info and info.get("name") else name_of({}, handle)


def _object_kind(table: Mapping[int, dict], op: KernelOp) -> str:
    info = table.get(op.obj)
    return (info or {}).get("kind") or op.obj_class or "other"


def _is_mutex(table: Mapping[int, dict], op: KernelOp) -> bool:
    return op.obj_class == "mutex" or "mutex" in _object_kind(table, op)


def _is_semaphore(table: Mapping[int, dict], op: KernelOp) -> bool:
    kind = _object_kind(table, op)
    return op.obj_class in ("semaphore", "mutex") or "semaphore" in kind or "mutex" in kind


# --- Communication Flow ---------------------------------------------------------------------------------

def communication_flow(trace: KernelTrace, names: Mapping[int, str], objects: Mapping[int, dict] | None = None,
                       isr_names: Mapping[int, str] | None = None) -> dict:
    """節: タスク・割り込み・オブジェクト。辺（from → to、op）: 送り・give（行為者 → オブジェクト）、受け・take・覗き
    （オブジェクト → 行為者）、event_set（行為者 → イベントグループ）、notify（行為者 → タスク）、wait（行為者が
    オブジェクトを待った）。count は成功の回数、failed は失敗（満杯・空・時間切れ）の回数、bytes はストリームの量。"""
    table = object_table(trace, objects)
    nodes: dict[str, dict] = {}
    edges: dict[tuple[str, str, str], dict] = {}

    def task_node(handle: int) -> str:
        name = name_of(names, handle)
        nodes.setdefault(f"task:{handle:08X}", {"id": f"task:{handle:08X}", "type": "task", "name": name,
                                                "handle": handle})
        return name

    def actor_node(op: KernelOp) -> str:
        if op.isr:
            name = exception_name(op.isr, isr_names)
            nodes.setdefault(f"isr:{op.isr}", {"id": f"isr:{op.isr}", "type": "isr", "name": name, "number": op.isr})
            return name
        if op.actor is None:
            nodes.setdefault("unknown", {"id": "unknown", "type": "unknown", "name": "?"})
            return "?"
        return task_node(op.actor)

    def object_node(op: KernelOp) -> str:
        name = _object_name(table, op.obj)
        nodes.setdefault(f"object:{op.obj:08X}", {"id": f"object:{op.obj:08X}", "type": "object", "name": name,
                                                  "handle": op.obj, "kind": _object_kind(table, op)})
        return name

    def edge(src: str, dst: str, kind: str, ok: bool, amount: int | None = None) -> None:
        row = edges.setdefault((src, dst, kind), {"from": src, "to": dst, "op": kind, "count": 0, "failed": 0})
        row["count" if ok else "failed"] += 1
        if amount is not None:
            row["bytes"] = row.get("bytes", 0) + amount
    for op in trace.ops:
        if op.kind in ("send", "send_failed"):
            edge(actor_node(op), object_node(op), "give" if _is_semaphore(table, op) else "send", op.kind == "send")
        elif op.kind in ("receive", "receive_failed", "peek"):
            kind = "peek" if op.kind == "peek" else "take" if _is_semaphore(table, op) else "receive"
            edge(object_node(op), actor_node(op), kind, op.kind != "receive_failed")
        elif op.kind == "stream_send":
            edge(actor_node(op), object_node(op), "send", True, op.arg)
        elif op.kind == "stream_receive":
            edge(object_node(op), actor_node(op), "receive", True, op.arg)
        elif op.kind == "event_set":
            edge(actor_node(op), object_node(op), "set", True)
        elif op.kind == "notify":
            edge(actor_node(op), task_node(op.task), "notify", True)
        elif op.kind == "block" and op.obj:
            edge(actor_node(op), object_node(op), "wait", True)
    rows = sorted(edges.values(), key=lambda e: (-e["count"] - e["failed"], e["from"], e["to"]))
    return {"nodes": sorted(nodes.values(), key=lambda n: (n["type"], n["name"])), "edges": rows,
            "dot": _dot(nodes.values(), rows), "lost": trace.lost}


def _dot(nodes: Iterable[dict], edges: list[dict]) -> str:
    shapes = {"task": "box", "isr": "diamond", "object": "ellipse", "unknown": "plaintext"}
    lines = ["digraph flow {", "  rankdir=LR;"]
    for n in nodes:
        lines.append(f'  "{n["name"]}" [shape={shapes[n["type"]]}];')
    for e in edges:
        label = f'{e["op"]} {e["count"]}' + (f' (failed {e["failed"]})' if e["failed"] else "")
        style = ", style=dashed" if e["op"] == "wait" else ""
        lines.append(f'  "{e["from"]}" -> "{e["to"]}" [label="{label}"{style}];')
    lines.append("}")
    return "\n".join(lines)


# --- ブロック時間 ---------------------------------------------------------------------------------------

@dataclass
class _Wait:
    task: int
    on: str
    obj: int
    wait: str | None
    t0: int
    t1: int | None = None
    outcome: str | None = None
    truncated: bool = False

    def to_dict(self, names: Mapping[int, str]) -> dict:
        out = {"task": name_of(names, self.task), "handle": self.task, "on": self.on, "object_handle": self.obj or None,
               "wait": self.wait, "t0": self.t0, "t1": self.t1, "duration": None if self.t1 is None else self.t1 - self.t0,
               "outcome": self.outcome}
        if self.truncated:
            out["truncated"] = True
        return out


def blocking(trace: KernelTrace, names: Mapping[int, str], objects: Mapping[int, dict] | None = None,
             t_end: int | None = None, hz: float | None = None) -> dict:
    """待ちの区間: block（オブジェクト・通知を待った）/ delay / suspend から、そのタスクの次の ready まで。
    outcome: 起きた後の同じオブジェクトへの操作が成功 = ok、失敗 = timeout（分からなければ None）。観測の終わりで
    開いている待ちは truncated（統計に入れない）。lost（あふれ）で開いていた待ちは忘れる（forgotten）。"""
    table = object_table(trace, objects)
    open_waits: dict[int, _Wait] = {}
    awaiting: dict[int, _Wait] = {}
    done: list[_Wait] = []
    forgotten = 0

    def begin(task: int, op: KernelOp, on: str, obj: int = 0, wait: str | None = None) -> None:
        if task and task not in open_waits:
            open_waits[task] = _Wait(task, on, obj, wait, op.t)
    for op in trace.ops:
        if op.kind == "lost":
            forgotten += len(open_waits)
            open_waits.clear()
            awaiting.clear()
            continue
        if not op.isr and op.actor is not None and op.actor in awaiting:
            w = awaiting[op.actor]
            if op.kind == "block":
                del awaiting[op.actor]
            elif op.obj == w.obj and (op.kind in _SUCCESS or op.kind in _FAILED):
                w.outcome = "ok" if op.kind in _SUCCESS else "timeout"
                del awaiting[op.actor]
        if op.kind == "block" and not op.isr and op.actor is not None:
            on = _object_name(table, op.obj) if op.obj else "notify"
            begin(op.actor, op, on, op.obj, op.wait)
        elif op.kind == "delay":
            begin(op.task, op, "delay")
        elif op.kind == "suspend":
            begin(op.task, op, "suspend")
        elif op.kind == "ready" and op.task in open_waits:
            w = open_waits.pop(op.task)
            w.t1 = op.t
            done.append(w)
            if w.obj:
                awaiting[op.task] = w
    end = t_end if t_end is not None else (trace.ops[-1].t if trace.ops else 0)
    for w in open_waits.values():
        w.t1, w.truncated = max(end, w.t0), True
        done.append(w)
    done.sort(key=lambda w: (w.t0, w.task))
    complete = [w for w in done if not w.truncated]
    tasks: dict[str, dict] = {}
    for w in complete:
        tasks.setdefault(name_of(names, w.task), []).append(w)
    by_object: dict[str, list[_Wait]] = {}
    for w in complete:
        if w.obj:
            by_object.setdefault(w.on, []).append(w)
    return {"waits": [w.to_dict(names) for w in done],
            "tasks": {name: _wait_stats(ws, hz, by="on") for name, ws in tasks.items()},
            "objects": {name: _wait_stats(ws, hz, by="task", names=names) for name, ws in by_object.items()},
            "open": len(open_waits), "forgotten": forgotten}


def _wait_stats(waits: list[_Wait], hz: float | None, by: str, names: Mapping[int, str] | None = None) -> dict:
    durations = [w.t1 - w.t0 for w in waits]
    longest = max(waits, key=lambda w: w.t1 - w.t0)
    split: dict[str, dict] = {}
    for w in waits:
        key = w.on if by == "on" else name_of(names or {}, w.task)
        row = split.setdefault(key, {"count": 0, "total": 0})
        row["count"] += 1
        row["total"] += w.t1 - w.t0
    out = {"count": len(waits), "total": sum(durations), "mean": sum(durations) / len(durations),
           "max": {"t0": longest.t0, "t1": longest.t1, "duration": longest.t1 - longest.t0, "on": longest.on,
                   "task": name_of(names or {}, longest.task)},
           "timeouts": sum(1 for w in waits if w.outcome == "timeout"), "by_" + by: split}
    if hz:
        out["total_us"] = out["total"] * 1e6 / hz
        out["max"]["duration_us"] = out["max"]["duration"] * 1e6 / hz
    return out


# --- キュー使用率 -----------------------------------------------------------------------------------------

def queue_usage(trace: KernelTrace, names: Mapping[int, str], objects: Mapping[int, dict] | None = None,
                t1: int | None = None, points: int = POINTS) -> dict:
    """キュー・セマフォごとのメッセージ数（セマフォは数）の推移（操作の後の数）・最大・最小・時間で重みを付けた平均と、
    長さに対する割合。送り・受けの成功の回数、失敗（send_failed = 満杯、receive_failed = 空）、待ち（waits_send /
    waits_receive）。ミューテックスは含めない（持ち主の区間は rtos.timeline.inversions）。"""
    table = object_table(trace, objects)
    per: dict[int, dict] = {}

    def row_for(op: KernelOp) -> dict:
        row = per.get(op.obj)
        if row is None:
            info = table.get(op.obj) or {}
            row = per[op.obj] = {"handle": op.obj, "kind": _object_kind(table, op), "length": info.get("length"),
                                 "series": [], "sends": 0, "receives": 0, "send_failed": 0, "receive_failed": 0,
                                 "waits_send": 0, "waits_receive": 0, "saturated": False}
        return row
    for op in trace.ops:
        if not op.obj or _is_mutex(table, op) or op.obj_class == "other":
            continue
        if op.kind == "block":
            if op.wait in ("send", "receive"):
                row_for(op)[f"waits_{op.wait}"] += 1
            continue
        if op.kind not in _QUEUE_OPS:
            continue
        row = row_for(op)
        after = op.arg + 1 if op.kind == "send" else max(0, op.arg - 1) if op.kind == "receive" else op.arg
        if row["length"] is not None:
            after = min(after, row["length"])
        row["series"].append((op.t, after))
        row["saturated"] |= op.saturated
        if op.kind == "send":
            row["sends"] += 1
        elif op.kind == "receive":
            row["receives"] += 1
        elif op.kind in _FAILED:
            row[op.kind] += 1
    end = t1 if t1 is not None else (trace.ops[-1].t if trace.ops else 0)
    out = {}
    for handle, row in per.items():
        series = row.pop("series")
        if series:
            values = [v for _, v in series]
            span = max(end - series[0][0], 0)
            weighted = sum(v * (max(min(nt, end), t) - t) for (t, v), (nt, _) in zip(series, series[1:] + [(end, 0)]))
            row.update(max=max(values), min=min(values), last=values[-1],
                       mean=weighted / span if span else float(values[-1]),
                       points=_downsample(series, points), points_total=len(series))
            length = row["length"]
            row["max_usage"] = row["max"] / length if length else None
            row["mean_usage"] = row["mean"] / length if length else None
        else:
            row.update(max=None, min=None, last=None, mean=None, points=[], points_total=0, max_usage=None,
                       mean_usage=None)
        if not row["saturated"]:
            del row["saturated"]
        out[_object_name(table, handle)] = row
    return {"objects": out, "lost": trace.lost}


def _downsample(series: list[tuple[int, int]], limit: int) -> list[list[int]]:
    """点が limit を超えたら、等しい幅の窓ごとに最大の点（山を落とさない）。"""
    if len(series) <= limit or limit <= 0:
        return [[t, v] for t, v in series]
    t0, t1 = series[0][0], series[-1][0]
    width = max(1, math.ceil((t1 - t0 + 1) / limit))
    best: dict[int, tuple[int, int]] = {}
    for t, v in series:
        k = (t - t0) // width
        if k not in best or v > best[k][1]:
            best[k] = (t, v)
    return [[t, v] for t, v in (best[k] for k in sorted(best))]


# --- 優先度の逆転（出来事から） ---------------------------------------------------------------------------

@dataclass
class _MutexState:
    holder: int | None = None
    waiters: dict | None = None
    opened: tuple[int, int, int] | None = None
    inherited: dict | None = None


def inversions_from_ops(tl: Timeline, trace: KernelTrace, priorities: Mapping[int, int], order: int = 1,
                        names: Mapping[int, str] | None = None, objects: Mapping[int, dict] | None = None,
                        hz: float | None = None) -> dict:
    """ミューテックスごとに、持ち主（take した）と待ち手（block して、まだ ready でない）を出来事の並びで追い、持ち主の
    元の優先度より高い待ち手がいる区間を逆転とする。区間は [待ち手の block, 持ち主の give・待ち手の ready・持ち主か
    最も高い待ち手が変わった時）。inherited は区間の中の優先度の継承（いつ・何に・始めからの遅れ）。"""
    table = object_table(trace, objects)
    names = dict(names or {})
    names.update({r.handle: r.task for r in tl.runs})
    per = runs_by_task(tl)
    states: dict[int, _MutexState] = {}
    episodes: list[dict] = []

    def urgency(h: int | None) -> int | None:
        p = priorities.get(h) if h is not None else None
        return None if p is None else p * order

    def close(handle: int, st: _MutexState, t: int, truncated: bool = False) -> None:
        ep = inversion_episode(_object_name(table, handle), st.opened, t, per, urgency, names, truncated)
        ep["inherited"] = st.inherited
        episodes.append(ep)
        st.opened, st.inherited = None, None

    def evaluate(handle: int, st: _MutexState, t: int) -> None:
        hu = urgency(st.holder)
        top = max(st.waiters, key=lambda w: urgency(w) or -math.inf) if st.waiters else None
        wu = urgency(top)
        inverted = hu is not None and wu is not None and top != st.holder and wu > hu
        if st.opened is not None and (not inverted or (st.opened[1], st.opened[2]) != (st.holder, top)):
            close(handle, st, t)
        if inverted and st.opened is None:
            st.opened = (t, st.holder, top)
    for op in trace.ops:
        if op.kind == "lost":
            for handle, st in states.items():
                if st.opened is not None:
                    close(handle, st, op.t, truncated=True)
            states.clear()
            continue
        if op.kind == "ready":
            for handle, st in states.items():
                if op.task in st.waiters:
                    del st.waiters[op.task]
                    evaluate(handle, st, op.t)
            continue
        if op.kind == "prio_inherit":
            for st in states.values():
                if st.opened is not None and st.holder == op.task and st.inherited is None:
                    st.inherited = {"t": op.t, "priority": op.arg, "latency": op.t - st.opened[0]}
            continue
        if not op.obj or not _is_mutex(table, op) or op.isr or op.actor is None:
            continue
        st = states.setdefault(op.obj, _MutexState(waiters={}))
        if op.kind == "receive":
            st.holder = op.actor
            st.waiters.pop(op.actor, None)
        elif op.kind == "send" and st.holder in (op.actor, None):
            st.holder = None
        elif op.kind == "block":
            st.waiters[op.actor] = op.t
        else:
            continue
        evaluate(op.obj, st, op.t)
    for handle, st in states.items():
        if st.opened is not None:
            close(handle, st, tl.t1, truncated=True)
    out = inversion_summary(episodes, hz)
    out["source"] = "trace"
    out["notes"] = ["トレースフックの出来事から（取りこぼし無し）。あふれ（lost）の間は分からない（truncated）"]
    return out


# --- 割り込み・ジョブの出来事 -------------------------------------------------------------------------------

def isr_activations(trace: KernelTrace) -> list[Activation]:
    """isr_enter / isr_exit（IPSR で対にする。入れ子は自分の時間から子の時間を引く）→ 例外の回。入りの無い出
    （portEND_SWITCHING_ISR だけを呼ぶ ISR）は数えない。"""
    stack: list[list[int]] = []                                 # [番号, 入った時刻, 子の時間]
    out: list[Activation] = []
    for op in trace.ops:
        if op.kind == "lost":
            stack.clear()
        elif op.kind == "isr_enter":
            stack.append([op.isr, op.t, 0])
        elif op.kind == "isr_exit" and any(f[0] == op.isr for f in stack):
            while stack[-1][0] != op.isr:
                stack.pop()                                     # 出の欠けた内側の回は捨てる
            number, start, child = stack.pop()
            inclusive = op.t - start
            out.append(Activation(number, start, op.t, inclusive, inclusive - child))
            if stack:
                stack[-1][2] += inclusive
    return out


def job_events(trace: KernelTrace, task_events: Iterable[TaskEvent] = ()) -> list[KernelEvent]:
    """find_jobs に渡す出来事: ready（対象のタスク）、block（待ったタスク）、delay・suspend、delete（ポート 31）。"""
    out = []
    for op in trace.ops:
        if op.kind == "ready" or op.kind in ("delay", "suspend"):
            out.append(KernelEvent(op.t, op.kind, op.task))
        elif op.kind == "block" and not op.isr and op.actor is not None:
            out.append(KernelEvent(op.t, "block", op.actor))
    out += [KernelEvent(e.t, "delete", e.handle) for e in task_events if e.kind == "delete"]
    return sorted(out, key=lambda e: e.t)


# --- フックの負荷 -----------------------------------------------------------------------------------------

def overhead(trace: KernelTrace, duration: int, timebase_hz: float, baud: float | None = None,
             core_clock_hz: float | None = None) -> dict:
    """トレースの語の数（ポート 31・見出し・中身）・SWO のバイト数と帯域の使用率・CPU の負荷の見積もり。
    CPU は語の数 × 1 語あたりの命令の見積もり（kernel_trace の WORD_CYCLES 等）+ FIFO の待ち / コアのクロック。
    FIFO の待ち（baud と core_clock_hz があるとき）: 記録の中身の語は見出しのすぐ後に書くので、見出しが SWO に出る
    （1 語 = 5 バイト × 10 ビット）まで待つ。語どうしが離れている分（ポート 31 の語・記録の見出し）の待ちは数えない
    （swo_share が 1 に近いとそれも増え、あふれる）。"""
    w = trace.words
    words = w["task"] + w["kernel"] + w["payload"]
    seconds = duration / timebase_hz if timebase_hz else 0
    instructions = w["task"] * WORD_CYCLES + w["kernel"] * RECORD_CYCLES + w["payload"] * PAYLOAD_CYCLES
    wait = None
    if baud and core_clock_hz:
        wait = w["payload"] * max(0.0, 50 / baud * core_clock_hz - PAYLOAD_CYCLES)
    cycles = instructions + (wait or 0)
    out = {"words": words, "words_by_port": dict(w), "bytes": 5 * words, "seconds": seconds,
           "words_per_s": words / seconds if seconds else None, "by_kind": trace.by_kind(), "lost": trace.lost,
           "cycles": cycles, "instruction_cycles": instructions, "fifo_wait_cycles": wait,
           "swo_share": None, "cpu_share": None,
           "cycles_model": {"task_word": WORD_CYCLES, "record": RECORD_CYCLES, "payload_word": PAYLOAD_CYCLES},
           "notes": ["CPU の負荷は命令数と、記録の中身の語が FIFO の空きを待つ時間からの見積もり。実機の計測は"
                     "gateware/tests/hil/test_trace_hooks_hw.py（トレースの有無でアイドルの回る数を比べる）"]}
    if baud and seconds:
        out["swo_share"] = 5 * words * 10 / baud / seconds
        if out["swo_share"] > 0.8:
            out["notes"].append("SWO の帯域の 8 割を超えている: ITM の FIFO が待ち・あふれる。SWO を速くするか出来事を減らす")
    if core_clock_hz and seconds:
        out["cpu_share"] = cycles / core_clock_hz / seconds
    return out
