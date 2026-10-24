"""Zephyr の CTF トレース（subsys/tracing/ctf）の解読（ZEP-01・ZEP-02・ZEP-03・PFT-04）。

Zephyr の CTF は「出来事の記録が隙間なく並ぶだけ」の流れ（packet header・magic・長さの欄は無い。整数はバイト境界・
リトルエンディアン）。1 つの記録 = 見出し（時刻と ID）+ 出来事ごとの欄。見出しは版で違う:

| 形（header） | 見出し | Zephyr |
|---|---|---|
| ts32_id8  | u32 時刻（ns、約 4.29 秒で一周）+ u8 ID   | 4.3 まで |
| ts32_id16 | u32 時刻 + u16 ID                        | 4.4 |
| ts64_id16 | u64 時刻（ns）+ u16 ID                   | main（2026-05 以降） |
| id16      | u16 ID（CONFIG_TRACING_CTF_TIMESTAMP=n）  | 時刻なし |

欄の並びはビルドの metadata（TSDL）が正: `metadata` を渡すとそれを読む（見出しも metadata の event_header から）。
見出しの形は版で違うので既定を持たない（metadata の event_header か、使う側が header で明示する）。
metadata を渡さなければ組み込みの表（スレッド・割り込み・アイドル・セマフォ・ミューテックス・named_event 等の主な出来事。
ID は 4.3〜main で同じ）を使う。長さの欄が無いので、表に無い ID が来たらそこから先は読めない（同期を失う）。

転送: UART・USB・RTT・ITM は同じバイト列。RAM の backend（`ram_tracing[CONFIG_RAM_TRACING_BUFFER_SIZE]`）は
リングでなく満ちたら止まる線形のバッファで、末尾は 0 埋め（ID 0 は使われない）なので、ID 0 の記録で終わりとする。
出典: zephyr subsys/tracing/ctf/tsdl/metadata・ctf_top.h、scripts/tracing/trace_viewer.py・ctf2perfetto.py。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .model import Trace, TraceEvent

__all__ = ["CtfDecoder", "CtfError", "CtfMetadata", "HEADERS", "decode_ctf", "parse_tsdl"]

HEADERS = ("ts32_id8", "ts32_id16", "ts64_id16", "id16")
NS_HZ = 1e9


class CtfError(Exception):
    pass


@dataclass(frozen=True)
class CtfField:
    name: str
    size: int                    # ビット（文字列は 8）
    signed: bool = False
    count: int | None = None     # 配列の長さ（文字列の配列は固定長の文字列）
    string: bool = False

    @property
    def nbytes(self) -> int:
        return self.size // 8 * (self.count or 1)


@dataclass(frozen=True)
class CtfEvent:
    id: int
    name: str
    fields: tuple[CtfField, ...]


@dataclass
class CtfMetadata:
    header: tuple[CtfField, ...]
    events: dict[int, CtfEvent] = field(default_factory=dict)
    big_endian: bool = False


_INT = {"int8_t": (8, True), "uint8_t": (8, False), "uint16_t": (16, False), "int16_t": (16, True),
        "uint32_t": (32, False), "int32_t": (32, True), "uint64_t": (64, False), "int64_t": (64, True)}


def parse_tsdl(text: str, header: str | None = None) -> CtfMetadata:
    """TSDL（CTF 1.8）の、Zephyr が使う部分: typealias integer、struct event_header、trace の byte_order、event。
    見出しは struct event_header。それが無ければ header（HEADERS のどれか）で明示する（無ければ誤り）。"""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"//[^\n]*", "", text)
    types: dict[str, tuple[int, bool, bool]] = {k: (v[0], v[1], False) for k, v in _INT.items()}
    for m in re.finditer(r"typealias\s+integer\s*\{([^}]*)\}\s*:=\s*(\w+)\s*;", text):
        props = dict(re.findall(r"(\w+)\s*=\s*([\w\-]+)", m[1]))
        types[m[2]] = (int(props.get("size", 8)), props.get("signed", "false") == "true",
                       props.get("encoding", "").upper() in ("ASCII", "UTF8"))

    def fields(body: str) -> tuple[CtfField, ...]:
        out = []
        for fm in re.finditer(r"(\w+)\s+(\w+)\s*(?:\[\s*(\d+)\s*\])?\s*;", body):
            kind, name, count = fm[1], fm[2], fm[3]
            if kind not in types:
                raise CtfError(f"TSDL: 知らない型 {kind}（{name}）")
            size, signed, is_string = types[kind]
            out.append(CtfField(name, size, signed, int(count) if count else None, is_string))
        return tuple(out)
    header_m = re.search(r"struct\s+event_header\s*\{([^}]*)\}", text)
    if header_m:
        header_fields = fields(header_m[1])
    elif header is not None:
        header_fields = _header(header)
    else:
        raise CtfError(f"metadata に struct event_header が無い: 見出しの形を header（{' / '.join(HEADERS)}）で明示する")
    order = re.search(r"byte_order\s*=\s*(\w+)", text)
    meta = CtfMetadata(header_fields, big_endian=bool(order and order[1] in ("be", "network")))
    for block in _blocks(text, "event"):
        name = re.search(r"name\s*=\s*\"?([\w.]+)\"?\s*;", block)
        event_id = re.search(r"\bid\s*=\s*(0[xX][0-9a-fA-F]+|\d+)\s*;", block)
        body = re.search(r"fields\s*:=\s*struct\s*\{(.*)\}", block, flags=re.S)
        if name is None or event_id is None:
            continue
        number = int(event_id[1], 0)
        meta.events[number] = CtfEvent(number, name[1], fields(body[1]) if body else ())
    if not meta.events:
        raise CtfError("TSDL に event が無い")
    return meta


def _blocks(text: str, keyword: str) -> list[str]:
    """`keyword { … }` の中身（入れ子の波括弧を数えて閉じる）。"""
    out = []
    for m in re.finditer(rf"\b{keyword}\s*\{{", text):
        depth, i = 1, m.end()
        while i < len(text) and depth:
            depth += {"{": 1, "}": -1}.get(text[i], 0)
            i += 1
        out.append(text[m.end():i - 1])
    return out


def _header(kind: str) -> tuple[CtfField, ...]:
    if kind not in HEADERS:
        raise CtfError(f"header は {' / '.join(HEADERS)}")
    ts = {"ts32_id8": 32, "ts32_id16": 32, "ts64_id16": 64}.get(kind)
    idf = CtfField("id", 8 if kind == "ts32_id8" else 16)
    return ((CtfField("timestamp", ts),) if ts else ()) + (idf,)


# 組み込みの表（Zephyr の metadata の主な出来事。ID と欄は 4.3〜main で同じ）
_T, _S = "uint32_t thread_id; ctf_bounded_string_t name[20];", "uint32_t id;"
_BUILTIN = {
    0x10: ("thread_switched_out", _T), 0x11: ("thread_switched_in", _T),
    0x12: ("thread_priority_set", _T + " int8_t prio;"), 0x13: ("thread_create", _T), 0x14: ("thread_abort", _T),
    0x15: ("thread_suspend", _T), 0x16: ("thread_resume", _T), 0x17: ("thread_ready", _T),
    0x18: ("thread_pending", _T),
    0x19: ("thread_info", _T + " uint32_t stack_base; uint32_t stack_size;"), 0x1A: ("thread_name_set", _T),
    0x1B: ("isr_enter", ""), 0x1C: ("isr_exit", ""), 0x1D: ("isr_exit_to_scheduler", ""), 0x1E: ("idle", ""),
    0x21: ("semaphore_init", _S + " int32_t ret;"), 0x22: ("semaphore_give_enter", _S),
    0x23: ("semaphore_give_exit", _S), 0x24: ("semaphore_take_enter", _S + " uint32_t timeout;"),
    0x25: ("semaphore_take_blocking", _S + " uint32_t timeout;"),
    0x26: ("semaphore_take_exit", _S + " uint32_t timeout; int32_t ret;"), 0x27: ("semaphore_reset", _S),
    0x28: ("mutex_init", _S + " int32_t ret;"), 0x29: ("mutex_lock_enter", _S + " uint32_t timeout;"),
    0x2A: ("mutex_lock_blocking", _S + " uint32_t timeout;"),
    0x2B: ("mutex_lock_exit", _S + " uint32_t timeout; int32_t ret;"), 0x2C: ("mutex_unlock_enter", _S),
    0x2D: ("mutex_unlock_exit", _S + " int32_t ret;"), 0x2E: ("timer_init", _S),
    0x2F: ("timer_start", _S + " uint32_t duration; uint32_t period;"), 0x30: ("timer_stop", _S),
    0x34: ("user_mode_enter", _T), 0x35: ("thread_wakeup", _T),
    0x62: ("named_event", "ctf_bounded_string_t name[20]; uint32_t arg0; uint32_t arg1;"),
    0x7F: ("k_sleep_enter", "uint32_t timeout;"), 0x80: ("k_sleep_exit", "uint32_t timeout; int32_t ret;"),
}


def builtin_metadata(header: str) -> CtfMetadata:
    lines = ["typealias integer { size = 8; align = 8; signed = false; encoding = ASCII; } := ctf_bounded_string_t;"]
    for event_id, (name, body) in _BUILTIN.items():
        fields = f" fields := struct {{ {body} }};" if body else ""
        lines.append(f"event {{ name = {name}; id = {event_id:#x};{fields} }};")
    meta = parse_tsdl("\n".join(lines), header)
    return meta


_THREAD_KINDS = {"thread_switched_in": "task_in", "thread_switched_out": "task_out", "thread_create": "task_create",
                 "thread_abort": "task_delete", "thread_ready": "task_ready", "thread_resume": "task_ready",
                 "thread_sched_ready": "task_ready", "thread_sched_resume": "task_ready",
                 "thread_pending": "task_block", "thread_suspend": "task_block", "thread_sched_pend": "task_block",
                 "thread_sched_suspend": "task_block", "thread_info": "task_info", "thread_name_set": "task_info",
                 "thread_priority_set": "task_info", "thread_sched_priority_set": "task_info",
                 "isr_enter": "isr_enter", "isr_exit": "isr_exit", "isr_exit_to_scheduler": "isr_exit",
                 "idle": "idle", "named_event": "marker"}
# カーネルオブジェクトの操作の出来事（kind object）: Zephyr v4.4.0 の subsys/tracing/ctf/tsdl/metadata にある名前の表
# （名前の接頭辞では決めない。表に無い名前は other）
_OBJECT_EVENTS = frozenset("""
condvar_broadcast_enter condvar_broadcast_exit condvar_init condvar_signal_blocking condvar_signal_enter
condvar_signal_exit condvar_wait_enter condvar_wait_exit event_init event_post_enter event_post_exit
event_wait_blocking event_wait_enter event_wait_exit k_sleep_enter k_sleep_exit mbox_async_put_enter
mbox_async_put_exit mbox_data_get mbox_get_blocking mbox_get_enter mbox_get_exit mbox_init
mbox_message_put_blocking mbox_message_put_enter mbox_message_put_exit mbox_put_enter mbox_put_exit
mem_slab_alloc_blocking mem_slab_alloc_enter mem_slab_alloc_exit mem_slab_free_enter mem_slab_free_exit
mem_slab_init msgq_alloc_init_enter msgq_alloc_init_exit msgq_cleanup_enter msgq_cleanup_exit msgq_get_blocking
msgq_get_enter msgq_get_exit msgq_init msgq_peek msgq_purge msgq_put_blocking msgq_put_enter msgq_put_exit
msgq_put_front_blocking msgq_put_front_enter msgq_put_front_exit mutex_init mutex_lock_blocking mutex_lock_enter
mutex_lock_exit mutex_unlock_enter mutex_unlock_exit poll_enter poll_event_init poll_exit poll_signal_check
poll_signal_init poll_signal_raise poll_signal_reset semaphore_give_enter semaphore_give_exit semaphore_init
semaphore_reset semaphore_take_blocking semaphore_take_enter semaphore_take_exit timer_expiry_enter
timer_expiry_exit timer_init timer_start timer_status_sync_blocking timer_status_sync_enter
timer_status_sync_exit timer_stop timer_stop_fn_expiry_enter timer_stop_fn_expiry_exit
work_cancel_delayable_enter work_cancel_delayable_exit work_cancel_delayable_sync_enter
work_cancel_delayable_sync_exit work_cancel_enter work_cancel_exit work_cancel_sync_blocking
work_cancel_sync_enter work_cancel_sync_exit work_delayable_init work_flush_blocking work_flush_delayable_enter
work_flush_delayable_exit work_flush_enter work_flush_exit work_init work_poll_cancel_enter
work_poll_cancel_exit work_poll_init_enter work_poll_init_exit work_poll_submit_enter work_poll_submit_exit
work_poll_submit_to_queue_blocking work_poll_submit_to_queue_enter work_poll_submit_to_queue_exit
work_queue_drain_enter work_queue_drain_exit work_queue_init work_queue_start_enter work_queue_start_exit
work_queue_stop_blocking work_queue_stop_enter work_queue_stop_exit work_queue_unplug_enter
work_queue_unplug_exit work_reschedule_enter work_reschedule_exit work_reschedule_for_queue_enter
work_reschedule_for_queue_exit work_schedule_enter work_schedule_exit work_schedule_for_queue_enter
work_schedule_for_queue_exit work_submit_enter work_submit_exit work_submit_to_queue_enter
work_submit_to_queue_exit
""".split())


class CtfDecoder:
    """流れ → Trace。metadata が無ければ組み込みの表と header の形（既定なし: 版で違う）。stop_at_zero は RAM の
    backend の 0 埋めの終わり。"""

    def __init__(self, metadata: CtfMetadata | None = None, header: str | None = None, stop_at_zero: bool = False):
        if metadata is None and header is None:
            raise CtfError(f"metadata（ビルドの TSDL）か、見出しの形 header（{' / '.join(HEADERS)}）を明示する")
        self.meta = metadata or builtin_metadata(header)
        self.trace = Trace("ctf", ts_hz=NS_HZ if any(f.name == "timestamp" for f in self.meta.header) else None)
        self._order = ">" if self.meta.big_endian else "<"
        self._buf = bytearray()
        self._ts: int | None = None
        self._count = 0
        self.synced = True
        self.ended = False
        self.stop_at_zero = stop_at_zero
        if not any(f.name == "timestamp" for f in self.meta.header):
            self.trace.notes.append("時刻の無い形（id16）: 出来事の順だけが分かる")

    def feed(self, data: bytes) -> list[TraceEvent]:
        if not self.synced or self.ended:
            return []
        self._buf += data
        out = []
        pos = 0
        buf = bytes(self._buf)
        while True:
            parsed = self._record(buf, pos)
            if parsed is None:
                break
            if parsed == "end":
                self.ended = True
                break
            if isinstance(parsed, str):
                self.synced = False
                self.trace.errors += 1
                self.trace.notes.append(parsed)
                break
            event, pos = parsed
            out.append(event)
            self.trace.events.append(event)
        del self._buf[:pos]
        if not self.synced:
            self._buf.clear()
        return out

    def _ints(self, buf: bytes, pos: int, f: CtfField):
        size = f.size // 8
        if f.string:
            raw = buf[pos:pos + f.nbytes]
            return raw.split(b"\0", 1)[0].decode("utf-8", errors="replace"), pos + f.nbytes
        values = []
        for _ in range(f.count or 1):
            values.append(int.from_bytes(buf[pos:pos + size], "big" if self._order == ">" else "little",
                                         signed=f.signed))
            pos += size
        return (values if f.count else values[0]), pos

    def _record(self, buf: bytes, pos: int):
        head_size = sum(f.nbytes for f in self.meta.header)
        if len(buf) - pos < head_size:
            return None
        values = {}
        p = pos
        for f in self.meta.header:
            values[f.name], p = self._ints(buf, p, f)
        event_id = values.get("id", 0)
        if event_id == 0 and self.stop_at_zero:
            return "end"
        event = self.meta.events.get(event_id)
        if event is None:
            return (f"表に無い出来事の ID {event_id:#x}（{self._count} 個目の後）: 長さの欄が無いので以後は読めない。"
                    "ビルドの metadata（build/…/tracing/ctf/tsdl/metadata）を渡す")
        if len(buf) - p < sum(f.nbytes for f in event.fields):
            return None
        fields = {}
        for f in event.fields:
            fields[f.name], p = self._ints(buf, p, f)
        self._count += 1
        raw_ts = values.get("timestamp")
        if raw_ts is None:
            ts = self._count
        elif any(f.name == "timestamp" and f.size == 32 for f in self.meta.header):
            ts = self._ts = self.trace.unwrap(self._ts, raw_ts)
        else:
            ts = self._ts = raw_ts
        return self._event(ts, event, fields), p

    def _event(self, ts: int, event: CtfEvent, fields: dict) -> TraceEvent:
        name = event.name
        kind = _THREAD_KINDS.get(name)
        task = fields.get("thread_id")
        if kind is None:
            kind = "object" if name in _OBJECT_EVENTS else "other"
        if task is not None:
            info = self.trace.task(task)
            if fields.get("name") and fields["name"] != "unknown":
                info.name = fields["name"]
            if "prio" in fields:
                info.priority = fields["prio"]
            if "stack_size" in fields:
                info.stack_base, info.stack_size = fields["stack_base"], fields["stack_size"]
        text = None
        if name == "named_event":
            text = fields.get("name")
        args = {k: v for k, v in fields.items() if k not in ("thread_id",)}
        return TraceEvent(ts, kind, name, task, args, text)


def decode_ctf(data: bytes, metadata: CtfMetadata | None = None, header: str | None = None,
               ram_buffer: bool = False) -> Trace:
    decoder = CtfDecoder(metadata, header, stop_at_zero=ram_buffer)
    decoder.feed(data)
    if decoder._buf and decoder.synced and not decoder.ended:
        decoder.trace.notes.append(f"末尾の {len(decoder._buf)} バイトは記録の途中で終わっている")
    return decoder.trace

