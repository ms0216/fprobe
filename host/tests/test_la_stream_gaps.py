"""深いバッファの垂れ流しの欠けの区間（段 B。CSR la_deep.control.gaps）: GapPlacer の置き方・LaStream・VCD・記録（偽のデバイス。
ハードウェア不要）。仕様: docs/superpowers/specs/2026-11-01-la-deep-fast-design.md §12"""

import random
import threading
import time
from array import array

import pytest

from fprobe import generated as g
from fprobe.events import KIND_LA_GAP, decode_events, resolve
from fprobe.la import DeepAnalyzer, GapPlacer, StreamFormatError, VcdWriter, decode_beats
from fprobe.recording import Recorder, Recording
from fprobe.recording_stream import LaRecordingStream
from test_la_stream import raw_source
from test_la_stream_events import EV_CAPACITY, FakeMixedDevice, header
from caps_fakes import with_caps

SPB = 8                                                     # RAW 16 ch の 1 拍のサンプルの数
HOLD = 64 << 20                                             # 位置の決まらない拍を持つ上限（試験では届かない大きさ）


def gap_record(beat, samples, t=777):
    return (KIND_LA_GAP << 120 | samples << 80 | (beat & 0xFFFFFFFF) << 48 | t).to_bytes(16, "little")


def beats_of(values):
    return array("H", values).tobytes()


def placed(pieces, rle=False):
    """GapPlacer の並び → ({位置: 値}, [欠け])。"""
    out, gaps = {}, []
    for piece in pieces:
        if piece[0] == "gap":
            gaps.append(piece[1])
            continue
        _, data, first, *_ = piece
        for k, v in enumerate(decode_beats(data, rle)):
            out[first + k] = v
    return out, gaps


# --- GapPlacer ---------------------------------------------------------------------------------------

def test_a_record_puts_the_gap_between_the_beats():
    p = GapPlacer(False, 0, hold_bytes=HOLD)
    p.add_beats(beats_of(range(32)))                         # 4 拍
    p.add_record(2, 24, 5)                                   # 2 拍の後に 24 サンプルの欠け
    values, gaps = placed(p.release(4, 1, 2))
    assert [(x.start, x.samples, x.beat, x.drop_time48) for x in gaps] == [(16, 24, 2, 5)]
    assert values == {**{i: i for i in range(16)}, **{40 + i: 16 + i for i in range(16)}}
    assert p.position == 56


def test_beats_wait_for_a_reported_record_that_has_not_arrived():
    p = GapPlacer(False, 0, hold_bytes=HOLD)
    p.add_beats(beats_of(range(48)))                         # 6 拍
    # 回路は 1 件（位置 3）を出したと言うが、記録はまだ届かない: 位置 3 の前の拍だけ出せる
    values, gaps = placed(p.release(6, 1, 3))
    assert sorted(values) == list(range(24)) and not gaps
    p.add_record(3, 80, 0)
    values, gaps = placed(p.release(6, 1, 3))
    assert [(x.start, x.samples) for x in gaps] == [(24, 80)] and sorted(values) == list(range(104, 128))


def test_with_two_records_missing_only_beats_before_the_last_known_one_go_out():
    p = GapPlacer(False, 0, hold_bytes=HOLD)
    p.add_beats(beats_of(range(80)))                         # 10 拍
    p.add_record(2, 8, 0)
    values, gaps = placed(p.release(10, 3, 9))               # 2 件が未着（位置 >= 2、最後は 9）
    assert sorted(values) == list(range(16)) and [x.start for x in gaps] == [16]
    p.add_record(5, 16, 0)
    p.add_record(9, 8, 0)
    values, gaps = placed(p.release(10, 3, 9))
    assert [(x.start, x.samples) for x in gaps] == [(48, 16), (96, 8)]
    assert sorted(values) == [*range(24, 48), *range(64, 96), *range(104, 112)]


def test_a_stale_report_never_releases_beats_received_after_it():
    # CSR を読む前に受け取っていた拍（received_at_read）までしか出さない
    p = GapPlacer(False, 0, hold_bytes=HOLD)
    p.add_beats(beats_of(range(40)))
    values, _ = placed(p.release(3, 0, 0))
    assert sorted(values) == list(range(24))


def test_finish_counts_beats_whose_position_is_unknown():
    p = GapPlacer(False, 0, hold_bytes=HOLD)
    p.add_beats(beats_of(range(40)))                         # 5 拍
    p.add_record(1, 8, 0)
    pieces = p.finish(3, 4)                                  # 2 件は届かなかった（位置 >= 1）
    values, gaps = placed(pieces)
    assert sorted(values) == list(range(8)) and len(gaps) == 1
    assert p.unplaced_beats == 4


def test_finish_with_a_trailing_record_after_the_last_beat_places_everything():
    # 書き込みを止める直前に出た記録（その後の拍が書かれなかった）は届かなくても、届いた拍の位置は決まる
    p = GapPlacer(False, 0, hold_bytes=HOLD)
    p.add_beats(beats_of(range(40)))
    values, gaps = placed(p.finish(1, 5))
    assert sorted(values) == list(range(40)) and not gaps and p.unplaced_beats == 0


def test_rle_gap_positions_use_the_record_lengths():
    def rle_beat(records):
        return b"".join(((n - 1) << 16 | v).to_bytes(4, "little") for n, v in records)
    p = GapPlacer(True, 0, hold_bytes=HOLD)
    p.add_beats(rle_beat([(3, 1), (5, 2), (1, 3), (7, 4)]) + rle_beat([(2, 9), (2, 8), (2, 7), (2, 6)]))
    p.add_record(1, 1000, 0)
    values, gaps = placed(p.release(2, 1, 1), rle=True)
    assert [(x.start, x.samples) for x in gaps] == [(16, 1000)]
    assert values[15] == 4 and values[1016] == 9 and max(values) == 1023


def test_beat_positions_wrap_at_32_bits():
    p = GapPlacer(False, 0, hold_bytes=HOLD)
    p._released = p._received = (1 << 32) - 1                # 2^32 − 1 拍を出した後
    p.add_beats(beats_of(range(24)))
    p.add_record(0, 8, 0)                                    # 位置 2^32 = 32 ビットで 0
    values, gaps = placed(p.release(p.received_beats, 1, 0))
    assert [x.beat for x in gaps] == [1 << 32]


def test_records_going_back_are_an_error():
    p = GapPlacer(False, 0, hold_bytes=HOLD)
    p.add_record(5, 8, 0)
    with pytest.raises(StreamFormatError):
        p.add_record(4, 8, 0)


@pytest.mark.parametrize("seed", range(20))
def test_any_arrival_order_rebuilds_the_positions(seed):
    """回路の順（記録はその後の拍より先）と、流れの上の任意の到着順・CSR の読みの時点で、値のある全サンプルの位置が正しい。"""
    rng = random.Random(seed)
    full = list(range(SPB * 600))                            # 位置 = 値
    beats, records, j = [], [], 0
    while j < 600:
        if rng.random() < 0.05:                              # j 拍目から d 拍を捨てる（同じ位置に分けた件のこともある）
            d = rng.randint(1, 30)
            parts = [d] if rng.random() < 0.7 else [1, d]
            for part in parts:
                records.append((len(beats), part * SPB))
            j += sum(parts)
            continue
        beats.append(full[j * SPB:(j + 1) * SPB])
        j += 1
    p = GapPlacer(False, 0, hold_bytes=HOLD)
    got, gaps = {}, []
    sent_beats = sent_records = 0
    while sent_beats < len(beats) or sent_records < len(records):
        if sent_records < len(records) and rng.random() < 0.3:
            b, q = records[sent_records]
            p.add_record(b, q, 0)
            sent_records += 1
        elif sent_beats < len(beats):
            n = min(rng.randint(1, 20), len(beats) - sent_beats)
            p.add_beats(beats_of([v for beat in beats[sent_beats:sent_beats + n] for v in beat]))
            sent_beats += n
        # 回路が出した記録: 届いた拍の位置の後のものも、出ていることがある
        registered = sum(1 for b, _ in records if b <= sent_beats + rng.randint(0, 3))
        registered = max(registered, sent_records)
        last = records[registered - 1][0] if registered else 0
        v, gs = placed(p.release(sent_beats, registered, last))
        got.update(v)
        gaps += gs
    v, gs = placed(p.finish(len(records), records[-1][0] if records else 0))
    got.update(v)
    gaps += gs
    assert p.unplaced_beats == 0
    assert all(full[pos] == value for pos, value in got.items())
    assert len(got) == SPB * len(beats)
    assert sum(x.samples for x in gaps) == sum(q for _, q in records)


# --- LaStream（偽のデバイス） ---------------------------------------------------------------------------

def framed_with_gaps(rng, full_beats=1500, first_event=0, max_chunk=g.STREAM_PL_CHUNK_BEATS, record_delay=None):
    """回路の振る舞い: 欠けを入れたロジアナの拍と、欠けの記録（と UART のイベント）を、見出し付きのかたまりにして並べる。
    ロジアナのかたまりは 1〜max_chunk 拍（PL・中継の複写の道は STREAM_PL_CHUNK_BEATS、中継のゼロコピーの道は STREAM_SEG_MAX_BEATS）。
    記録は、その位置の拍の後のかたまりで（遅れて）届く。record_delay = None なら遅れはでたらめ（0〜200 拍の後で半々）、
    整数なら、記録はその位置から record_delay 拍より後のロジアナが流れに出た後で初めて届く（回路の CSR の gaps は位置の拍を書いた
    時点で数える。流れの上の遅れ = 位置の決まらない拍を PC が持つ量）。返す: (流れ, かたまりの終わり, 全体のサンプル, 記録,
    かたまりごとのロジアナの拍の数の累計)。"""
    full = array("H", raw_source(rng, full_beats))
    delivered, records, j = bytearray(), [], 0
    while j < full_beats:
        if rng.random() < 0.02 and j > 10:
            d = rng.randint(1, 40)
            records.append((len(delivered) // 16, d * SPB))
            j += d
            continue
        delivered += full[j * SPB:(j + 1) * SPB].tobytes()
        j += 1
    out, ends, la_counts, la_pos, ev, la_beats = bytearray(), [], [], 0, 0, len(delivered) // 16
    pending = list(records)
    while la_pos < la_beats or pending:
        if record_delay is None:
            due = pending and la_pos > pending[0][0] + rng.randint(0, 200) and rng.random() < 0.5
        else:
            due = pending and la_pos > pending[0][0] + record_delay
        if pending and (la_pos >= la_beats or due):
            n = min(rng.randint(1, 3), len(pending))
            out += header(g.STREAM_SEG_EVENTS, n, (first_event + ev) % EV_CAPACITY)
            out += b"".join(gap_record(b, q) for b, q in pending[:n])
            del pending[:n]
            ev += n
        else:
            n = min(rng.randint(1, max_chunk), la_beats - la_pos)
            out += header(g.STREAM_SEG_LA, n, la_pos)
            out += delivered[la_pos * 16:(la_pos + n) * 16]
            la_pos += n
        ends.append(len(out) // 16)
        la_counts.append(la_pos)
    return bytes(out), ends, full, records, la_counts


class FakeGapDevice(FakeMixedDevice):
    """欠けの区間を記録する構成: la_deep.gaps・gap_last_beat は、流れに書いたロジアナの拍の位置までの記録（回路は記録を、
    その後の拍より先に出す）。"""

    def __init__(self, data, ends, records, la_counts, **kw):
        super().__init__(data, ends, **kw)
        self.records, self.la_counts = records, la_counts
        self.events_enabled = False

    def capabilities(self):
        return with_caps(super().capabilities(), la_stream_gaps=True)

    def _registered(self):
        """回路が出した記録の件数（流れに書いたロジアナの拍の位置までの記録）。"""
        k = next((i for i, e in enumerate(self.ends) if e >= self.written), len(self.ends) - 1)
        la_written = self.la_counts[k] if self.written else 0
        return sum(1 for b, _ in self.records if b <= la_written)

    def read32(self, address):
        if address == g.EVENTS_CONTROL:
            return int(self.events_enabled)
        if address == g.LA_DEEP_GAP_LAST_BEAT:
            n = self._registered()
            return self.records[n - 1][0] if n else 0
        if address == g.LA_DEEP_GAPS:
            return self._registered()
        return super().read32(address)

    def write32(self, address, value):
        if address == g.EVENTS_CONTROL:
            self.events_enabled = bool(value & 1)
            return
        super().write32(address, value)


@pytest.fixture(autouse=True)
def fast_reads(monkeypatch):
    monkeypatch.setattr("fprobe.la.LaStream.READ_MIN", 4096)
    monkeypatch.setattr("fprobe.la.LaStream.READ_MAX", 3000)


def run_gap_stream(seed, full_beats=1500, max_chunk=g.STREAM_PL_CHUNK_BEATS, record_delay=None, hold=HOLD):
    rng = random.Random(seed)
    data, ends, full, records, la_counts = framed_with_gaps(rng, full_beats, max_chunk=max_chunk, record_delay=record_delay)
    dev = FakeGapDevice(data, ends, records, la_counts, rng=rng)
    stream = DeepAnalyzer(dev).stream(rle=False, gaps=True, gap_hold_bytes=hold)
    chunks = []
    for chunk in stream.chunks(60.0):
        chunks.append(chunk)
        if dev.written >= dev.ends[-1]:
            stream.stop()                                                  # 全部書いた: 残りを出し切って終える（時間を待たない）
    return dev, stream, chunks, full, records


# 中継のゼロコピーの道は 1 かたまり最大 STREAM_SEG_MAX_BEATS 拍（bandwidth §1.7.9）。かたまりの大きさに依らず同じ位置になること
@pytest.mark.parametrize("full_beats, max_chunk", [(1500, g.STREAM_PL_CHUNK_BEATS), (20000, g.STREAM_SEG_MAX_BEATS)])
@pytest.mark.parametrize("seed", range(6))
def test_stream_returns_gaps_at_the_positions_the_circuit_recorded(seed, full_beats, max_chunk):
    dev, stream, chunks, full, records = run_gap_stream(seed, full_beats, max_chunk)
    assert dev.control_writes[-2] >> g.LA_DEEP_CONTROL_GAPS[0] & 1           # 開始の書き込みに gaps
    assert dev.events_enabled                                                 # 欠けの記録はイベントのリングを通る
    st = stream.stats()
    assert [(x.beat, x.samples) for x in st.gaps] == records and st.unplaced_beats == 0
    assert not any(e.kind == KIND_LA_GAP for c in chunks for e in c.events)   # 欠けの記録はイベントに混ぜない
    pos = 0
    for c in chunks:
        if c.gap is not None:
            assert c.gap.start == pos and c.first == pos + c.gap.samples and c.previous is None
            pos = c.first
        assert c.first == pos
        values = list(c.samples())
        assert values == list(full[pos:pos + len(values)])                    # 位置の値 = 全体の値（欠けを埋めない）
        pos += len(values)
    assert st.samples + st.gap_samples == pos
    assert st.duration_s == pytest.approx(pos * 1e-8)


@pytest.mark.parametrize("delay", [0, 300, 2000])
def test_late_records_are_held_up_to_the_explicit_limit_and_placed_exactly(delay):
    # 記録が流れの上で delay 拍遅れて届く: その間の拍は位置が決まらないので持つ（持った量は事実として返す）。上限の中なら位置は正確
    dev, stream, chunks, full, records = run_gap_stream(3, 6000, record_delay=delay)
    st = stream.stats()
    assert [(x.beat, x.samples) for x in st.gaps] == records and st.unplaced_beats == 0
    assert st.gap_held_peak_bytes <= HOLD
    if delay >= 300:
        assert st.gap_held_peak_bytes >= delay * 16                       # 遅れの分を持った
    pos = 0
    for c in chunks:
        if c.gap is not None:
            pos = c.first
        values = list(c.samples())
        assert values == list(full[pos:pos + len(values)])
        pos += len(values)


def test_holding_more_than_the_limit_stops_with_the_facts_instead_of_guessing():
    from fprobe.la import StreamHoldExceeded
    limit = 500 * 16                                                     # 500 拍
    with pytest.raises(StreamHoldExceeded) as info:
        run_gap_stream(4, 6000, record_delay=2000, hold=limit)
    e = info.value
    assert e.hold_bytes == limit and e.held_bytes > limit and e.missing_records >= 1


def test_the_hold_limit_must_be_given_explicitly():
    rng = random.Random(12)
    data, ends, full, records, la_counts = framed_with_gaps(rng)
    dev = FakeGapDevice(data, ends, records, la_counts, rng=rng)
    for bad in (None, 0, -1):
        with pytest.raises(ValueError, match="gap_hold_bytes"):
            DeepAnalyzer(dev).stream(rle=False, gaps=True, gap_hold_bytes=bad)
    with pytest.raises(TypeError):
        GapPlacer(False, 0)                                                # 既定の暗黙の上限を持たない


def test_the_placer_does_not_copy_beats_that_arrive_in_one_piece():
    p = GapPlacer(False, 0, hold_bytes=HOLD)
    block = beats_of(range(64))                                          # 8 拍
    p.add_beats(memoryview(block))
    (piece,) = p.release(8, 0, 0)
    assert piece[0] == "data" and isinstance(piece[1], memoryview) and piece[1].obj is block


def test_without_gaps_the_stream_is_unchanged():
    rng = random.Random(9)
    data, ends, *_ = framed_with_gaps(rng)
    dev = FakeMixedDevice(data, ends, rng=rng)
    stream = DeepAnalyzer(dev).stream(rle=False)
    chunks = list(stream.chunks(5.0))
    assert not dev.control_writes[-2] >> g.LA_DEEP_CONTROL_GAPS[0] & 1
    assert all(c.gap is None for c in chunks) and stream.stats().gaps == ()
    # 欠けの記録はただのイベントとして届く（gaps でない流れで回路が出すことは無い。解読はできる）
    assert any(e.kind == KIND_LA_GAP for c in chunks for e in c.events)


def test_gaps_need_the_capability():
    rng = random.Random(10)
    data, ends, *_ = framed_with_gaps(rng)
    dev = FakeMixedDevice(data, ends, rng=rng)
    with pytest.raises(RuntimeError, match="la_stream_gaps"):
        DeepAnalyzer(dev).stream(rle=False, gaps=True, gap_hold_bytes=HOLD)


def test_gap_record_decodes_as_an_la_gap_access():
    (e,) = decode_events(gap_record(0x12345678, (1 << 40) - 1, t=99))
    assert (e.kind, e.address, e.data, e.time) == (KIND_LA_GAP, 0x12345678, (1 << 40) - 1, 99)
    (a,) = resolve([e])
    assert (a.kind, a.address, a.value) == ("la_gap", 0x12345678, (1 << 40) - 1)


def test_vcd_marks_the_gap_as_unknown(tmp_path):
    path = tmp_path / "g.vcd"
    with VcdWriter(path, 2, 1e-8) as vcd:
        vcd.write(array("H", [1, 2]), array("Q", [0, 5]))
        vcd.gap(10)
        vcd.write(array("H", [2]), array("Q", [30]))
    text = path.read_text()
    after = text.split("#100\n", 1)[1]
    assert after.startswith('x!\nx"\n#300\n0!\n1"')                       # 欠けで x、後の最初の値で全信号を書き直す


def test_recording_stream_puts_gaps_as_events_and_cuts_the_waveform(tmp_path):
    rng = random.Random(11)
    data, ends, full, records, la_counts = framed_with_gaps(rng, first_event=100)
    dev = FakeGapDevice(data, ends, records, la_counts, rng=rng)
    dev.events_enabled = True
    rec = Recorder(tmp_path / "r", timebase_hz=100e6)
    stream = LaRecordingStream(dev, rec, rle=False, chunk_s=1e-5, lock=threading.RLock(), events_from=100, gaps=True,
                               gap_hold_bytes=HOLD)
    stream.start()
    deadline = time.monotonic() + 5
    while dev.written < ends[-1] and time.monotonic() < deadline:
        time.sleep(0.005)
    status = stream.stop()
    assert status["gaps"] == len(records) and status["unplaced_beats"] == 0
    rec.close()
    view = Recording.open(tmp_path / "r")
    t0 = stream.anchor.ticks                                  # 1 サンプル = 1 刻み（100 MS/s・100 MHz）
    rows = [e for e in view.events(0, 1 << 62, streams=["la"]) if e.kind == "gap"]
    got = [(e.t - t0, e.body["samples"], e.body["beat"]) for e in rows]
    expected = [(b * SPB + sum(qq for bb, qq in records[:k]), q, b) for k, (b, q) in enumerate(records)]
    assert got == expected
    assert all(e.t_end - e.t == e.body["samples"] for e in rows)              # 欠けの区間（値の無い時間）
    # 欠けの区間に波形は無い: どのかたまりも欠けの区間と重ならない
    blocks = [(t - t0, t - t0 + round(tr.length * tps)) for t, tps, tr in view.waveforms()]
    assert blocks
    for start, q, _ in expected:
        assert not any(a < start + q and start < b for a, b in blocks), (start, q, blocks[:3])
