"""ロジアナのストリームで、使うチャンネルだけ詰める（ケイパビリティ la_pack。CSR la_deep.control.pack）: 指定の検査・制御レジスタへの書き込み・
詰めた語の展開（偽のデバイス。ハードウェア不要）。選んだチャンネルは 1 サンプルも欠けない。"""

import random
from array import array

import pytest

from fprobe import generated as g
from fprobe.la import DeepAnalyzer, StreamChunk, decode_transitions
from test_la_stream import FakeStreamDevice
from caps_fakes import with_caps

PACK_SHIFT = g.LA_DEEP_CONTROL_PACK[0]


class PackDev(FakeStreamDevice):
    def __init__(self, *a, has_pack=True, **kw):
        super().__init__(*a, **kw)
        self.has_pack = has_pack

    def capabilities(self):
        return with_caps(super().capabilities(), la_pack=self.has_pack)


def packed_source(rng, beats, pack):
    """pack（1〜3）で詰めた拍の列と、元のサンプル列（下位 16 >> pack ビットだけ）を返す。語の中は時間の古いサンプルが下位。"""
    width, k = 16 >> pack, 1 << pack
    mask = (1 << width) - 1
    samples, value = [], 0
    while len(samples) < beats * 8 * k:
        samples += [value] * rng.randint(1, 30)
        value = rng.choice([value, rng.randrange(1 << width)])
    samples = samples[:beats * 8 * k]
    words = array("H", (sum(samples[i + j] << (width * j) for j in range(k)) for i in range(0, len(samples), k)))
    assert all(0 <= s <= mask for s in samples)
    return words.tobytes(), samples


@pytest.fixture(autouse=True)
def fast_reads(monkeypatch):
    monkeypatch.setattr("fprobe.la.LaStream.READ_MIN", 4096)
    monkeypatch.setattr("fprobe.la.LaStream.READ_MAX", 3000)


def start_value(dev):
    return [v for v in dev.control_writes if v & 1][-1]


@pytest.mark.parametrize("channels, code", [(None, 0), (8, 1), (4, 2), (2, 3)])
def test_pack_channels_sets_the_pack_field_of_the_start_write(channels, code):
    rng = random.Random(1)
    dev = PackDev(bytes(16 * 100), rng=rng)
    DeepAnalyzer(dev).stream(pack_channels=channels)
    assert start_value(dev) >> PACK_SHIFT & 3 == code


def test_pack_channels_is_refused_without_the_capability_and_for_values_that_do_not_fit():
    dev = PackDev(bytes(16 * 100), rng=random.Random(1), has_pack=False)
    with pytest.raises(RuntimeError, match="la_pack"):
        DeepAnalyzer(dev).stream(pack_channels=8)
    DeepAnalyzer(dev).stream()                                    # 詰めないなら、ケイパビリティが無くても流せる
    dev = PackDev(bytes(16 * 100), rng=random.Random(1))
    for bad in (1, 3, 5, 16, 0):
        with pytest.raises(ValueError, match="pack_channels"):
            DeepAnalyzer(dev).stream(pack_channels=bad)
    with pytest.raises(ValueError, match="rle"):                  # RLE は 16 ビットの値の繰り返しなので、詰めと一緒には使えない
        DeepAnalyzer(dev).stream(pack_channels=8, rle=True)


@pytest.mark.parametrize("channels, pack", [(8, 1), (4, 2), (2, 3)])
def test_packed_words_expand_to_every_sample_in_order(channels, pack):
    rng = random.Random(10 + pack)
    source, samples = packed_source(rng, 20000, pack)
    dev = PackDev(source, rng=rng)
    stream = DeepAnalyzer(dev).stream(pack_channels=channels)
    chunks = list(stream.chunks(0.1))
    got = array("H")
    for chunk in chunks:
        assert chunk.length == len(chunk.samples())               # length は展開しないで数えた値と一致
        got.extend(chunk.samples())
    assert list(got) == samples[:len(got)] and len(got) > 0
    assert stream.stats().samples == len(got)                    # サンプル数はサンプルで数える（語ではない）
    assert chunks[1].first == len(chunks[0].samples())            # 次のチャンクの first は、サンプルの通し番号


@pytest.mark.parametrize("pack", [1, 2, 3, 4])
@pytest.mark.parametrize("previous", [None, 0, 1])
def test_chunk_transitions_of_packed_words_match_the_expanded_samples(pack, previous):
    # 1 チャンクの詰めた語（pack 4 = 1 ch は 1 周期に複数のサンプルの構成だけ）: 変化点は展開したサンプルのもの。値の変わりにくい語も混ぜる
    rng = random.Random(40 + pack)
    words = array("H", (rng.choice((0, 0xFFFF, rng.getrandbits(16))) for _ in range(4096)))
    chunk = StreamChunk(words.tobytes(), rle=False, first=1000, previous=previous, pack=pack)
    values, starts, _ = decode_transitions(chunk.samples().tobytes(), False, 1000)
    values, starts = list(values), list(starts)
    if values[0] == previous:
        del values[0], starts[0]
    got_values, got_starts = chunk.transitions()
    assert list(got_values) == values and list(got_starts) == starts


@pytest.mark.parametrize("channels, pack", [(8, 1), (4, 2), (2, 3)])
def test_packed_transitions_are_those_of_the_expanded_samples(channels, pack):
    rng = random.Random(20 + pack)
    source, samples = packed_source(rng, 5000, pack)
    dev = PackDev(source, rng=rng)
    stream = DeepAnalyzer(dev).stream(pack_channels=channels)
    values, starts = array("H"), array("Q")
    for chunk in stream.chunks(0.1):
        v, s = chunk.transitions()
        values.extend(v)
        starts.extend(s)
    expanded = array("H", samples[:stream.stats().samples])
    want_values, want_starts, _ = decode_transitions(expanded.tobytes(), False)
    assert list(values) == list(want_values) and list(starts) == list(want_starts)


def vcd_wires(path):
    return [line.split()[4] for line in open(path, encoding="ascii") if line.startswith("$var")]


@pytest.mark.parametrize("channels, pack", [(8, 1), (4, 2), (2, 3)])
def test_stream_to_vcd_writes_only_the_selected_channels_and_every_transition(tmp_path, channels, pack):
    # 詰めて流したものを VCD に書くと、選んだチャンネルだけが並び（上位は記録されていない）、変化点は展開したサンプルのものと同じ
    rng = random.Random(30 + pack)
    source, samples = packed_source(rng, 3000, pack)
    dev = PackDev(source, rng=rng)
    analyzer = DeepAnalyzer(dev)
    stats = analyzer.stream_to_vcd(tmp_path / "s.vcd", 0.1, pack_channels=channels)
    wires = vcd_wires(tmp_path / "s.vcd")
    assert len(wires) == channels and wires == [f"ch{i}" for i in range(channels)], wires
    got = stats.samples
    assert got > 0 and got <= len(samples)
    want_values, want_starts, _ = decode_transitions(array("H", samples[:got]).tobytes(), False)
    changes = [line for line in open(tmp_path / "s.vcd", encoding="ascii") if line[:1] == "#"]
    assert len(changes) == len(want_starts), (len(changes), len(want_starts))


def test_stream_to_vcd_without_pack_keeps_all_the_channels(tmp_path):
    rng = random.Random(40)
    source, _ = packed_source(rng, 500, 1)
    dev = PackDev(source, rng=rng)
    DeepAnalyzer(dev).stream_to_vcd(tmp_path / "s.vcd", 0.05)
    assert len(vcd_wires(tmp_path / "s.vcd")) == 16
