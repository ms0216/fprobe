"""Percepio Tracealyzer の PSF の流れの解読（fprobe.tracefmt.tracealyzer）。流れは TraceRecorder v4.11 の
trcStreamingRecorder.c（見出し・時刻・エントリの表）と trcEvent.c（出来事の見出し）と同じ並びで作る。"""

import struct

import pytest

from fprobe.tracefmt.tracealyzer import (COMMAND_START, TracealyzerDecoder, TracealyzerError, decode_tracealyzer)

T1, T2, IDLE, ISR, CH = 0x2000_0100, 0x2000_0200, 0x2000_0300, 0x2000_0900, 0x2000_0A00
SYMBOL = 28


class Psf:
    def __init__(self, freq: int = 48_000_000, platform: int = 0x1AA1, timer_type: int = 1):
        self.out = bytearray(struct.pack("<IHHIII", 0x50534600, 0x000E, platform, 0, 1, 0) +
                             struct.pack("<HBB", 0, 0, 0) + b"FreeRTOS")
        self.out += struct.pack("<IIIIIII", timer_type, 0, freq, 0, 1000, 0, 0)
        self.count = 0

    def table(self, entries: list[tuple[int, str, int]]) -> "Psf":
        self.out += struct.pack("<III", len(entries), SYMBOL, 3)
        for address, name, state in entries:
            self.out += struct.pack("<IIIII", address, state, 0, 0, 0) + name.encode().ljust(SYMBOL, b"\0")
        return self

    def event(self, code: int, ts: int, *words: int, data: bytes = b"", skip: int = 0) -> "Psf":
        data = data + b"\0" * (-len(data) % 4)
        n = len(words) + len(data) // 4
        self.count += 1 + skip
        self.out += struct.pack("<HHI", code | n << 12, self.count & 0xFFFF, ts)
        self.out += b"".join(struct.pack("<I", w) for w in words) + data
        return self

    def bytes(self) -> bytes:
        return bytes(self.out)


def scenario() -> Psf:
    p = Psf().table([(T1, "sensor", 3), (IDLE, "IDLE", 0)])
    p.event(0x01, 100, IDLE)                                          # TRACE_START: いま IDLE
    p.event(0x03, 110, T2, data=b"logger\0")                          # OBJ_NAME
    p.event(0x10, 111, T2, 2)                                         # TASK_CREATE
    p.event(0x07, 112, ISR, 5, data=b"UART_IRQ\0")                    # DEFINE_ISR
    p.event(0x30, 200, T1)                                            # TASK_READY
    p.event(0x37, 210, T1, 3)                                         # TASK_ACTIVATE
    p.event(0x33, 300, ISR)                                           # ISR_BEGIN
    p.event(0x37, 320, T1)                                            # ISR からタスクへ戻る（1 語）
    p.event(0x92, 400, CH, 42, data=b"value %d\0")                    # xTracePrintF（1 引数）
    p.event(0x7A, 450, 10)                                            # TASK_DELAY（待ちに入る）
    p.event(0x37, 460, T2, 2)
    return p


def test_a_stream_decodes_names_tasks_isrs_and_prints():
    trace = decode_tracealyzer(scenario().bytes())
    assert trace.ts_hz == 48e6 and trace.system["platform"] == "0x1AA1" and trace.ts_wraps == 0
    assert {t.id: t.name for t in trace.tasks.values() if t.name} == {T2: "logger", T1: "sensor", IDLE: "IDLE"}
    assert not getattr(trace, "idle", set())       # 名前でアイドルを決めない（使う側が idle_tasks で渡す）
    kinds = [(e.kind, e.ts, e.task) for e in trace.events if e.kind in ("task_in", "isr_enter", "isr_exit")]
    assert kinds == [("task_in", 100, IDLE), ("task_in", 210, T1), ("isr_enter", 300, None),
                     ("isr_exit", 320, None), ("task_in", 460, T2)]
    [p] = [e for e in trace.events if e.kind == "print"]
    assert p.text == "value 42"
    assert next(e for e in trace.events if e.kind == "isr_enter").args["name"] == "UART_IRQ"
    assert next(e for e in trace.events if e.kind == "task_block").task == T1


def test_missing_events_are_counted_from_the_event_counter():
    p = Psf().table([]).event(0x01, 1, IDLE).event(0x30, 2, T1, skip=3)
    trace = decode_tracealyzer(p.bytes())
    assert trace.lost == 3 and any(e.kind == "overflow" for e in trace.events)


def test_the_missing_events_mark_carries_the_interval_they_were_lost_in():
    # 欠けた出来事は前の出来事と次の出来事の間のどこか: 印に両端を残す（前の時刻が 0 でも 0 を使う）
    p = Psf().table([]).event(0x01, 0, IDLE).event(0x30, 500, T1, skip=2)
    trace = decode_tracealyzer(p.bytes())
    [mark] = [e for e in trace.events if e.kind == "overflow"]
    assert mark.ts == 0 and mark.args == {"missing": 2, "after_ts": 0, "before_ts": 500}


@pytest.mark.parametrize("timer_type", [2, 3, 4, 6])
def test_timers_that_are_not_free_running_up_counters_are_not_unwrapped(timer_type):
    # 減るタイマ・OS のタイマ（type 2・3・4・6）の値は 32 ビットの増える時刻として続けられない: 時刻にしない（順の番号）。
    # 生の値は args の raw_ts に残す
    p = Psf(timer_type=timer_type).table([]).event(0x01, 900, IDLE).event(0x30, 800, T1).event(0x30, 700, T1)
    trace = decode_tracealyzer(p.bytes())
    assert not trace.timed and trace.ts_hz is None and trace.ts_wraps == 0
    assert [e.args.get("raw_ts") for e in trace.events if e.kind == "task_ready"] == [800, 700]
    assert [e.ts for e in trace.events] == sorted(e.ts for e in trace.events)


def test_compact_prints_use_elf_strings():
    p = Psf().table([]).event(0x01, 1, 0).event(0x99, 5, 0x0800_0100, 0x0800_0200, 7)
    trace = decode_tracealyzer(p.bytes(), strings={0x0800_0100: "ch", 0x0800_0200: "n=%u"}.get)
    [e] = [e for e in trace.events if e.kind == "print"]
    assert (e.text, e.args["channel"]) == ("n=7", "ch")


def test_kernel_object_operations_are_named_not_a_code_range():
    # 0x11 以上を一律にカーネルオブジェクトの操作としない: 時刻・ヒープ・眠り・スタックの出来事は object でない
    p = Psf().table([(T1, "sensor", 3)]).event(0x01, 1, T1)
    p.event(0x50, 10, 0x2000_0100).event(0x31, 20, 5).event(0x38, 30, 0x2000_0200, 16).event(0x3A, 40, 1)
    p.event(0xEB, 50, T1, 64).event(0xA0, 60, 0x2000_0300)
    kinds = {e.name: e.kind for e in decode_tracealyzer(p.bytes()).events}
    assert kinds["QUEUE_SEND"] == "object" and kinds["TIMER_START"] == "object"
    assert kinds["NEW_TIME"] == kinds["MALLOC"] == kinds["LOWPOWER_BEGIN"] == kinds["UNUSED_STACK"] == "other"


def test_streaming_in_pieces():
    data = scenario().bytes()
    d = TracealyzerDecoder()
    for i in range(0, len(data), 7):
        d.feed(data[i:i + 7])
    assert len(d.trace.events) == len(decode_tracealyzer(data).events)


def test_not_psf_and_other_platforms():
    with pytest.raises(TracealyzerError):
        decode_tracealyzer(bytes(64))
    trace = decode_tracealyzer(Psf(platform=0x1234).table([]).event(0x01, 1, 0).bytes())
    assert any("FreeRTOS" in n for n in trace.notes)
    assert COMMAND_START == bytes.fromhex("0101000000 00fdff".replace(" ", ""))
