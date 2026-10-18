"""Keil の Event Recorder のバッファの解読（fprobe.tracefmt.eventrecorder）。記録は EventRecorder.c の
EventRecordItem・EventRecord2/4・EventRecordData と同じ書き方で作る。書式は実物の RTX5.scvd。"""

import struct
from pathlib import Path

import pytest

from fprobe.tracefmt.eventrecorder import (EventRecorderError, decode_event_recorder, parse_info, parse_status)
from fprobe.tracefmt.scvd import Scvd, ScvdContext

DATA = Path(__file__).resolve().parent / "data" / "tracefmt"
FIRST, LAST, VALID, IRQ = 1 << 24, 1 << 25, 1 << 27, 1 << 19


class Recorder:
    """EventRecorder.c の書き手の模型（record_count 個のリング、トグルビット、周回の番号）。"""

    def __init__(self, count: int = 16, freq: int = 1_000_000):
        self.count, self.freq = count, freq
        self.buf = bytearray(count * 16)
        self.index = 0
        self.ctx = 0
        self.dumped = 0

    def item(self, info: int, ts: int, v1: int, v2: int) -> None:
        i = self.index
        self.index += 1
        slot = i & (self.count - 1)
        old = struct.unpack_from("<I", self.buf, slot * 16 + 12)[0]
        tbit = (old >> 31) ^ 1
        info = info & ~(0xF << 20 | 0xF << 28) | ((i // self.count) & 0xF) << 20 | VALID
        info |= (ts >> 31 & 1) << 28 | (v1 >> 31 & 1) << 29 | (v2 >> 31 & 1) << 30 | tbit << 31
        top = tbit << 31
        struct.pack_into("<4I", self.buf, slot * 16, ts & 0x7FFF_FFFF | top, v1 & 0x7FFF_FFFF | top,
                         v2 & 0x7FFF_FFFF | top, info)

    def record2(self, event_id: int, ts: int, v1: int, v2: int, irq: bool = False) -> None:
        self.item(event_id | FIRST | LAST | (IRQ if irq else 0), ts, v1, v2)

    def record4(self, event_id: int, ts: int, v1: int, v2: int, v3: int, v4: int) -> None:
        ctx = (self.ctx & 7) << 16
        self.ctx += 1
        self.item(event_id | ctx | FIRST, ts, v1, v2)
        self.item(0x0001 | ctx | LAST, ts, v3, v4)

    def data(self, event_id: int, ts: int, payload: bytes) -> None:
        if len(payload) <= 8:
            v1, v2 = struct.unpack("<II", payload.ljust(8, b"\0"))
            self.item(event_id | (len(payload) << 16 & 0x70000) | FIRST | LAST, ts, v1, v2)
            return
        ctx = (self.ctx & 7) << 16
        self.ctx += 1
        chunks = [payload[i:i + 8] for i in range(0, len(payload), 8)]
        v1, v2 = struct.unpack("<II", chunks[0])
        self.item(event_id | ctx | FIRST, ts, v1, v2)
        for n, c in enumerate(chunks[1:-1], start=1):
            v1, v2 = struct.unpack("<II", c)
            self.item(0xFF00 | n | ctx, ts, v1, v2)
        last = chunks[-1]
        v1, v2 = struct.unpack("<II", last.ljust(8, b"\0"))
        self.item((len(last) << 8) | len(chunks) - 1 | ctx | LAST, ts, v1, v2)

    def info(self) -> bytes:
        return struct.pack("<BBHIIIIB3x", 1, 0, 0x0101, self.count, 0x2000_0100, 0x2000_0200, 0x2000_0040, 0)

    def status(self) -> bytes:
        return struct.pack("<BBH8I", 1, self.ctx & 7, 0, self.index, self.index, self.dumped, 0, self.freq, 0, 1,
                           0xE1A5276B)

    def decode(self, scvd=None, ctx=None, since=None):
        return decode_event_recorder(parse_info(self.info()), parse_status(self.status()), bytes(self.buf), scvd,
                                     ctx, since)


@pytest.fixture(scope="module")
def rtx():
    return Scvd.load(DATA / "RTX5.scvd", DATA / "EventRecorder.scvd")


def test_rtx_events_with_threads(rtx):
    r = Recorder()
    r.record2(0xFF00, 10, 1, 1_000_000)                                  # INIT
    r.record2(0xF203, 100, 0x2000_1000, 0x0800_0101)                     # ThreadCreated
    r.record2(0xF203, 110, 0x2000_2000, 0x0800_0201)
    r.record2(0xF219, 200, 0x2000_1000, 0)                               # ThreadSwitched
    r.record4(0xF001, 250, 0x2000_8000, 64, 0, 0x2000_8100)               # MemoryAlloc（4 値）
    r.record2(0xF219, 300, 0x2000_2000, 0)
    r.record2(0xF21C, 400, 0x2000_1000, 0)                               # ThreadDestroyed
    names = {0x0800_0101: "worker", 0x0800_0201: "osRtxIdleThread"}
    trace, nxt = r.decode(rtx, ScvdContext(symbol_name=names.get))
    assert nxt == r.index and trace.ts_hz == 1e6 and trace.lost == 0
    assert {t.id: t.name for t in trace.tasks.values()} == {0x2000_1000: "worker", 0x2000_2000: "osRtxIdleThread"}
    assert trace.idle == {0x2000_2000}
    assert [(e.kind, e.ts, e.task) for e in trace.events if e.kind.startswith("task_")] == [
        ("task_create", 100, 0x2000_1000), ("task_create", 110, 0x2000_2000), ("task_in", 200, 0x2000_1000),
        ("task_in", 300, 0x2000_2000), ("task_delete", 400, 0x2000_1000)]
    alloc = next(e for e in trace.events if e.name == "MemoryAlloc")
    assert alloc.text == "mem=0x20008000, size=64, type=0, block=0x20008100"


def test_data_records_and_stdout(rtx):
    r = Recorder(count=32)
    for chunk in (b"hello wo", b"rld\n", b"x=1\n"):
        r.data(0xFE00, 5, chunk)
    payload = bytes(range(20))
    r.data(0x0101, 6, payload)
    r.data(0x0102, 7, bytes(range(16)))
    r.data(0x0103, 8, b"abc")
    trace, _ = r.decode()
    prints = [e.text for e in trace.events if e.kind == "print"]
    assert prints == ["hello world", "x=1"]
    data = {e.args["id"]: e.args.get("data") for e in trace.events if e.kind == "other"}
    assert data == {0x0101: payload.hex(), 0x0102: bytes(range(16)).hex(), 0x0103: b"abc".hex()}


def test_wrap_around_keeps_the_last_records_and_since_reads_the_rest():
    r = Recorder(count=8)
    for t in range(12):
        r.record2(0x0101, t * 10, t, 0)
    trace, nxt = r.decode()
    assert [e.args["values"][0] for e in trace.events] == list(range(4, 12)) and nxt == 12
    for t in range(12, 15):
        r.record2(0x0101, t * 10, t, 0)
    more, _ = r.decode(since=nxt)
    assert [e.args["values"][0] for e in more.events] == [12, 13, 14]
    for t in range(15, 40):
        r.record2(0x0101, t * 10, t, 0)
    late, _ = r.decode(since=15)
    assert late.lost == 25 - 8 and any("上書き" in n for n in late.notes)


def test_bit31_is_restored_and_torn_records_are_dropped():
    r = Recorder()
    r.record2(0x0101, 0x8000_0010, 0xFFFF_FFFF, 0x8000_0000)
    r.record2(0x0101, 0x8000_0020, 1, 2)
    struct.pack_into("<I", r.buf, 16 + 4, 5)                             # 2 つ目の val1 の bit31 がトグルと合わない
    trace, _ = r.decode()
    [e] = trace.events
    assert e.ts == 0x8000_0010 and e.args["values"] == [0xFFFF_FFFF, 0x8000_0000] and trace.lost == 1


def test_timestamps_wrap_32_bits():
    r = Recorder()
    r.record2(0x0101, 0xFFFF_FF00, 0, 0)
    r.record2(0x0101, 0x0000_0100, 0, 0)
    trace, _ = r.decode()
    assert [e.ts for e in trace.events] == [0xFFFF_FF00, 0x1_0000_0100]


def test_bad_control_blocks():
    with pytest.raises(EventRecorderError):
        parse_info(struct.pack("<BBHIIIIB3x", 1, 0, 0x101, 12, 0, 0, 0, 0))     # 2 の冪でない
    with pytest.raises(EventRecorderError):
        parse_status(bytes(36))
