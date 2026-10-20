"""ロジアナの流し続けにイベントも載せる構成（R1）: 見出しの解き方・LaStream での分け方・記録への取り込み・collector との
引き渡し（偽のデバイス。ハードウェア不要）。仕様: docs/superpowers/specs/2026-10-19-r1-unified-stream-design.md"""

import random
import threading
import time
from types import SimpleNamespace

import pytest

from fprobe import generated as g
from fprobe.collector import Collector
from fprobe.events import decode_events
from fprobe.la import (DeepAnalyzer, SegmentSplitter, StreamFormatError, decode_beats, split_segments)
from fprobe.recording import Recorder, Recording
from fprobe.recording_stream import LaRecordingStream
from test_la_stream import FakeStreamDevice, raw_source

EV_CAPACITY = 1 << 19


def header(kind, count, seq):
    return (g.STREAM_SEG_MAGIC << 64 | seq << 32 | count << 16 | kind).to_bytes(16, "little")


def event(i, kind=3, t=None):
    """UART 受信のバイト i & 0xFF を、時刻 1000 + i で（1 件 = 16 バイト）。"""
    t = 1000 + i if t is None else t
    return (kind << 120 | (i & 0xFF) << 80 | t).to_bytes(16, "little")


def framed(rng, la: bytes, first_event: int, n_events: int):
    """ロジアナの拍とイベントを、デバイスと同じくかたまり（見出し + 1〜256 拍）にして交互に並べる。
    (流れ, かたまりの終わりの拍の位置の列) を返す。"""
    out, ends, la_pos, ev = bytearray(), [], 0, 0
    la_beats = len(la) // 16
    while la_pos < la_beats or ev < n_events:
        if ev < n_events and (la_pos >= la_beats or rng.random() < 0.4):
            n = min(rng.randint(1, 256), n_events - ev)
            out += header(g.STREAM_SEG_EVENTS, n, (first_event + ev) % EV_CAPACITY)
            out += b"".join(event(first_event + ev + k) for k in range(n))
            ev += n
        else:
            n = min(rng.randint(1, 256), la_beats - la_pos)
            out += header(g.STREAM_SEG_LA, n, la_pos)
            out += la[la_pos * 16:(la_pos + n) * 16]
            la_pos += n
        ends.append(len(out) // 16)
    return bytes(out), ends


class FakeMixedDevice(FakeStreamDevice):
    """イベントも流す構成の振る舞い: 流れは見出し付きのかたまり、stream_beats は出すと決めた拍（かたまりの境まで）。"""

    def __init__(self, source, ends, *, rng, written_events=0, **kw):
        super().__init__(source, rng=rng, **kw)
        self.ends, self.written_events, self.post = ends, written_events, None

    def capabilities(self):
        return SimpleNamespace(mem_bytes=128 << 20, la_channels=16, la_sample_hz=100_000_000, la_stream_events=True)

    def write32(self, address, value):
        if address == g.LA_DEEP_POST_BEATS:
            self.post = value
        super().write32(address, value)

    def read32(self, address):
        if address == g.EVENTS_CAPACITY:
            return EV_CAPACITY
        if address == g.EVENTS_WRITTEN:
            return self.written_events
        return super().read32(address)

    def _write_beats(self, n):
        target = self.written + n
        self.written = next((e for e in self.ends if e >= target), self.ends[-1]) if n else self.written

    def time(self):
        return 5000


# --- 見出しの解き方 ---------------------------------------------------------------------------------

def test_split_segments_and_feeding_in_pieces():
    rng = random.Random(1)
    la = raw_source(rng, 700)
    data, _ = framed(rng, la, 10, 300)
    whole = split_segments(data)
    assert b"".join(s.data for s in whole if s.kind == g.STREAM_SEG_LA) == la
    ev = b"".join(s.data for s in whole if s.kind == g.STREAM_SEG_EVENTS)
    assert [e.data & 0xFF for e in decode_events(ev)] == [(10 + i) & 0xFF for i in range(300)]
    splitter, pieces, pos = SegmentSplitter(), [], 0
    while pos < len(data):                                       # 拍・かたまりの境に揃わない切れ目で
        n = rng.randint(1, 5000)
        pieces += splitter.feed(data[pos:pos + n])
        pos += n
    assert pieces == whole and splitter.pending == 0


def test_broken_header_and_truncated_stream_are_errors():
    with pytest.raises(StreamFormatError):
        split_segments(bytes(16) + bytes(16))
    with pytest.raises(StreamFormatError):
        split_segments(header(g.STREAM_SEG_LA, 2, 0) + bytes(16))         # 2 拍のはずが 1 拍
    with pytest.raises(StreamFormatError):
        split_segments(header(9, 1, 0) + bytes(16))                       # 知らない種類


# --- LaStream ---------------------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def fast_reads(monkeypatch):
    monkeypatch.setattr("fprobe.la.LaStream.READ_MIN", 4096)
    monkeypatch.setattr("fprobe.la.LaStream.READ_MAX", 3000)


def test_stream_separates_waveform_and_events():
    rng = random.Random(2)
    la = raw_source(rng, 3000)
    data, ends = framed(rng, la, 7, 900)
    dev = FakeMixedDevice(data, ends, rng=rng)
    stream = DeepAnalyzer(dev).stream(rle=False, events_from=7)
    chunks = list(stream.chunks(0.2))
    assert dev.post == 7 and stream.carries_events
    got = data[:dev.written * 16]
    segs = split_segments(got)
    la_got = b"".join(s.data for s in segs if s.kind == g.STREAM_SEG_LA)
    assert len(la_got) > 16 * 1000 and b"".join(c.data for c in chunks) == la_got
    assert decode_beats(la_got, False) == decode_beats(la[:len(la_got)], False)
    events = [e for c in chunks for e in c.events]
    n = sum(len(s.data) // 16 for s in segs if s.kind == g.STREAM_SEG_EVENTS)
    assert n > 100 and [e.data & 0xFF for e in events] == [(7 + i) & 0xFF for i in range(n)]
    assert stream.events_next == 7 + n and stream.events_received == n
    assert all(b.first == a.first + a.length for a, b in zip(chunks, chunks[1:]))


def test_default_start_is_the_current_event_count():
    rng = random.Random(3)
    data, ends = framed(rng, raw_source(rng, 100), 42, 10)
    dev = FakeMixedDevice(data, ends, rng=rng, written_events=42)
    stream = DeepAnalyzer(dev).stream(rle=False)
    list(stream.chunks(0.05))
    assert dev.post == 42


def test_event_position_gap_is_an_error():
    rng = random.Random(4)
    data, ends = framed(rng, raw_source(rng, 100), 50, 10)                # 届くのは 50 からだが 40 からのつもり
    dev = FakeMixedDevice(data, ends, rng=rng)
    with pytest.raises(StreamFormatError):
        list(DeepAnalyzer(dev).stream(rle=False, events_from=40).chunks(0.05))


def test_events_from_needs_the_capability():
    rng = random.Random(5)
    dev = FakeStreamDevice(raw_source(rng, 10), rng=rng)
    with pytest.raises(RuntimeError):
        DeepAnalyzer(dev).stream(rle=False, events_from=0)


# --- 記録へ -----------------------------------------------------------------------------------------

def test_recording_stream_records_events_and_hands_back(tmp_path):
    rng = random.Random(6)
    la = raw_source(rng, 2000)
    data, ends = framed(rng, la, 100, 500)
    dev = FakeMixedDevice(data, ends, rng=rng)
    rec = Recorder(tmp_path / "r", timebase_hz=100e6)
    stream = LaRecordingStream(dev, rec, rle=False, chunk_s=1e-5, lock=threading.RLock(), events_from=100)
    stream.start()
    deadline = time.monotonic() + 5
    while dev.written < ends[-1] and time.monotonic() < deadline:
        time.sleep(0.005)
    stream.stop()
    assert stream.error is None and stream.carries_events
    n = stream.events
    assert n > 50 and stream.events_next == 100 + n
    rec.close()
    view = Recording.open(tmp_path / "r")
    rx = view.events(0, 1 << 62, streams=["uart"])
    assert [e.b for e in rx] == [(100 + i) & 0xFF for i in range(n)]


class FakeEventLog:
    def __init__(self, written):
        self.written, self.capacity = written, EV_CAPACITY
        self.reads = []

    def counts(self):
        return self.written, 0

    def read(self, last=None, end=None):
        self.reads.append((last, end))
        return decode_events(b"".join(event(i) for i in range(end - last, end)))


def test_collector_hands_over_and_takes_back(tmp_path):
    rec = Recorder(tmp_path / "c", timebase_hz=100e6)
    log = FakeEventLog(written=30)
    collector = Collector(SimpleNamespace(time=lambda: 5000), rec, events=log)
    collector._seen = 20
    seen, carry = collector.hand_over()                            # 20〜29 を取り込んでから渡す
    assert (seen, carry) == (30, None) and log.reads == [(10, 30)]
    log.written = 80                                               # 流している間（30〜79 は流れが取り込む）
    collector._poll_events()
    assert log.reads == [(10, 30)]                                 # 止まっている
    collector.take_back(75, None)                                  # 流れは 75 まで（止めた時点）
    collector._poll_events()
    assert log.reads[-1] == (5, 80)                                # 続きの 75〜79 だけ


# --- Probe の道具: 記録の取り込みと流し続けの引き渡し ------------------------------------------------

class ProbeMixedDevice(FakeMixedDevice):
    """Probe から使う: タイムベース・イベント記録（リングの読み）も真似る。"""

    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self.now = 5_000_000
        self.region_reads = []
        self._count = 0

    def capabilities(self):
        return SimpleNamespace(mem_bytes=128 << 20, la_channels=16, la_sample_hz=100_000_000, la_stream_events=True,
                               timebase_hz=100_000_000, board="fake", design="fprobe_la")

    def time(self):
        self.now += 1000
        return self.now

    def write(self, address, words):
        if address == g.LA_DEEP_READ_FIRST:
            self._count = words[1]

    def read(self, address, count):
        assert address == g.EVENTS_WRITTEN and count == 2
        return [self.written_events, 0]

    def read32(self, address):
        if address in (g.LA_DEEP_RING_BEATS, g.LA_DEEP_STATUS, g.LA_DEEP_STREAM_BEATS, g.EVENTS_CAPACITY,
                       g.EVENTS_WRITTEN, g.MEM_STATUS):
            return super().read32(address)
        return 0

    def read_stream(self, size, timeout_ms=2000):
        assert not self.running, "流している間にイベントのリングを読んだ"
        end = self.written_events
        self.region_reads.append((end - self._count, end))
        return b"".join(event(i) for i in range(end - self._count, end))


def test_probe_records_events_before_during_and_after_the_stream(tmp_path, monkeypatch):
    from fprobe.mcp_server import Probe
    monkeypatch.setenv("FPROBE_HOME", str(tmp_path))
    rng = random.Random(8)
    data, ends = framed(rng, raw_source(rng, 1500), 120, 300)        # 流れは 120 件目から
    dev = ProbeMixedDevice(data, ends, rng=rng, written_events=100)
    probe = Probe("unused")
    probe._session = SimpleNamespace(device=dev)
    probe.recording_start(name="mix")                               # 100 件目から取り込む
    probe._collector.poll_s = 3600                                  # 周期の読みは試験が呼ぶ
    dev.written_events = 120                                        # 流す前に 20 件
    status = probe.recording_la_stream_start(chunk_s=5e-5, rle=False)
    assert status["running"] and dev.post == 120 and dev.region_reads == [(100, 120)]
    deadline = time.monotonic() + 5
    while dev.written < ends[-1] and time.monotonic() < deadline:
        time.sleep(0.005)
    stopped = probe.recording_la_stream_stop()
    assert stopped["events"] == 300 and not probe._collector._paused.is_set()
    dev.written_events = 425                                        # 止めた後に 5 件
    probe._collector.poll()
    assert dev.region_reads[-1] == (420, 425)
    rx = probe.recording_events(streams=["uart"], limit=10_000)["events"]
    assert [e["b"] for e in rx] == [i & 0xFF for i in range(100, 425)]   # 欠けも二重もない
    probe.recording_stop()
