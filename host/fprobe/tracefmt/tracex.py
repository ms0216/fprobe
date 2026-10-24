"""Eclipse ThreadX（旧 Azure RTOS）の TraceX のイベントの記録（ターゲットの RAM の循環バッファ）の解読（TRX-01・
TRX-02）。出典: eclipse-threadx/threadx common/inc/tx_trace.h、tx_trace_enable.c、tx_trace_object_register.c。

バッファ（`tx_trace_enable(buf, size, registry_entries)` が作る）= 見出し 48 バイト + オブジェクトの登録 48 バイト ×
N + 出来事 32 バイト × M。すべてターゲットのエンディアンの 32 ビット（見出しの ID 0x54585442 "TXTB" で判定）。
ポインタはターゲットの番地で、見出しの trace_base_address を引くとバッファの中の位置。

- 出来事: thread_pointer（その時の文脈: スレッドの番地、0xFFFFFFFF = 割り込みの中、0xF0F0F0F0 = 初期化、
  0 = 未使用）、priority、event_id、time_stamp（見出しの timer_valid_mask のビットだけ）、info1〜4。
- 循環: current_pointer が次に書く（= 一番古い）位置。その位置の thread_pointer が 0 でなければ一周済みで、
  順は [current … 終わり] + [始め … current)。走行中に読むと current の近くは書きかけのことがある（止めて読む）。
- スレッドの切り替えは出来事として無い。推定して足さず、各出来事の文脈（thread_pointer）を args.context に入れる
  （文脈が変わった所を切り替えとみなす見方はプラグイン fprobe.plugins.heuristics.tracex_context_switches）。
- 時刻の速さは分からない（既定は出来事ごとに 1 増える模擬の時刻、Cortex-M のポートは DWT の CYCCNT）。ts_hz で渡す。
"""

from __future__ import annotations

import struct

from .model import Trace, TraceEvent

__all__ = ["HEADER_ID", "HEADER_SIZE", "TraceXError", "decode_tracex", "header_info"]

HEADER_ID = 0x54585442
HEADER_SIZE = 48
ENTRY_SIZE = 32
ISR_CONTEXT, INIT_CONTEXT = 0xFFFFFFFF, 0xF0F0F0F0
USER_EVENT_START = 4096
_OBJECT_TYPES = {1: "thread", 2: "timer", 3: "queue", 4: "semaphore", 5: "mutex", 6: "event_flags",
                 7: "block_pool", 8: "byte_pool"}
_EVENTS = {1: "THREAD_RESUME", 2: "THREAD_SUSPEND", 3: "ISR_ENTER", 4: "ISR_EXIT", 5: "TIME_SLICE", 6: "RUNNING",
           40: "INTERRUPT_CONTROL", 100: "THREAD_CREATE", 101: "THREAD_DELETE", 102: "THREAD_ENTRY_EXIT_NOTIFY",
           103: "THREAD_IDENTIFY", 104: "THREAD_INFO_GET", 107: "THREAD_PREEMPTION_CHANGE",
           108: "THREAD_PRIORITY_CHANGE", 109: "THREAD_RELINQUISH", 110: "THREAD_RESET", 111: "THREAD_RESUME_API",
           112: "THREAD_SLEEP", 113: "THREAD_STACK_ERROR_NOTIFY", 114: "THREAD_SUSPEND_API", 115: "THREAD_TERMINATE",
           116: "THREAD_TIME_SLICE_CHANGE", 117: "THREAD_WAIT_ABORT", 120: "TIME_GET", 121: "TIME_SET",
           122: "TIMER_ACTIVATE", 123: "TIMER_CHANGE", 124: "TIMER_CREATE", 125: "TIMER_DEACTIVATE",
           126: "TIMER_DELETE"}
_POOL = ("ALLOCATE", "POOL_CREATE", "POOL_DELETE", "POOL_INFO_GET", "POOL_PERFORMANCE_INFO_GET",
         "POOL_PERFORMANCE_SYSTEM_INFO_GET", "POOL_PRIORITIZE", "RELEASE")
_INFO = ("INFO_GET", "PERFORMANCE_INFO_GET", "PERFORMANCE_SYSTEM_INFO_GET")
_GROUPS = ((10, "BLOCK", _POOL), (20, "BYTE", _POOL),
           (30, "EVENT_FLAGS", ("CREATE", "DELETE", "GET", *_INFO, "SET", "SET_NOTIFY")),
           (50, "MUTEX", ("CREATE", "DELETE", "GET", *_INFO, "PRIORITIZE", "PUT")),
           (60, "QUEUE", ("CREATE", "DELETE", "FLUSH", "FRONT_SEND", *_INFO, "PRIORITIZE", "RECEIVE", "SEND",
                          "SEND_NOTIFY")),
           (80, "SEMAPHORE", ("CEILING_PUT", "CREATE", "DELETE", "GET", *_INFO, "PRIORITIZE", "PUT", "PUT_NOTIFY")))
for _base, _kind, _names in _GROUPS:
    for _i, _n in enumerate(_names):
        _EVENTS[_base + _i] = f"{_kind}_{_n}"
_THREAD_STATES = {0: "ready", 1: "completed", 2: "terminated", 3: "suspended", 4: "sleep", 5: "queue", 6: "semaphore",
                  7: "event_flags", 8: "block_memory", 9: "byte_memory", 10: "io_driver", 11: "file", 12: "tcp_ip",
                  13: "mutex", 14: "priority_change"}


class TraceXError(Exception):
    pass


def _order(data: bytes) -> str:
    if len(data) < HEADER_SIZE:
        raise TraceXError("TraceX の見出し（48 バイト）より短い")
    if struct.unpack_from("<I", data)[0] == HEADER_ID:
        return "<"
    if struct.unpack_from(">I", data)[0] == HEADER_ID:
        return ">"
    raise TraceXError("TraceX の見出しの ID（0x54585442 \"TXTB\"）が無い: tx_trace_enable を呼んでいないか番地が違う")


def header_info(data: bytes) -> dict:
    """見出し → mask・各ポインタ（ターゲットの番地）・オブジェクトの名前の長さ・全体の大きさ。"""
    e = _order(data)
    (_, mask, base, reg_start, _, name_size, reg_end, buf_start, buf_end, current) = struct.unpack_from(
        e + "IIIIHHIIII", data)
    return {"order": e, "mask": mask, "base": base, "registry_start": reg_start, "registry_end": reg_end,
            "name_size": name_size, "buffer_start": buf_start, "buffer_end": buf_end, "current": current,
            "size": buf_end - base}


def decode_tracex(data: bytes, ts_hz: float | None = None) -> Trace:
    """バッファ全体（見出しから buffer_end まで）→ Trace。"""
    h = header_info(data)
    e, base = h["order"], h["base"]
    if h["size"] > len(data):
        raise TraceXError(f"バッファが短い（{len(data)} バイト。見出しでは {h['size']} バイト）")
    trace = Trace("tracex", ts_hz=ts_hz)
    if ts_hz is None:
        trace.notes.append("時刻の速さが分からない（ts_hz）: 時刻は TX_TRACE_TIME_SOURCE の生の値")
    objects: dict[int, tuple[str, str]] = {}                      # 番地 → (種類, 名前)
    entry_size = 16 + h["name_size"]
    for off in range(h["registry_start"] - base, h["registry_end"] - base, entry_size):
        available, kind, r1, r2, pointer, p1, p2 = struct.unpack_from(e + "BBBBIII", data, off)
        if pointer == 0 or kind not in _OBJECT_TYPES:
            continue
        raw = data[off + 16:off + 16 + h["name_size"]]
        name = raw.split(b"\0", 1)[0].decode("utf-8", errors="replace")
        objects[pointer] = (_OBJECT_TYPES[kind], name)
        if kind == 1:
            info = trace.task(pointer)
            info.name = name or None
            if r1 & 0x80:
                info.priority = (r1 & 0x7F) << 8 | r2
            info.stack_base, info.stack_size = p1, p2
    start, end = h["buffer_start"] - base, h["buffer_end"] - base
    count = (end - start) // ENTRY_SIZE
    current = (h["current"] - h["buffer_start"]) // ENTRY_SIZE
    if not 0 <= current < max(count, 1):
        raise TraceXError("current_pointer がバッファの外")

    def entry(i: int) -> tuple:
        return struct.unpack_from(e + "8I", data, start + i * ENTRY_SIZE)
    wrapped = count and entry(current)[0] != 0
    order = list(range(current, count)) + list(range(0, current)) if wrapped else list(range(0, current))
    mask = h["mask"] or 0xFFFFFFFF
    previous_ts: int | None = None
    for i in order:
        ctx, prio, event_id, stamp, i1, i2, i3, i4 = entry(i)
        if ctx == 0:
            continue
        stamp &= mask
        ts = trace.unwrap(previous_ts, stamp, mask)
        previous_ts = ts
        name = _EVENTS.get(event_id, f"USER_{event_id}" if event_id >= USER_EVENT_START else f"EVENT_{event_id}")
        args = {"info": [i1, i2, i3, i4], "context": ctx}
        if event_id == 3:
            trace.events.append(TraceEvent(ts, "isr_enter", name, None, {"number": i2, **args}))
            continue
        if event_id == 4:
            trace.events.append(TraceEvent(ts, "isr_exit", name, None, args))
            continue
        if event_id == 1:
            trace.events.append(TraceEvent(ts, "task_ready", name, i1, {"state": _THREAD_STATES.get(i2, i2), **args}))
        elif event_id == 2:
            trace.events.append(TraceEvent(ts, "task_block", name, i1, {"state": _THREAD_STATES.get(i2, i2), **args}))
        elif event_id == 100:
            trace.task(i1).priority = i2
            trace.events.append(TraceEvent(ts, "task_create", name, i1,
                                           {"priority": i2, "stack": i3, "stack_size": i4, "context": ctx}))
        elif event_id in (101, 115):
            trace.events.append(TraceEvent(ts, "task_delete", name, i1, args))
        elif event_id >= USER_EVENT_START:
            trace.events.append(TraceEvent(ts, "marker", name, None, {"event": event_id, **args}))
        elif event_id in (5, 6):
            trace.events.append(TraceEvent(ts, "info", name, None, args))
        else:
            target = objects.get(i1)
            if target is not None:
                args["object"] = target[1] or f"0x{i1:08X}"
            trace.events.append(TraceEvent(ts, "object", name, ctx if ctx not in (ISR_CONTEXT, INIT_CONTEXT) else None,
                                           args))
    trace.system = {"objects": {f"0x{p:08X}": {"type": t, "name": n} for p, (t, n) in objects.items()}}
    trace.notes.append("TraceX にスレッドの切り替えの出来事は無い: 実行中のスレッドの区間は出さない（各出来事の文脈は"
                       " args.context）")
    if wrapped:
        trace.notes.append(f"循環バッファが一周している: 直近の {count} 件だけ")
    return trace
