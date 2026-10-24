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
    d = decode_i2s(tr, sck=SCK, ws=WS, sd=SD, format=format, word_bits=16, signed=True)
    left, right = pairs(d)
    # 最初のスロット（始まりが見えない）は読まないので、左の 1 つ目が抜けることがある
    assert left == [f[0] for f in frames][-len(left):] and right == [f[1] for f in frames][-len(right):]
    assert len(left) + len(right) >= 2 * len(frames) - 2
    assert all(a.fields["bits"] == 24 for a in d.of("left", "right") if a.error is None)


def test_i2s_word_bits_defaults_to_the_slot_and_unsigned():
    tr = i2s_wave([(0x1234, 0xFEDC), (0x0F0F, 0xF0F0), (1, 2)], word_bits=16)
    d = decode("i2s", tr, sck=SCK, ws=WS, sd=SD, signed=False, format="i2s", word_bits=None)
    left, right = pairs(d)
    assert right == [0xFEDC, 0xF0F0, 2][-len(right):] and 0x0F0F in left
    with pytest.raises(ValueError):
        decode_i2s(tr, sck=SCK, ws=WS, sd=SD, format="right_justified", word_bits=None, signed=True)


def test_i2s_last_slot_without_its_end_is_truncated_whatever_its_length():
    """捕捉の終わりで WS が変わっていない最後のスロットは、終わりが見えていない（truncated）。前のスロットと同じ
    ビット数かで「完全」と決めない（スロット長が一定という仮定を使わない）。読めたビット数は fields.bits。"""
    tr = i2s_wave([(1, 2), (3, 4), (5, 6)], word_bits=16, format="left_justified")
    d = decode_i2s(tr, sck=SCK, ws=WS, sd=SD, format="left_justified", word_bits=16, signed=False)
    last = d.of("left", "right")[-1]
    assert last.value == 6 and last.fields["bits"] == 16 and last.error == "truncated"
    assert all(a.error is None for a in d.of("left", "right")[:-1])


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
    d = decode_parallel(tr, data=data_ch, clock=clk, edge="rising", enable=en, enable_level=0)
    assert [a.value for a in d.of("word")] == [0x3, 0xA, 0xF, 0x0]
    both = decode_parallel(tr, data=data_ch, clock=clk, edge="both", enable=None)
    assert len(both.of("word")) == 2 * len(words)
    with pytest.raises(ValueError):
        decode_parallel(tr, data=[0, 0], clock=None, enable=None)


def test_parallel_without_clock_skips_skewed_intermediate_values():
    w = Wave(channels=4, sample_rate_hz=100e6)
    t = 1e-6
    for v in (0b11, 0b00, 0b10):
        w.set(0, v & 1, t)
        w.set(1, v >> 1 & 1, t + 20e-9)                       # 2 本目が 2 サンプル遅れる
        t += 1e-6
    tr = w.build(end_s=t)
    assert [a.value for a in decode_parallel(tr, data=[0, 1], settle=3, clock=None,
                                             enable=None).of("word")] == [0b11, 0b00, 0b10]
    assert len(decode_parallel(tr, data=[0, 1], clock=None, enable=None).of("word")) > 3   # 途中の値も出る


# ---- Manchester ----

@pytest.mark.parametrize("convention", ["ieee", "thomas"])
@pytest.mark.parametrize("idle", [0, 1])
def test_manchester_round_trip(convention, idle):
    rng = random.Random(7)
    frames = [[rng.randrange(2) for _ in range(24)], [1] * 8 + [0] * 8, [0, 1] * 8]   # どれも長い間を含む
    w = Wave(channels=2, sample_rate_hz=1e6 * 16, idle={0: idle}, jitter=0.03e-6)
    t = 2e-6
    for bits in frames:
        t = manchester(w, 0, bits, t, bitrate=1e6, convention=convention, idle=idle) + 5e-6
    d = decode_manchester(w.build(end_s=t), 0, bitrate=1e6, convention=convention, tolerance=0.25, word_bits=8,
                          msb_first=True)
    got = [a.text for a in d.of("frame")]
    assert all(0 <= a.fields["max_deviation"] < 0.1 for a in d.of("frame"))      # 揺らぎ 0.03 µs = 0.03 ビット + 量子化
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
    d = decode_manchester(tr, 0, bitrate=1e6, tolerance=0.2, convention="ieee", word_bits=8, msb_first=True)
    assert d.errors() and d.of("frame")[0].error == "timing"
    with pytest.raises(ValueError):
        decode_manchester(tr, 0, bitrate=1e6, convention="nrz", tolerance=0.2, word_bits=8, msb_first=True)
    with pytest.raises(TypeError, match="tolerance"):              # 許容幅は使う側が明示する（既定の経験則を持たない）
        decode_manchester(tr, 0, bitrate=1e6, convention="ieee", word_bits=8, msb_first=True)
    with pytest.raises(ValueError):                                # 短と長の窓が重なる幅は受けない
        decode_manchester(tr, 0, bitrate=1e6, tolerance=0.3, convention="ieee", word_bits=8, msb_first=True)


def test_manchester_frame_without_a_long_interval_has_unknown_phase():
    # 同じビットだけのフレームは短い間しか無く、最初の変化が中央か境目か分からない。中央とみなして解読しない
    w = Wave(channels=1, sample_rate_hz=16e6)
    t = manchester(w, 0, [1] * 8, 2e-6, bitrate=1e6)
    d = decode_manchester(w.build(end_s=t + 3e-6), 0, bitrate=1e6, tolerance=0.25, convention="ieee", word_bits=8,
                          msb_first=True)
    (frame,) = d.of("frame")
    assert frame.error == "phase_unknown" and d.of("bit") == [] and d.of("word") == []



def test_manchester_after_a_timing_error_waits_for_a_long_interval_instead_of_assuming_the_phase():
    # 誤りの後に「次の変化を中央」とみなして合わせ直さない。次の長い間（両端が中央）で位相を決め直すまで解読しない。
    # 出したビットはすべて正しい（推測したビットを出さない）
    bits = [1, 1, 1, 1, 0, 1, 0, 1, 1, 0]
    t0, T = 2e-6, 1e-6
    w = Wave(channels=1, sample_rate_hz=64e6)
    end = manchester(w, 0, bits, t0, bitrate=1e6)
    level_at_glitch = 0                                   # ieee の 1 = 前半 0: 1 ビット目の前半の中に短い山を入れる
    w.set(0, 1 - level_at_glitch, t0 + 1 * T + 0.10e-6)
    w.set(0, level_at_glitch, t0 + 1 * T + 0.15e-6)
    d = decode_manchester(w.build(end_s=end + 3e-6), 0, bitrate=1e6, tolerance=0.2, convention="ieee", word_bits=8,
                          msb_first=True)
    sp = d.sample_period_s
    out = [(int(((a.start + a.end) / 2 * sp - t0) // T), a.value) for a in d.of("bit")]
    assert d.of("frame")[0].error == "timing" and out
    assert all(bits[i] == v for i, v in out), out
    # 語は抜けをまたいでつながない: どの語も、続いた番号のビットだけから
    for word in d.of("word"):
        inside = [i for i, _ in out if word.start * sp <= t0 + (i + 0.5) * T <= word.end * sp]
        assert inside == list(range(inside[0], inside[0] + len(inside))), (word, inside)
