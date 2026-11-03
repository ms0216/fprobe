"""生の保存ファイル（fprobe.la_raw。仕様 docs/superpowers/specs/2026-11-03-la-raw-capture-design.md）: 書いて読むと同じ・
壊れ方・書き出し（VCD）が今の道とバイト単位で同じ。"""

import random
import struct
from array import array

import pytest

from fprobe import la, la_vcd, la_wave
from fprobe.la import StreamChunk, StreamGap, Transitions
from fprobe.la_raw import (RawCapture, RawCaptureWriter, RawFormatError, RawMeta, raw_to_vcd)


def meta(**kw) -> RawMeta:
    base = dict(rle=False, pack=0, channels=16, first_channel=0, sample_period_s=1e-8, names={}, skew=(), timebase_hz=None)
    base.update(kw)
    return RawMeta(**base)


def raw_beats(rng, beats):
    samples, v = array("H"), 0
    while len(samples) < beats * 8:
        samples.extend(array("H", [v]) * rng.randint(1, 30))
        v = rng.choice([v, rng.randrange(1 << 16)])
    return samples[:beats * 8].tobytes()


def rle_beats(rng, beats):
    records, v = array("I"), 0
    for _ in range(beats * 4):
        v = rng.choice([v, rng.randrange(1 << 16)])
        records.append(rng.choice([0, rng.randrange(40)]) << 16 | v)
    return records.tobytes()


def chunks_of(data: bytes, rle: bool, sizes) -> list[StreamChunk]:
    """データを拍の境で切ったチャンク（LaStream と同じく first・previous・count を持たせる）。"""
    out, pos, first, previous = [], 0, 0, None
    for size in sizes:
        part = data[pos:pos + size * 16]
        if not part:
            break
        count, last = la._samples_of(part, rle, 0)
        out.append(StreamChunk(part, rle, first, previous, count=count))
        pos, first, previous = pos + len(part), first + count, last
    return out


def same(a, b) -> bool:
    def gap(x):
        return None if x.gap is None else (x.gap.start, x.gap.samples, x.gap.beat)
    return (bytes(a.data), a.first, a.previous, a.count, gap(a)) == (bytes(b.data), b.first, b.previous, b.count, gap(b))


def test_roundtrip_keeps_every_field(tmp_path):
    rng = random.Random(1)
    data = raw_beats(rng, 500)
    chunks = chunks_of(data, False, [100, 1, 250, 149])
    gap = StreamGap(start=chunks[1].first + chunks[1].count, samples=77, beat=101, drop_time48=5)
    chunks[2] = StreamChunk(chunks[2].data, False, chunks[2].first + 77, None, count=chunks[2].count, gap=gap)
    path = tmp_path / "a.fplaraw"
    with RawCaptureWriter(path, meta(names={0: "clk", 3: "d"}, skew=(0.0, None, 1.5), timebase_hz=1e8)) as w:
        for c in chunks:
            w.write(c)
    cap = RawCapture(path)
    assert cap.complete and len(cap) == len(chunks)
    assert cap.meta.names == {0: "clk", 3: "d"} and cap.meta.skew == (0.0, None, 1.5) and cap.meta.timebase_hz == 1e8
    got = list(cap.chunks())
    assert all(same(g, c) for g, c in zip(got, chunks))
    assert got[2].gap.start == gap.start and got[2].gap.samples == 77 and got[2].gap.beat == 101
    assert cap.samples == sum(c.count for c in chunks)


def test_writer_accepts_memoryview_and_empty_chunk(tmp_path):
    data = raw_beats(random.Random(2), 10)
    path = tmp_path / "m.fplaraw"
    with RawCaptureWriter(path, meta()) as w:
        w.write(StreamChunk(memoryview(data), False, 0, None, count=80))
        w.write(StreamChunk(b"", False, 80, 5, count=0))                 # 欠けで終わる・イベントだけのチャンク
    got = list(RawCapture(path).chunks())
    assert bytes(got[0].data) == data and got[1].data == b"" and got[1].previous == 5


def test_count_unknown_is_kept(tmp_path):
    path = tmp_path / "u.fplaraw"
    with RawCaptureWriter(path, meta()) as w:
        w.write(StreamChunk(raw_beats(random.Random(3), 2), False, 0, None))
    cap = RawCapture(path)
    assert next(iter(cap.chunks())).count == -1 and cap.samples == 16        # 数えていなければ読むとき数える


def test_empty_capture(tmp_path):
    path = tmp_path / "e.fplaraw"
    RawCaptureWriter(path, meta()).close()
    cap = RawCapture(path)
    assert cap.complete and len(cap) == 0 and list(cap.chunks()) == [] and cap.samples == 0


def test_without_index_is_scanned_and_marked_incomplete(tmp_path):
    rng = random.Random(4)
    chunks = chunks_of(raw_beats(rng, 300), False, [100, 100, 100])
    path = tmp_path / "x.fplaraw"
    w = RawCaptureWriter(path, meta())
    for c in chunks:
        w.write(c)
    w._file.flush()                                  # 閉じずに落ちた（索引が無い）
    cap = RawCapture(path)
    assert not cap.complete and len(cap) == 3
    assert all(same(g, c) for g, c in zip(cap.chunks(), chunks))
    w.close()
    assert RawCapture(path).complete


INDEX_BYTES = 3 * 8 + 24                              # 3 つのかたまりの索引（オフセット 3 つ + 末尾 24 バイト）


def test_truncated_index_only_keeps_every_chunk_but_is_incomplete(tmp_path):
    chunks = chunks_of(raw_beats(random.Random(6), 300), False, [100, 100, 100])
    path = tmp_path / "i.fplaraw"
    with RawCaptureWriter(path, meta()) as w:
        for c in chunks:
            w.write(c)
    path.write_bytes(path.read_bytes()[:-1])             # 索引の末尾が 1 バイト欠けた
    full = RawCapture(path)
    assert not full.complete and len(full) == 3 and all(same(g, c) for g, c in zip(full.chunks(), chunks))


@pytest.mark.parametrize("cut", [INDEX_BYTES + k for k in (1, 7, 30, 59, 60, 61, 200)])
def test_truncated_last_chunk_is_dropped_and_earlier_ones_are_kept(tmp_path, cut):
    rng = random.Random(5)
    chunks = chunks_of(raw_beats(rng, 300), False, [100, 100, 100])
    path = tmp_path / "t.fplaraw"
    w = RawCaptureWriter(path, meta())
    for c in chunks:
        w.write(c)
    w._file.flush()
    full = path.read_bytes()
    w.close()
    path.write_bytes(full[:-cut])
    cap = RawCapture(path)
    assert not cap.complete
    assert len(cap) == 2 and all(same(g, c) for g, c in zip(cap.chunks(), chunks))


def test_bad_magic_and_version(tmp_path):
    path = tmp_path / "b.fplaraw"
    path.write_bytes(b"NOTARAW!" + b"\0" * 64)
    with pytest.raises(RawFormatError):
        RawCapture(path)
    good = tmp_path / "g.fplaraw"
    RawCaptureWriter(good, meta()).close()
    blob = bytearray(good.read_bytes())
    blob[7] = 9                                      # 版
    path.write_bytes(bytes(blob))
    with pytest.raises(RawFormatError, match="版"):
        RawCapture(path)
    path.write_bytes(b"")
    with pytest.raises(RawFormatError):
        RawCapture(path)


def test_chunk_head_size_is_documented():
    from fprobe import la_raw
    assert struct.calcsize(la_raw.CHUNK_HEAD) == 60


# --- 書き出し: 今の道（chunk.changes() → VcdWriter）とバイト単位で同じ -------------------------------------------------

@pytest.fixture(params=["numpy", "python"])
def mode(request, monkeypatch):
    if request.param == "python":
        for module in (la_wave, la, la_vcd):
            monkeypatch.setattr(module, "_np", None)
    return request.param


def direct_vcd(path, chunks, m: RawMeta, names):
    """今の stream_to_vcd の書き出し（取り込みと書き出しを分ける前の道）。"""
    per = Transitions((), (), 0, 0, 0, m.sample_period_s, m.channels, m.skew)
    with la_vcd.VcdWriter(path, m.channels, m.sample_period_s, names,
                          shifts=per.skew_samples(m.sample_period_s * m.timebase_hz if m.timebase_hz else None),
                          unknown=per.skew_unknown()) as vcd:
        for c in chunks:
            if c.gap is not None:
                vcd.gap(c.gap.start)
            vcd.write(*c.changes())


@pytest.mark.parametrize("seed", range(12))
@pytest.mark.parametrize("rle", [False, True])
def test_raw_to_vcd_is_byte_identical_to_the_direct_path(tmp_path, mode, seed, rle):
    rng = random.Random(seed * 2 + rle)
    channels = rng.choice((1, 3, 8, 16))
    data = (rle_beats if rle else raw_beats)(rng, 400)
    sizes = [rng.randint(1, 120) for _ in range(12)]
    chunks = chunks_of(data, rle, sizes)
    skewed = seed % 2 == 1
    m = meta(rle=rle, channels=channels, sample_period_s=rng.choice((1e-8, 1.25e-9)),
             skew=tuple(rng.choice((0.0, 1.0, None, 2.5)) for _ in range(channels)) if skewed else (),
             timebase_hz=1e9 if skewed else None)
    if seed % 3 == 0 and len(chunks) > 3:                                    # 欠けを挟む
        c = chunks[2]
        gap = StreamGap(c.first, 40, 7, 0)
        chunks[2] = StreamChunk(c.data, rle, c.first + 40, None, count=c.count, gap=gap)
        chunks[3:] = [StreamChunk(x.data, rle, x.first + 40, x.previous, count=x.count) for x in chunks[3:]]
    names = {0: "clk"} if seed % 4 == 0 else None
    raw = tmp_path / "c.fplaraw"
    with RawCaptureWriter(raw, m) as w:
        for c in chunks:
            w.write(c)
    direct_vcd(tmp_path / "d.vcd", chunks, m, names)
    raw_to_vcd(RawCapture(raw), tmp_path / "r.vcd", names)
    assert (tmp_path / "r.vcd").read_bytes() == (tmp_path / "d.vcd").read_bytes()
