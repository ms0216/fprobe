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
    assert not getattr(trace, "idle", set())       # 名前でアイドルを決めない（使う側が idle_tasks で渡す）
    kinds = [(e.kind, e.ts, e.task) for e in trace.events if e.kind in ("task_in", "task_out", "isr_enter", "isr_exit")]
    # ts は差の和（TRACE_START・INIT の差は意味を持たないので、INIT からの区切り。scenario の時刻 1_000 が 0）
    assert kinds == [("task_in", 100, IDLE), ("isr_enter", 200, None), ("isr_exit", 260, None),
                     ("task_in", 310, A), ("task_out", 500, A), ("task_in", 520, IDLE)]
    assert [b["reason"] for b in trace.breaks] == ["init"] and trace.events[0].name == "TRACE_START"
    [p] = [e for e in trace.events if e.kind == "print"]
    assert p.text == "value=-2 <str@0x00000000>" and p.args["level"] == "info"
    assert next(e for e in trace.events if e.kind == "isr_enter").args["number"] == 27
    assert next(e for e in trace.events if e.kind == "task_block").args["cause"] == 3


def test_the_trace_becomes_a_timeline_and_isr_activations():
    trace = decode_systemview(scenario().bytes())
    seg = trace.segment
    clock = TargetClock(trace.ts_hz, 100_000_000, anchor_ts=0, anchor_t=5_000_000, segment=seg)
    tl = timeline_from_events(task_events(trace.events, clock), trace.names())
    runs = [(r.task, r.t0, r.t1) for r in tl.runs]
    assert runs[0][0] == "IDLE" and runs[1] == ("sensor", clock.to_timebase(310, seg), clock.to_timebase(500, seg))
    [act] = activations(trace.events, clock)
    assert act.number == 27 and act.inclusive == clock.to_timebase(260, seg) - clock.to_timebase(200, seg)
    assert clock.place(trace.events[0]) is None                         # TRACE_START の区切りには対応点が無い


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


def test_the_model_has_no_idle_task_name_rule():
    # タスクの名前でアイドルを決めない（configIDLE_TASK_NAME は設定で変わり、同じ名前の利用者のタスクもありうる）。
    # アイドルのタスクは使う側が trace_stop / trace_snapshot / trace_import の idle_tasks で明示する
    import fprobe.tracefmt.model as model
    assert not hasattr(model, "is_rtos_idle_name") and not hasattr(model, "_IDLE_NAME")


def test_overflow_keeps_the_counted_drops_apart_from_the_lower_bound():
    """DropCount の差（数えた分）を出来事に残す。差が 0 でも OVERFLOW は 1 個以上落ちた印なので lost は下限の 1。"""
    w = Writer().start(t=0)
    w.packet(1, varint(4), 10).packet(1, varint(4), 20)
    trace = decode_systemview(w.bytes())
    overflows = [e for e in trace.events if e.kind == "overflow"]
    assert [e.args["dropped"] for e in overflows] == [4, 0]
    assert trace.lost == 4 + 1


def test_a_print_level_the_protocol_does_not_define_is_not_named():
    """options の下位 2 ビット 3 は SystemView に無い重要度（LOG 0・WARNING 1・ERROR 2）: info と名付けず None。"""
    w = Writer().start(t=0)
    w.packet(26, string("x") + varint(3) + varint(0), 10)
    [p] = [e for e in decode_systemview(w.bytes()).events if e.kind == "print"]
    assert p.args["level"] is None and p.args["options"] == 3



# --- 時刻（段 H2: 基準を混ぜない・つながりの切れ目を区切りにする） -------------------------------------------------


def test_systime_cycles_does_not_jump_the_time_base():
    # 途中から受けた流れ: 最初の SYSTIME_CYCLES の前の出来事も後の出来事も、差を足した同じ基準（絶対の値へ跳ばない）
    w = Writer()
    w.out += b"\x00" * 10
    w.packet(4, varint(1), 100).packet(17, b"", 150)
    w.out += varint(12) + varint(5_000_000) + varint(30)              # SYSTIME_CYCLES（差 30）
    w.last = 180
    w.packet(5, b"", 210)
    trace = decode_systemview(w.bytes())
    assert [e.ts for e in trace.events] == [100, 150, 180, 210] and trace.segment == 0
    assert trace.events[2].args["cycles"] == 5_000_000


def test_a_second_start_begins_a_new_segment():
    # STOP → START の間に止めていた時間は流れに無い: 2 回目の START の後は前と別の区切り（差を詰めてずらさない）
    w = Writer().start(t=0)
    w.packet(4, varint(w.shrink(A)), 10).packet(11, b"", 20)             # TRACE_STOP
    w.start(t=1_000_000)                                                   # 止めた後に START（差は意味を持たない）
    w.packet(5, b"", 1_000_050)
    trace = decode_systemview(w.bytes())
    starts = [e for e in trace.events if e.name == "TRACE_START"]
    assert len(starts) == 2 and starts[1].segment > trace.events[0].segment
    assert any(b["reason"] == "trace_start" for b in trace.breaks)
    out = [e for e in trace.events if e.kind == "task_out"][0]
    assert out.segment == trace.segment and out.segment != next(e for e in trace.events if e.kind == "task_in").segment


def test_a_resynchronization_after_a_broken_packet_begins_a_new_segment():
    head = Writer().start(t=0).packet(4, varint(1), 10).bytes()
    broken = head + bytes([0x80] * 10 + [0x01]) + Writer().start(t=0).packet(5, b"", 5).bytes()
    trace = decode_systemview(broken)
    assert any(b["reason"] == "resync" for b in trace.breaks)
    task_in = next(e for e in trace.events if e.kind == "task_in")
    task_out = next(e for e in trace.events if e.kind == "task_out")
    assert task_out.segment != task_in.segment


def test_a_systime_that_disagrees_with_the_summed_deltas_is_a_fact_and_a_new_segment():
    # SYSTIME_CYCLES は 32 ビットの絶対の値: 差の和と合わなければ（差の 32 ビットのあふれ・失ったパケット）区切る
    w = Writer()
    w.out += b"\x00" * 10
    w.out += varint(12) + varint(1_000) + varint(0)
    w.packet(4, varint(1), 100)
    w.out += varint(12) + varint(1_000 + 100 + 50 + 7) + varint(50)        # 差の和なら 1150（7 合わない）
    trace = decode_systemview(w.bytes())
    [b] = trace.breaks
    assert b["reason"] == "systime_mismatch" and b["mismatch"] == 7 and b["since_ts"] == 0
    assert trace.events[-1].segment == 1


def test_activations_pair_numbered_exits_by_number():
    # 出口が番号を持てば同じ番号の入口と対にする（番号を見ずに最も内側を取り出さない。flow.isr_pairing と同じ）
    from fprobe.tracefmt.model import TraceEvent
    from fprobe.tracefmt.model import activations as acts
    events = [TraceEvent(0, "isr_enter", "e", args={"number": 15}), TraceEvent(10, "isr_enter", "e", args={"number": 27}),
              TraceEvent(30, "isr_exit", "x", args={"number": 15})]          # 27 の出口が欠けた
    clock = TargetClock(1.0, 1.0, 0, 0)
    [a] = acts(events, clock)
    assert (a.number, a.start, a.end) == (15, 0, 30)
