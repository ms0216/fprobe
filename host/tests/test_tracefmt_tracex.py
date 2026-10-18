"""ThreadX の TraceX のバッファの解読（fprobe.tracefmt.tracex）。バッファは tx_trace_enable・TX_TRACE_IN_LINE_INSERT と
同じ並びで作る。"""

import struct

import pytest

from fprobe.tracefmt.tracex import ISR_CONTEXT, TraceXError, decode_tracex, header_info

BASE = 0x2000_8000
T1, T2 = 0x2000_0100, 0x2000_0200


def image(events: list[tuple], registry: list[tuple], entries: int = 8, current: int | None = None,
          order: str = "<", mask: int = 0xFFFFFFFF) -> bytes:
    name_size = 32
    reg_start = BASE + 48
    reg_end = reg_start + len(registry) * (16 + name_size)
    buf_start, buf_end = reg_end, reg_end + entries * 32
    data = bytearray(buf_end - BASE)
    for i, (kind, pointer, prio, p1, p2, name) in enumerate(registry):
        off = 48 + i * (16 + name_size)
        r1, r2 = (0x80 | prio >> 8, prio & 0xFF) if kind == 1 else (0, 0)
        struct.pack_into(order + "BBBBIII", data, off, 0, kind, r1, r2, pointer, p1, p2)
        data[off + 16:off + 16 + len(name)] = name.encode()
    for i, ev in enumerate(events):
        struct.pack_into(order + "8I", data, buf_start - BASE + (i % entries) * 32, *ev)
    cur = len(events) % entries if current is None else current
    struct.pack_into(order + "IIIIHHIIIIIII", data, 0, 0x54585442, mask, BASE, reg_start, 0, name_size, reg_end,
                     buf_start, buf_end, buf_start + cur * 32, 0xAAAAAAAA, 0xBBBBBBBB, 0xCCCCCCCC)
    return bytes(data)


REGISTRY = [(1, T1, 5, 0x2000_1000, 1024, "sensor"), (1, T2, 10, 0x2000_2000, 2048, "logger"),
            (4, 0x2000_3000, 0, 0, 0, "sem_rx")]


def ev(ctx, event_id, ts, *info, prio=0x80000005):
    info = list(info) + [0] * (4 - len(info))
    return (ctx, prio, event_id, ts, *info)


def test_registry_and_events_in_order():
    events = [ev(T1, 100, 10, T2, 10, 0x2000_2000, 2048), ev(T1, 83, 20, 0x2000_3000, 0xFFFFFFFF, 0),
              ev(T1, 2, 30, T1, 6), ev(T2, 6, 35), ev(ISR_CONTEXT, 3, 40, 0, 27), ev(ISR_CONTEXT, 4, 45, 0, 27),
              ev(T2, 1, 50, T1, 6), ev(T1, 4096, 60, 1, 2, 3, 4)]
    trace = decode_tracex(image(events, REGISTRY), ts_hz=1e6)
    assert {t.id: (t.name, t.priority, t.stack_size) for t in trace.tasks.values()} == {
        T1: ("sensor", 5, 1024), T2: ("logger", 10, 2048)}
    kinds = [(e.kind, e.ts, e.task) for e in trace.events]
    assert kinds == [("task_in", 10, T1), ("task_create", 10, T2), ("object", 20, T1), ("task_block", 30, T1),
                     ("task_in", 35, T2), ("info", 35, None), ("isr_enter", 40, None), ("isr_exit", 45, None),
                     ("task_ready", 50, T1), ("task_in", 60, T1), ("marker", 60, None)]
    sem = trace.events[2]
    assert sem.name == "SEMAPHORE_GET" and sem.args["object"] == "sem_rx"
    assert trace.events[3].args["state"] == "semaphore" and trace.events[6].args["number"] == 27
    assert trace.ts_hz == 1e6


def test_a_wrapped_ring_starts_at_current():
    events = [ev(T1, 6, t) for t in range(1, 11)]                  # 10 件を 8 件のリングに: 3..10 が残る
    trace = decode_tracex(image(events, REGISTRY, entries=8))
    assert [e.ts for e in trace.events if e.kind == "info"] == list(range(3, 11))
    assert any("一周" in n for n in trace.notes)


def test_timestamps_wrap_with_the_mask():
    events = [ev(T1, 6, 0xFFF0), ev(T1, 6, 0x0010)]
    trace = decode_tracex(image(events, REGISTRY, mask=0xFFFF))
    assert [e.ts for e in trace.events if e.kind == "info"] == [0xFFF0, 0x1_0010]


def test_big_endian_targets():
    trace = decode_tracex(image([ev(T1, 6, 5)], REGISTRY, order=">"))
    assert header_info(image([], REGISTRY, order=">"))["order"] == ">" and trace.tasks[T1].name == "sensor"


def test_missing_header_is_refused():
    with pytest.raises(TraceXError):
        decode_tracex(bytes(64))
    with pytest.raises(TraceXError):
        decode_tracex(image([], REGISTRY)[:60])
