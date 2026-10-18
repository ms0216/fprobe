"""fprobe.decode の I2S・パラレルバス・Manchester: 発生器 → 解読の往復と端の条件（ハードウェア不要）。"""

import random

import pytest

from fprobe.decode import decode
from fprobe.decode.i2s import decode_i2s
from fprobe.decode.manchester import decode_manchester
from fprobe.decode.parallel import decode_parallel
from fprobe.decode.synth import Wave, i2s, manchester

SCK, WS, SD = 0, 1, 2
F = 1e6


# ---- I2S ----

def i2s_wave(frames, **kw):
    w = Wave(channels=4, sample_rate_hz=F * 8, idle={SCK: 0})
    end = i2s(w, 1e-6, sck=SCK, ws=WS, sd=SD, clock_hz=F, frames=frames, **kw)
    return w.build(end_s=end + 1e-6)


def pairs(d):
    left, right = [a.value for a in d.of("left")], [a.value for a in d.of("right")]
    return left, right


@pytest.mark.parametrize("format", ["i2s", "left_justified", "right_justified"])
def test_i2s_formats_round_trip_signed_samples(format):
    rng = random.Random(len(format))
    frames = [(rng.randrange(-32768, 32768), rng.randrange(-32768, 32768)) for _ in range(6)]
    tr = i2s_wave(frames, word_bits=16, slot_bits=24, format=format)
    d = decode_i2s(tr, sck=SCK, ws=WS, sd=SD, format=format, word_bits=16)
    left, right = pairs(d)
    # 最初のスロット（始まりが見えない）は読まないので、左の 1 つ目が抜けることがある
    assert left == [f[0] for f in frames][-len(left):] and right == [f[1] for f in frames][-len(right):]
    assert len(left) + len(right) >= 2 * len(frames) - 2
    assert all(a.fields["bits"] == 24 for a in d.of("left", "right") if a.error is None)


def test_i2s_word_bits_defaults_to_the_slot_and_unsigned():
    tr = i2s_wave([(0x1234, 0xFEDC), (0x0F0F, 0xF0F0), (1, 2)], word_bits=16)
    d = decode("i2s", tr, sck=SCK, ws=WS, sd=SD, signed=False)
    left, right = pairs(d)
    assert right == [0xFEDC, 0xF0F0, 2][-len(right):] and 0x0F0F in left
    with pytest.raises(ValueError):
        decode_i2s(tr, sck=SCK, ws=WS, sd=SD, format="right_justified")


# ---- パラレル ----

def test_parallel_with_clock_and_enable():
    data_ch, clk, en = [0, 1, 2, 3], 4, 5
    w = Wave(channels=8, sample_rate_hz=10e6, idle={en: 1})
    words = [0x3, 0xA, 0x5, 0xF, 0x0]
    t = 1e-6
    for k, v in enumerate(words):
        w.set(en, 0 if k != 2 else 1, t)                       # 3 つ目は無効の間
        for i, ch in enumerate(data_ch):
            w.set(ch, v >> i & 1, t)
        w.set(clk, 1, t + 0.5e-6)
        w.set(clk, 0, t + 1e-6)
        t += 1e-6
    tr = w.build(end_s=t + 1e-6)
    d = decode_parallel(tr, data=data_ch, clock=clk, enable=en)
    assert [a.value for a in d.of("word")] == [0x3, 0xA, 0xF, 0x0]
    both = decode_parallel(tr, data=data_ch, clock=clk, edge="both")
    assert len(both.of("word")) == 2 * len(words)
    with pytest.raises(ValueError):
        decode_parallel(tr, data=[0, 0])


def test_parallel_without_clock_skips_skewed_intermediate_values():
    w = Wave(channels=4, sample_rate_hz=100e6)
    t = 1e-6
    for v in (0b11, 0b00, 0b10):
        w.set(0, v & 1, t)
        w.set(1, v >> 1 & 1, t + 20e-9)                       # 2 本目が 2 サンプル遅れる
        t += 1e-6
    tr = w.build(end_s=t)
    assert [a.value for a in decode_parallel(tr, data=[0, 1], settle=3).of("word")] == [0b11, 0b00, 0b10]
    assert len(decode_parallel(tr, data=[0, 1]).of("word")) > 3   # 途中の値も出る


# ---- Manchester ----

@pytest.mark.parametrize("convention", ["ieee", "thomas"])
@pytest.mark.parametrize("idle", [0, 1])
def test_manchester_round_trip(convention, idle):
    rng = random.Random(7)
    frames = [[rng.randrange(2) for _ in range(24)], [1] * 8 + [0] * 8, [0, 1] * 8]
    w = Wave(channels=2, sample_rate_hz=1e6 * 16, idle={0: idle}, jitter=0.03e-6)
    t = 2e-6
    for bits in frames:
        t = manchester(w, 0, bits, t, bitrate=1e6, convention=convention, idle=idle) + 5e-6
    d = decode_manchester(w.build(end_s=t), 0, bitrate=1e6, convention=convention)
    got = [a.text for a in d.of("frame")]
    assert got[0] == "".join(map(str, frames[0])) and d.errors() == []
    assert got[1:] == ["".join(map(str, f)) for f in frames[1:]]
    words = [a.value for a in d.of("word")][:3]
    assert words == [int("".join(map(str, frames[0][i:i + 8])), 2) for i in (0, 8, 16)]


def test_manchester_timing_error_and_bad_params():
    w = Wave(channels=1, sample_rate_hz=16e6)
    t = manchester(w, 0, [1, 0, 1, 1], 2e-6, bitrate=1e6)
    w.set(0, 1, t + 0.1e-6)                                    # 半ビットより短い間
    w.set(0, 0, t + 0.2e-6)
    tr = w.build(end_s=t + 3e-6)
    d = decode_manchester(tr, 0, bitrate=1e6)
    assert d.errors() and d.of("frame")[0].error == "timing"
    with pytest.raises(ValueError):
        decode_manchester(tr, 0, bitrate=1e6, convention="nrz")
