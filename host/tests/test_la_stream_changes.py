"""流れの拍から変化点への一括の計算（la_wave.stream_transitions・StreamChunk.changes）を、素朴な展開と比べる。

RAW の詰めた各 pack（16・8・4・2・1 ビットのサンプル）と RLE、直前のチャンクの値（無い・同じ・違う）、変化のまばら・毎サンプル、
1 語の中の変化の数の偏り（1 語に多い・少ない）を当てる。numpy の道と無い道の両方。
"""

import random
from array import array

import pytest

from fprobe import la, la_stream, la_wave
from fprobe.la import StreamChunk


@pytest.fixture(params=["numpy", "python"])
def numpy_mode(request, monkeypatch):
    if request.param == "python":
        monkeypatch.setattr(la_wave, "_np", None)
        monkeypatch.setattr(la_stream, "_np", None)
    return request.param


def ref_samples(data: bytes, rle: bool, pack: int) -> list[int]:
    if rle:
        out = []
        for i in range(0, len(data), 4):
            value = int.from_bytes(data[i:i + 2], "little")
            out += [value] * (int.from_bytes(data[i + 2:i + 4], "little") + 1)
        return out
    width = 16 >> pack
    bits = int.from_bytes(data, "little")
    return [bits >> (k * width) & ((1 << width) - 1) for k in range(len(data) * 8 // width)]


def ref_changes(samples, first, previous):
    values, starts = [], []
    for i, v in enumerate(samples):
        if v != previous:
            values.append(v)
            starts.append(first + i)
            previous = v
    return values, starts


def make_data(rng, rle, pack, beats, style):
    width = 16 >> pack
    if rle:
        records = []
        for _ in range(beats * 4):
            value = rng.getrandbits(16) if style != "sparse" else rng.choice((0, 1, 0x8000))
            run = rng.choice((0, 0, 1, 149, 0xFFFF, rng.getrandbits(16)))
            records.append(value | run << 16)
        return array("I", records).tobytes()
    count = beats * 128 // width
    if style == "every":
        samples = [rng.getrandbits(width) for _ in range(count)]
    elif style == "sparse":
        period = rng.choice((3, 64, 300, 1000))
        samples = [((1 << width) - 1) * ((k % period) >= period // 2) for k in range(count)]
    else:                                            # 語ごとに偏る: 変化の無い語・全部変わる語・1 つだけ
        samples, v = [], 0
        for _ in range(0, count, 64 // width):
            mode = rng.choice(("hold", "all", "one"))
            for j in range(64 // width):
                if mode == "all" or (mode == "one" and j == 7 % (64 // width)):
                    v = (v + 1 + rng.getrandbits(width)) % (1 << width) if width > 1 else v ^ 1
                samples.append(v)
    bits = 0
    for k, s in enumerate(samples):
        bits |= s << (k * width)
    return bits.to_bytes(beats * 16, "little")


@pytest.mark.parametrize("rle, pack", [(True, 0), (False, 0), (False, 1), (False, 2), (False, 3), (False, 4)])
@pytest.mark.parametrize("style", ["every", "sparse", "mixed"])
@pytest.mark.parametrize("prev", ["none", "same", "other"])
def test_stream_changes_are_the_runs_of_the_expanded_samples(rle, pack, style, prev, numpy_mode):
    rng = random.Random(f"{rle}-{pack}-{style}-{prev}")
    for beats in (1, 2, 7, 64):
        data = make_data(rng, rle, pack, beats, style)
        samples = ref_samples(data, rle, pack)
        previous = {"none": None, "same": samples[0], "other": samples[0] ^ 1}[prev]
        first = rng.choice((0, 1000, (1 << 40) + 3))
        chunk = StreamChunk(data, rle, first, previous, pack=pack)
        want = ref_changes(samples, first, previous)
        values, starts = chunk.transitions()
        assert isinstance(values, array) and isinstance(starts, array) and values.typecode == "H"
        assert (list(values), list(starts)) == want
        v, s = chunk.changes()
        assert (list(map(int, v)), list(map(int, s))) == want


def test_an_empty_chunk_has_no_changes(numpy_mode):
    for rle, pack in ((True, 0), (False, 0), (False, 4)):
        v, s = StreamChunk(b"", rle, 5, None, pack=pack).changes()
        assert len(v) == len(s) == 0
        v, s = StreamChunk(b"", rle, 5, None, pack=pack).transitions()
        assert len(v) == len(s) == 0


def test_changes_are_numpy_arrays_without_a_copy_to_array():
    np = pytest.importorskip("numpy")
    v, s = StreamChunk(bytes(range(32)), False, 0, None, pack=0).changes()
    assert isinstance(v, np.ndarray) and v.dtype == np.uint16
    assert isinstance(s, np.ndarray) and s.dtype == np.uint64
