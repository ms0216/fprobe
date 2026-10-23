"""取り込んだトレースの共通の形。形式（SystemView・CTF・Event Recorder・TraceX・Tracealyzer）の解読は、それぞれ
この形の出来事（`TraceEvent`）の列と、タスクの一覧（`TaskInfo`）にする。

- 時刻 `ts` はターゲットの時計（形式の単位。`Trace.ts_hz` が速さ。分からなければ None）で、解読が 32 ビットの
  桁あふれを直した単調な値。
- 出来事の種類（kind）は形式に依らない名前にそろえる（`KINDS`）。形式ごとの名前は `name`、値は `args`。
- 共通の時間軸の記録へは `TargetClock` でタイムベースに写して stream "trace" に書き（`write_trace`）、タスクの
  出入りは fprobe.rtos.timeline の `TaskEvent` にして stream "rtos" の時間軸（run / switch / gap）にもできる
  （`task_events` → `timeline_from_events`）。こうすると時間軸の解析（rtos_timeline_load 等）がそのまま使える。
"""

from __future__ import annotations

import re

from collections.abc import Iterable
from dataclasses import dataclass, field

from ..profile.exceptions import Activation
from ..rtos.timeline import TaskEvent

__all__ = ["KINDS", "STREAM", "TargetClock", "TaskInfo", "Trace", "TraceEvent", "activations", "task_events",
           "is_rtos_idle_name", "unwrap32", "write_trace"]

STREAM = "trace"
# RTOS のソースが定めるアイドルのタスクの名前: FreeRTOS の configIDLE_TASK_NAME の既定 "IDLE"（SMP はコアの番号が付く）・
# Zephyr の "idle"（SMP は "idle %02d"）・RTX5 の "osRtxIdleThread"。部分一致はしない（利用者のタスクを idle にしない）
_IDLE_NAME = re.compile(r"IDLE\d*|idle( \d+)?|osRtxIdleThread")


def is_rtos_idle_name(name: str) -> bool:
    """RTOS が定めるアイドルのタスクの名前と完全に一致するか（取り込んだトレースでカーネルの情報が無いとき）。"""
    return bool(_IDLE_NAME.fullmatch(name))
KINDS = (
    "task_in",        # タスクが CPU を使い始めた（task = そのタスク）
    "task_out",       # タスクが CPU を手放した
    "task_ready",     # 実行できる状態になった
    "task_block",     # 待ちに入った（理由は args）
    "task_create",
    "task_delete",
    "task_info",      # 名前・優先度・スタック（tasks に反映済み）
    "isr_enter",      # 割り込みに入った（args["number"] = 例外の番号。分かれば）
    "isr_exit",
    "idle",           # アイドル（どのタスクも動いていない）に入った
    "print",          # 文字のログ（text）
    "marker",         # 名前付きの出来事・利用者の印（ZEP-03・SystemView の Mark 等）
    "object",         # カーネルオブジェクトの操作（セマフォ・ミューテックス・キュー等。name と args）
    "overflow",       # ターゲットのバッファがあふれて出来事を失った
    "info",           # 記録の情報（システムの説明・時刻の速さ等）
    "other",          # 解読はしたが上のどれでもない
)


@dataclass(frozen=True)
class TraceEvent:
    ts: int                         # ターゲットの時計（単調）
    kind: str                       # KINDS のどれか
    name: str                       # 形式での名前（"TASK_START_EXEC"・"thread_switched_in" 等）
    task: int | None = None         # かかわるタスク（形式の ID・TCB の番地）
    args: dict = field(default_factory=dict)
    text: str | None = None

    def to_dict(self) -> dict:
        out: dict = {"ts": self.ts, "kind": self.kind, "name": self.name}
        if self.task is not None:
            out["task"] = self.task
        if self.args:
            out["args"] = self.args
        if self.text is not None:
            out["text"] = self.text
        return out


@dataclass
class TaskInfo:
    id: int
    name: str | None = None
    priority: int | None = None
    stack_base: int | None = None
    stack_size: int | None = None

    def to_dict(self) -> dict:
        return {k: v for k, v in vars(self).items() if v is not None}


@dataclass
class Trace:
    """1 つの形式の解読の結果（流れの途中でも、そこまでの分）。"""
    format: str
    events: list[TraceEvent] = field(default_factory=list)
    tasks: dict[int, TaskInfo] = field(default_factory=dict)
    ts_hz: float | None = None      # ターゲットの時計の速さ
    cpu_hz: float | None = None
    system: dict = field(default_factory=dict)    # システムの説明（形式が送るもの）
    lost: int = 0                   # あふれ・解読の誤りで失った出来事の数（分かる範囲）
    errors: int = 0                 # 解読できなかったかたまりの数
    notes: list[str] = field(default_factory=list)
    idle: set[int] = field(default_factory=set)   # アイドルのタスク（分かれば）

    def task(self, task_id: int) -> TaskInfo:
        return self.tasks.setdefault(task_id, TaskInfo(task_id))

    def names(self) -> dict[int, str]:
        return {t.id: t.name for t in self.tasks.values() if t.name}

    def summary(self) -> dict:
        counts: dict[str, int] = {}
        for e in self.events:
            counts[e.kind] = counts.get(e.kind, 0) + 1
        out = {"format": self.format, "events": len(self.events), "kinds": counts,
               "tasks": [t.to_dict() for t in sorted(self.tasks.values(), key=lambda t: t.id)],
               "lost": self.lost, "errors": self.errors, "notes": list(self.notes)}
        if self.events:
            out.update(ts_first=self.events[0].ts, ts_last=self.events[-1].ts)
            if self.ts_hz:
                out["duration_s"] = (self.events[-1].ts - self.events[0].ts) / self.ts_hz
        for key in ("ts_hz", "cpu_hz"):
            if getattr(self, key):
                out[key] = getattr(self, key)
        if self.system:
            out["system"] = self.system
        return out


class TargetClock:
    """ターゲットの時計 → デバイスのタイムベース。1 つの対応点（anchor_ts のときタイムベース anchor_t）と速さで写す。
    対応点の不確かさ（タイムベース）はそのまま各出来事の不確かさになる（速さの誤差は含まない）。"""

    def __init__(self, ts_hz: float, timebase_hz: float, anchor_ts: int, anchor_t: int, uncertainty: float = 0.0):
        self.ratio = timebase_hz / ts_hz
        self.anchor_ts, self.anchor_t = anchor_ts, anchor_t
        self.uncertainty = uncertainty

    def to_timebase(self, ts: int) -> int:
        return self.anchor_t + round((ts - self.anchor_ts) * self.ratio)


def task_events(events: Iterable[TraceEvent], clock: TargetClock) -> list[TaskEvent]:
    """タスクの出入り（task_in / task_out / task_create / task_delete / idle / overflow）→ 時間軸の出来事（タイムベース）。
    idle はどのタスクでもない時間なので、直前のタスクの task_out にする（アイドルのタスクを持つ RTOS はその task_in が来る）。"""
    out: list[TaskEvent] = []
    current: int | None = None
    for e in events:
        t = clock.to_timebase(e.ts)
        if e.kind == "task_in" and e.task is not None:
            out.append(TaskEvent(t, "task_in", e.task))
            current = e.task
        elif e.kind == "task_out" and e.task is not None:
            out.append(TaskEvent(t, "task_out", e.task))
            current = None if current == e.task else current
        elif e.kind == "idle" and current is not None:
            out.append(TaskEvent(t, "task_out", current))
            current = None
        elif e.kind == "task_create" and e.task is not None:
            out.append(TaskEvent(t, "create", e.task))
        elif e.kind == "task_delete" and e.task is not None:
            out.append(TaskEvent(t, "delete", e.task))
        elif e.kind == "overflow":
            out.append(TaskEvent(t, "overflow"))
            current = None
    return out


def activations(events: Iterable[TraceEvent], clock: TargetClock) -> list[Activation]:
    """isr_enter / isr_exit の入れ子 → 割り込みの回（fprobe.profile.exceptions.Activation。タイムベース）。
    出口の無い入口・入口の無い出口は数えない。番号の分からない割り込みは 0。"""
    out: list[Activation] = []
    stack: list[list[int]] = []                 # [番号, 入った時刻, 横取りした時間]
    for e in events:
        t = clock.to_timebase(e.ts)
        if e.kind == "isr_enter":
            stack.append([int(e.args.get("number", 0)), t, 0])
        elif e.kind == "isr_exit" and stack:
            number, start, nested = stack.pop()
            inclusive = t - start
            out.append(Activation(number, start, t, inclusive, inclusive - nested))
            if stack:
                stack[-1][2] += inclusive
        elif e.kind == "overflow":
            stack.clear()
    return out


def write_trace(rec, trace: Trace, clock: TargetClock, events: Iterable[TraceEvent] | None = None) -> int:
    """出来事を記録の stream "trace" に（kind = 種類、a = タスク、body = 形式の名前・値・本文・ターゲットの時刻）。
    書いた数を返す。"""
    names = trace.names()
    rows = []
    for e in (trace.events if events is None else events):
        body = {"format": trace.format, **e.to_dict()}
        if e.task is not None and e.task in names:
            body["task_name"] = names[e.task]
        rows.append((STREAM, e.kind, clock.to_timebase(e.ts), e.task or 0, 0, body, None, clock.uncertainty or None))
    rec.add_many(rows)
    return len(rows)


def unwrap32(previous: int | None, value: int) -> int:
    """32 ビットで送られた時刻を、前の値（単調な 64 ビット）から続く値にする。"""
    if previous is None:
        return value
    return previous + ((value - previous) & 0xFFFFFFFF)

