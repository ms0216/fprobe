"""ロジアナの連続ストリーミング（LaStream）: 逐次の復号・VCD・停止と出し切り・溢れ（偽のデバイス。ハードウェア不要）。"""

import random
from array import array
from collections import deque
from types import SimpleNamespace

import pytest

from fprobe import generated as g
from fprobe.la import (DeepAnalyzer, Transitions, decode_beats, decode_transitions, write_vcd)

MPS = 512


class FakeStreamDevice:
    """ストリーミング中の fprobe の振る舞いを真似る。

    - status を読むたびに（走っていれば）source の拍を 0〜per_poll 拍書く（stream_beats が増える）。
    - 書いた拍は USB のパケットに切って並べる（途中にも短パケット = アイドルでのフラッシュ。拍の途中で切れることもある）。
    - read_stream_some は 1 転送: 満杯か短パケットで終わる。それ以外で足りない（実機なら待ち続ける）と TimeoutError。
    - 未読が ring − 256 拍を超えそうなら書き込みを止めて overrun を立てる。
    - run = 0 で書き込みを止め（その後に数拍書く = 非同期 FIFO の残り）、status を数回読む間 reading = 1。
    """

    def __init__(self, source: bytes, *, rng: random.Random, per_poll: int = 300, ring: int = 1 << 20):
        assert len(source) % 16 == 0
        self.source, self.rng, self.per_poll, self.ring = source, rng, per_poll, ring
        self.written = 0                                   # 書いた拍
        self.packetized = 0                                # パケットにしたバイト
        self.received = 0                                  # ホストが受け取ったバイト
        self.packets: deque[bytes] = deque()
        self.running = self.overrun = False
        self.drain_polls = 0
        self.control_writes: list[int] = []
        self.mem_ready = True                              # 外部メモリの準備（PYNQ では中継が MEM_BASE を書いた）

    # --- Device の API ----------------------------------------------------------------

    def capabilities(self):
        return SimpleNamespace(mem_bytes=128 << 20, la_channels=16, la_sample_hz=100_000_000)

    def write(self, address, words):
        pass

    def write32(self, address, value):
        if address != g.LA_DEEP_CONTROL:
            return
        self.control_writes.append(value)
        run = bool(value & 1)
        if run and not self.running:
            assert value >> g.LA_DEEP_CONTROL_STREAM[0] & 1
            self.written = self.packetized = self.received = 0
            self.overrun = False
        elif self.running and not run:
            self._write_beats(3)                          # 止めた後に書かれる残り
            self.drain_polls = 3
        self.running = run

    def read32(self, address):
        if address == g.LA_DEEP_RING_BEATS:
            return self.ring
        if address == g.LA_DEEP_STATUS:
            if self.running:
                self._write_beats(self.rng.randint(0, self.per_poll))
            reading = self.running or self.drain_polls > 0
            self.drain_polls = max(0, self.drain_polls - 1)
            return int(self.overrun) << g.LA_DEEP_STATUS_OVERRUN[0] | int(reading) << g.LA_DEEP_STATUS_READING[0]
        if address == g.LA_DEEP_STREAM_BEATS:
            return self.written
        if address == g.MEM_STATUS:
            return int(self.mem_ready) << g.MEM_STATUS_READY[0]
        raise AssertionError(f"想定外の読み 0x{address:X}")

    def read_stream_some(self, size, timeout_ms=2000):
        self._packetize()
        size = max(MPS, size // MPS * MPS)
        out = bytearray()
        while self.packets and len(out) + len(self.packets[0]) <= size:
            packet = self.packets.popleft()
            out += packet
            if len(packet) < MPS:
                break
        else:
            if len(out) < size:
                raise TimeoutError("届かない量を求めた（実機なら待ち続ける）")
        self.received += len(out)
        return bytes(out)

    # --- 内部 ---------------------------------------------------------------------------

    def _write_beats(self, n):
        unread = self.written - self.received // 16
        if unread + n > self.ring - 256:
            n = max(0, self.ring - 256 - unread)
            self.overrun = True
        self.written = min(len(self.source) // 16, self.written + n)

    def _packetize(self):
        end = self.written * 16
        while self.packetized < end:
            size = MPS if self.rng.random() < 0.7 else self.rng.randint(1, MPS - 1)
            size = min(size, end - self.packetized)
            self.packets.append(self.source[self.packetized:self.packetized + size])
            self.packetized += size


def raw_source(rng, beats):
    samples, v = array("H"), 0
    while len(samples) < beats * 8:
        samples.extend(array("H", [v]) * rng.randint(1, 30))
        v = rng.choice([v, rng.randrange(1 << 16)])
    return samples[:beats * 8].tobytes()


def rle_source(rng, beats):
    records, v = array("I"), 0
    for i in range(beats * 4):
        v = rng.choice([v, rng.randrange(1 << 16)])       # 同じ値が続くレコード（65536 で閉じた場合）も混ぜる
        count = 0xFFFF if i % 1000 == 999 else rng.choice([0, rng.randrange(40)])
        records.append(count << 16 | v)
    return records.tobytes()


@pytest.fixture(autouse=True)
def fast_reads(monkeypatch):
    monkeypatch.setattr("fprobe.la.LaStream.READ_MIN", 4096)
    monkeypatch.setattr("fprobe.la.LaStream.READ_MAX", 3000)            # 拍の途中で切れる長さ


def run_stream(dev, rle, duration_s=0.1):
    stream = DeepAnalyzer(dev).stream(rle=rle)
    chunks = list(stream.chunks(duration_s))
    return stream, chunks


@pytest.mark.parametrize("rle", [False, True])
def test_chunks_join_to_whole_capture(rle):
    rng = random.Random(1 + rle)
    source = (rle_source if rle else raw_source)(rng, 20000)
    dev = FakeStreamDevice(source, rng=rng)
    stream, chunks = run_stream(dev, rle)
    got = source[:dev.written * 16]
    assert dev.written > 1000 and dev.received == len(got)               # 止めた後の残りまで出し切った
    assert dev.control_writes[-1] == 0 and not dev.packets
    assert b"".join(c.data for c in chunks) == got
    assert len(chunks) > 5 and all(len(c.data) % 16 == 0 for c in chunks)
    whole = decode_beats(got, rle)
    assert array("H", b"".join(c.samples().tobytes() for c in chunks)) == whole
    values, starts = array("H"), array("Q")
    for c in chunks:
        v, s = c.transitions()
        values += v
        starts += s
    ref_values, ref_starts, length = decode_transitions(got, rle)
    assert (values, starts) == (ref_values, ref_starts)
    stats = stream.stats()
    assert (stats.samples, stats.bytes, stats.overrun) == (length, len(got), False)
    assert stats.duration_s == pytest.approx(length * stats.sample_period_s) and stats.mb_per_s > 0


def _changes(path, skip_id=None):
    return [line for line in path.read_text().splitlines()
            if (line.startswith("#") or line[:1] in "01") and line[1:] != skip_id]


@pytest.mark.parametrize("rle", [False, True])
def test_stream_to_vcd_matches_write_vcd(tmp_path, rle):
    rng = random.Random(7)
    source = (rle_source if rle else raw_source)(rng, 4000)
    dev = FakeStreamDevice(source, rng=rng, per_poll=100)
    stats = DeepAnalyzer(dev).stream_to_vcd(tmp_path / "s.vcd", 0.05, rle=rle)
    got = source[:dev.written * 16]
    values, starts, length = decode_transitions(got, rle)
    write_vcd(Transitions(values, starts, length, 0, 0, stats.sample_period_s, 16), tmp_path / "w.vcd")
    trigger_id = chr(33 + 16)
    expected = _changes(tmp_path / "w.vcd", trigger_id)                   # トリガだけの時刻を除く
    expected = [line for i, line in enumerate(expected)
                if not (line.startswith("#") and (i + 1 == len(expected) or expected[i + 1].startswith("#")))]
    assert _changes(tmp_path / "s.vcd") == expected
    assert stats.samples == length


def test_overrun_stops_and_keeps_received_data():
    rng = random.Random(3)
    source = raw_source(rng, 20000)
    dev = FakeStreamDevice(source, rng=rng, per_poll=5000, ring=2048)    # ホストより速く書く
    stream, chunks = run_stream(dev, False, duration_s=10.0)
    stats = stream.stats()
    assert stats.overrun and stats.elapsed_s < 5.0                         # 溢れたら時間を待たずに止める
    got = source[:dev.written * 16]
    assert b"".join(c.data for c in chunks) == got and stats.bytes == len(got)


def test_breaking_out_stops_and_drains():
    rng = random.Random(5)
    source = raw_source(rng, 20000)
    dev = FakeStreamDevice(source, rng=rng)
    stream = DeepAnalyzer(dev).stream(rle=False)
    for _ in stream.chunks(10.0):
        break
    assert not dev.running and dev.received == dev.written * 16 and not dev.packets
    assert stream.stats().elapsed_s < 5.0
    with pytest.raises(RuntimeError):
        next(stream.chunks(1.0))


@pytest.mark.parametrize("rle", [False, True])
def test_chunk_length_is_its_sample_count(rle):
    rng = random.Random(7)
    source = (rle_source if rle else raw_source)(rng, 3000)
    _, chunks = run_stream(FakeStreamDevice(source, rng=rng), rle)
    assert [c.length for c in chunks] == [len(c.samples()) for c in chunks]
    assert all(b.first == a.first + a.length for a, b in zip(chunks, chunks[1:]))


def test_stop_ends_the_stream_and_drains_the_rest():
    # stop は時間の前でも書き込みを止め、残りを出し切ってから終わる（抜けるのと違い、残りも受け取る）
    rng = random.Random(9)
    source = raw_source(rng, 50000)
    dev = FakeStreamDevice(source, rng=rng)
    stream = DeepAnalyzer(dev).stream(rle=False)
    received = bytearray()
    for i, chunk in enumerate(stream.chunks(float("inf"))):
        received += chunk.data
        if i == 2:
            stream.stop()
    assert not dev.running and bytes(received) == source[:dev.written * 16]
    assert stream.stats().elapsed_s < 5.0


def test_starting_without_a_ready_memory_is_an_error_with_the_reason():
    # PYNQ の深いバッファは、中継が DDR の領域（MEM_BASE）を書くまで使えない。待ち続けずに理由を言って断る
    dev = FakeStreamDevice(bytes(16), rng=random.Random(1))
    dev.mem_ready = False
    with pytest.raises(RuntimeError, match="外部メモリ"):
        DeepAnalyzer(dev).start(rate_hz=1e6)
    assert dev.control_writes == []


@pytest.mark.parametrize("was_running", [False, True])
def test_leftovers_of_an_earlier_stream_are_discarded_before_starting(was_running):
    # 前の流れが止められた後に出し切った残り（例外で抜けた・デーモンが落ちた）が受け取り口に残っていても、
    # 新しい流れの先頭にしない（見出しの無い所から読み始めて StreamFormatError になり、止めるたびに残りが増える）
    rng = random.Random(11)
    source = raw_source(rng, 20000)
    dev = FakeStreamDevice(source, rng=rng)
    dev.packets.extend([b"\x31\xc9" * (MPS // 2), b"\xff" * 7])
    dev.running = was_running                                           # 前の流れが走ったまま（落ちたデーモン）
    stream, chunks = run_stream(dev, False)
    assert b"".join(c.data for c in chunks) == source[:dev.written * 16]
