"""カーネルの出来事の解読（U10）: freertos_demo の fprobe_trace.h が ITM に書く記録（見出しはポート 30、中身はポート 29）を、
ポート 31（タスクの切り替え）と同じ並びで読み、だれが（actor）・何に（オブジェクト・タスク）・何をしたかの列にする。
入出力なし。解析は fprobe.rtos.flow。

仕様: docs/superpowers/specs/2026-10-18-u10-u16-trace-hooks-user-events-design.md の 1・2 節。

    trace = kernel_trace(itm_packets(swo_bytes))       # 時刻付きの ITM のパケット（fprobe.rtos.timeline.itm_packets）
    trace.ops                                           # KernelOp の列（並びの順）

actor は記録に入っていない: IPSR が 0 なら、並びでその直前のポート 31 の task_in のタスク。あふれ・解読の誤りの後は
次の task_in まで分からない（None）。そこには kind "lost" の印を入れる（解析は待ち・持ち主の状態を忘れる）。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from ..itm import DecodeError, Instrumentation, Overflow, Packet, Sync
from .timeline import TRACE_PORT, decode_trace_word, name_of

__all__ = ["CLASSES", "EVENTS", "KERNEL_PORT", "OBJECT_EVENTS", "PAYLOAD_PORT", "QUEUE_TYPES", "RECORD_CYCLES",
           "TASK_EVENTS", "WAITS", "WORD_CYCLES", "KernelOp", "KernelTrace", "kernel_trace", "object_table"]

KERNEL_PORT = 30                       # fprobe_trace.h の FPROBE_KERNEL_PORT（見出し）
PAYLOAD_PORT = 29                      # FPROBE_PAYLOAD_PORT（中身）
ARG_MAX = 0x1FFF                       # 引数は 13 ビットで飽和
EVENTS = {1: "ready", 2: "delay", 3: "suspend", 4: "block", 5: "prio_inherit", 6: "prio_disinherit", 7: "prio_set",
          16: "send", 17: "send_failed", 18: "receive", 19: "receive_failed", 20: "peek", 21: "object_create",
          22: "stream_send", 23: "stream_receive", 24: "event_set", 25: "notify", 32: "isr_enter", 33: "isr_exit"}
CLASSES = ("queue", "semaphore", "mutex", "other")               # オブジェクトの語の bit 1..0
WAITS = ("receive", "send", "peek", "notify")                     # block の引数
QUEUE_TYPES = {0: "queue", 1: "mutex", 2: "counting_semaphore", 3: "binary_semaphore", 4: "recursive_mutex"}
TASK_EVENTS = frozenset({"ready", "delay", "suspend", "prio_inherit", "prio_disinherit", "prio_set", "notify"})
OBJECT_EVENTS = frozenset({"block", "send", "send_failed", "receive", "receive_failed", "peek", "object_create",
                           "stream_send", "stream_receive", "event_set"})

# フックの負荷の見積もり（Cortex-M4、FIFO が空いているとき）: freertos_demo.elf の逆アセンブル（fprobe_trace_record は
# 有効の確認・IPSR と PRIMASK・語ごとの FIFO の確認とストアで、中身 2 語の道筋が約 45 命令）と、ポート 31 の 1 語の
# インライン（約 12 命令）から。周辺のレジスタ（PPB）の読み書きの待ちを含めたおおよその値。実機の計測は本流
# （gateware/tests/hil/test_trace_hooks_hw.py）。
WORD_CYCLES = 14                       # ポート 31 の 1 語
RECORD_CYCLES = 36                     # 記録の固定分（呼び出し・確認・割り込みの禁止と戻し）
PAYLOAD_CYCLES = 7                     # 記録の 1 語ごと（FIFO の確認とストア）


@dataclass(frozen=True)
class KernelOp:
    """カーネルの出来事 1 つ。seq は並びの番号（時刻が同じでも前後が決まる）。isr は IPSR（0 = タスクの中）。
    actor はタスクの中なら実行中のタスクの TCB（分からなければ None）。obj は対象のオブジェクト、task は対象のタスク。"""
    t: int
    seq: int
    kind: str
    isr: int = 0
    actor: int | None = None
    arg: int = 0
    obj: int = 0
    obj_class: str | None = None
    task: int = 0
    words: tuple[int, ...] = ()

    @property
    def wait(self) -> str | None:
        """block の待ちの向き（receive / send / peek / notify）。"""
        return WAITS[self.arg] if self.kind == "block" and self.arg < len(WAITS) else None

    @property
    def length(self) -> int | None:
        """object_create のキューの長さ（セマフォは最大の数）。"""
        return self.words[1] if self.kind == "object_create" and len(self.words) > 1 else None

    @property
    def queue_type(self) -> str | None:
        """object_create の ucQueueType の名前。"""
        return QUEUE_TYPES.get(self.arg) if self.kind == "object_create" else None

    @property
    def saturated(self) -> bool:
        return self.arg >= ARG_MAX

    def to_dict(self, names: Mapping[int, str], objects: Mapping[int, dict] | None = None,
                isr_names: Mapping[int, str] | None = None) -> dict:
        out: dict = {"t": self.t, "kind": self.kind,
                     "actor": None if self.isr else name_of(names, self.actor) if self.actor is not None else "?",
                     "isr": None if not self.isr else (isr_names or {}).get(self.isr, self.isr)}
        if self.kind in OBJECT_EVENTS:
            info = (objects or {}).get(self.obj)
            out.update(object=info["name"] if info else (name_of({}, self.obj) if self.obj else None),
                       object_handle=self.obj, **{"class": self.obj_class})
        if self.kind in TASK_EVENTS:
            out.update(task=name_of(names, self.task), task_handle=self.task)
        if self.kind not in ("isr_enter", "isr_exit", "lost", "ready", "delay", "suspend"):
            out["arg"] = self.arg
        if self.wait is not None:
            out["wait"] = self.wait
        if self.kind == "object_create":
            out.update(length=self.length, type=self.queue_type)
        if self.saturated:
            out["saturated"] = True
        return out


@dataclass
class KernelTrace:
    ops: list[KernelOp] = field(default_factory=list)
    lost: int = 0                          # 中身が欠けた・見出しの無い記録の数
    words: dict[str, int] = field(default_factory=lambda: {"task": 0, "kernel": 0, "payload": 0})

    def by_kind(self) -> dict[str, int]:
        return dict(Counter(op.kind for op in self.ops if op.kind != "lost"))


def kernel_trace(packets: Iterable[tuple[int, Packet]], task_port: int = TRACE_PORT, kernel_port: int = KERNEL_PORT,
                 payload_port: int = PAYLOAD_PORT) -> KernelTrace:
    """時刻付きの ITM のパケット → カーネルの出来事。ポート 31 の語は actor を決めるのに使う（出来事には入れない:
    タスクの時間軸は fprobe.rtos.timeline）。"""
    out = KernelTrace()
    current: int | None = None
    pending: tuple[int, int, list[int]] | None = None        # (時刻, 見出し, 集めた中身)
    seq = 0

    def emit(t: int, header: int, payload: list[int]) -> None:
        nonlocal seq
        code, isr, arg = header & 0xFF, header >> 10 & 0x1FF, header >> 19 & ARG_MAX
        kind = EVENTS.get(code, f"event_{code}")
        first = payload[0] if payload else 0
        obj = cls = None
        task = 0
        if kind in OBJECT_EVENTS:
            obj, cls = first & ~3 & 0xFFFF_FFFF, CLASSES[first & 3]
            if kind == "object_create" and QUEUE_TYPES.get(arg, "").endswith("mutex"):
                cls = "mutex"                                 # 作った時点では pcHead がまだ NULL でない
        elif kind in TASK_EVENTS:
            task = first
        out.ops.append(KernelOp(t, seq, kind, isr, None if isr else current, arg, obj or 0, cls, task, tuple(payload)))
        seq += 1

    def lose(t: int) -> None:
        nonlocal current, pending, seq
        if pending is not None:
            out.lost += 1
            pending = None
        current = None
        out.ops.append(KernelOp(t, seq, "lost"))
        seq += 1
    for t, p in packets:
        if isinstance(p, Instrumentation):
            if p.size != 4:
                continue
            if p.port == task_port:
                out.words["task"] += 1
                kind, handle = decode_trace_word(p.value)
                if kind == "task_in":
                    current = handle
            elif p.port == kernel_port:
                out.words["kernel"] += 1
                if pending is not None:
                    out.lost += 1                             # 前の記録の中身が足りない
                count = p.value >> 8 & 3
                pending = None
                if count:
                    pending = (t, p.value, [])
                else:
                    emit(t, p.value, [])
            elif p.port == payload_port:
                out.words["payload"] += 1
                if pending is None:
                    out.lost += 1                             # 見出しの無い中身
                    continue
                pending[2].append(p.value)
                if len(pending[2]) == pending[1] >> 8 & 3:
                    emit(*pending)
                    pending = None
        elif isinstance(p, Overflow) or isinstance(p, DecodeError) or (isinstance(p, Sync) and p.skipped):
            lose(t)
    return out


def object_table(trace: KernelTrace, objects: Mapping[int, dict] | None = None) -> dict[int, dict]:
    """オブジェクトの番地 → {name, kind, length}。objects（rtos.objects の読み）が先、無ければ object_create の出来事、
    どちらも無ければ番地の名前と記録の種類（class）。"""
    table = {h: dict(v) for h, v in (objects or {}).items()}
    for op in trace.ops:
        if op.kind == "object_create":
            row = table.setdefault(op.obj, {"name": name_of({}, op.obj), "kind": op.queue_type or op.obj_class})
            row.setdefault("length", op.length)
            if row.get("length") is None:
                row["length"] = op.length
    return table
