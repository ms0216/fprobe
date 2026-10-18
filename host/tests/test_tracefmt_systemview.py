"""SystemView の解読（fprobe.tracefmt.systemview）。パケットは sysview_writer（SEGGER_SYSVIEW.c と同じ符号化）で作る。"""

import pytest
from sysview_writer import Writer, string, varint

from fprobe.rtos.timeline import timeline_from_events
from fprobe.tracefmt.model import TargetClock, activations, task_events
from fprobe.tracefmt.systemview import (SyncNotFound, SystemViewDecoder, decode_systemview, parse_description,
                                        strip_svdat_header)

A, B, IDLE = 0x2000_1000, 0x2000_2000, 0x2000_3000


def scenario() -> Writer:
    w = Writer().start(t=1_000)
    w.task_info(A, 3, "sensor", 1_000).task_info(B, 2, "logger", 1_000).task_info(IDLE, 0, "IDLE", 1_000)
    w.packet(4, varint(w.shrink(IDLE)), 1_100)                         # IDLE 実行
    w.packet(2, varint(27), 1_200).packet(3, b"", 1_260)              # 割り込み（IRQ11 = 例外 27）
    w.packet(6, varint(w.shrink(A)), 1_300)                           # A 準備完了
    w.packet(4, varint(w.shrink(A)), 1_310)                           # A 実行
    w.packet(26, string("value=%d %s") + varint(0) + varint(2) + varint(0xFFFF_FFFE) + varint(0), 1_400)
    w.packet(7, varint(w.shrink(A)) + varint(3), 1_500)               # A 待ち
    w.packet(5, b"", 1_500)
    w.packet(4, varint(w.shrink(IDLE)), 1_520)
    return w


def test_a_stream_decodes_tasks_isrs_prints_and_times():
    trace = decode_systemview(scenario().bytes())
    assert trace.ts_hz == 64_000_000 and trace.system["O"] == "FreeRTOS"
    assert {t.id: (t.name, t.priority) for t in trace.tasks.values()} == {
        A: ("sensor", 3), B: ("logger", 2), IDLE: ("IDLE", 0)}
    assert trace.idle == {IDLE}
    kinds = [(e.kind, e.ts, e.task) for e in trace.events if e.kind in ("task_in", "task_out", "isr_enter", "isr_exit")]
    assert kinds == [("task_in", 1_100, IDLE), ("isr_enter", 1_200, None), ("isr_exit", 1_260, None),
                     ("task_in", 1_310, A), ("task_out", 1_500, A), ("task_in", 1_520, IDLE)]
    [p] = [e for e in trace.events if e.kind == "print"]
    assert p.text == "value=-2 <str@0x00000000>" and p.args["level"] == "info"
    assert next(e for e in trace.events if e.kind == "isr_enter").args["number"] == 27
    assert next(e for e in trace.events if e.kind == "task_block").args["cause"] == 3


def test_the_trace_becomes_a_timeline_and_isr_activations():
    trace = decode_systemview(scenario().bytes())
    clock = TargetClock(trace.ts_hz, 100_000_000, anchor_ts=1_000, anchor_t=5_000_000)
    tl = timeline_from_events(task_events(trace.events, clock), trace.names())
    runs = [(r.task, r.t0, r.t1) for r in tl.runs]
    assert runs[0][0] == "IDLE" and runs[1] == ("sensor", clock.to_timebase(1_310), clock.to_timebase(1_500))
    [act] = activations(trace.events, clock)
    assert act.number == 27 and act.inclusive == clock.to_timebase(1_260) - clock.to_timebase(1_200)


def test_streaming_in_pieces_gives_the_same_events():
    data = scenario().bytes()
    d = SystemViewDecoder()
    for i in range(0, len(data), 3):
        d.feed(data[i:i + 3])
    assert [e.to_dict() for e in d.trace.events] == [e.to_dict() for e in decode_systemview(data).events]


def test_a_post_mortem_ring_starts_in_the_middle_of_a_packet():
    data = scenario().bytes()
    garbage = bytes([0x85, 0x91, 0x07, 0x33])                         # 前のパケットの尻尾
    trace = decode_systemview(garbage + data)
    assert len(trace.events) == len(decode_systemview(data).events)
    assert any("4 バイト" in n for n in trace.notes)


def test_without_sync_it_is_refused():
    with pytest.raises(SyncNotFound):
        decode_systemview(b"\x04\x01\x02" * 10)


def test_svdat_header_is_skipped():
    header = b";\n; Version     SEGGER SystemViewer V2.42\n; Author      Espressif Inc\n;\n"
    assert strip_svdat_header(header + b"\x00\x01") == b"\x00\x01"
    assert len(decode_systemview(header + scenario().bytes()).events) > 5


def test_overflow_counts_lost_events_and_breaks_the_task():
    w = Writer().start(t=0)
    w.packet(4, varint(w.shrink(A)), 10).packet(1, varint(5), 20).packet(1, varint(8), 30)
    trace = decode_systemview(w.bytes())
    assert trace.lost == 5 + 3
    assert [e.kind for e in trace.events][-2:] == ["overflow", "overflow"]


def test_api_events_with_a_description_file():
    desc = """
NamedType Bool 0=false 1=true
TaskState 0xBF 1=dummy
37  k_mutex_init               | Returns %ErrCodePosix
39  k_mutex_lock               mutex=%I timeout=%u ok=%Bool
40  k_thread_name_set          thread=%t name=%s
"""
    events, named = parse_description(desc)
    assert events[39].name == "k_mutex_lock" and named["Bool"] == {0: "false", 1: "true"}
    w = Writer().start(t=0)
    w.task_info(A, 1, "main", 0)
    w.packet(25, varint(w.shrink(0x2000_4000)) + string("lock_a"), 5)
    w.packet(39, varint(w.shrink(0x2000_4000)) + varint(100) + varint(1), 10)
    w.packet(40, varint(w.shrink(A)) + string("main2"), 11)
    w.packet(41, varint(7), 12)
    trace = decode_systemview(w.bytes(), description=desc)
    api = [e for e in trace.events if e.kind == "object"]
    assert api[0].name == "k_mutex_lock" and api[0].text == "mutex=lock_a timeout=100 ok=true"
    assert api[1].text == "thread=main name=main2"
    assert api[2].name == "API41" and api[2].args == {"values": [7]}


def test_marks_print_elf_and_stack_info():
    w = Writer().start(t=0)
    w.packet(31, varint(1) + varint(4) + string("frame"), 1)                     # NAME_MARKER
    w.packet(15, varint(4), 2).packet(16, varint(4), 9)
    w.packet(31, varint(7) + varint(0x0800_1000) + varint(2) + varint(42), 10)   # PRINT_ELF（ERROR）
    w.packet(21, varint(w.shrink(A)) + varint(0x2000_0400) + varint(1024) + varint(300), 11)
    trace = decode_systemview(w.bytes(), strings={0x0800_1000: "boom %u"}.get)
    marks = [e for e in trace.events if e.kind == "marker"]
    assert [(m.args["phase"], m.text, m.ts) for m in marks] == [("start", "frame", 2), ("stop", "frame", 9)]
    [p] = [e for e in trace.events if e.kind == "print"]
    assert (p.text, p.args["level"]) == ("boom 42", "error")
    assert trace.tasks[A].stack_size == 1024


def test_a_broken_packet_resynchronizes_at_the_next_sync():
    good = scenario().bytes()
    head = Writer().start(t=0).bytes()                                  # パケットの境目
    broken = head + bytes([0x80] * 10 + [0x01]) + good                    # 長すぎる LEB128 の ID
    d = SystemViewDecoder()
    d.feed(broken)
    assert d.trace.errors >= 1 and sum(1 for e in d.trace.events if e.kind == "print") >= 1
