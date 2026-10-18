"""fprobe.decode.spi: 発生器 → 解読の往復、CS、端の条件（ハードウェア不要）。"""

import random

import pytest

from fprobe.decode import decode_spi
from fprobe.decode.synth import Wave, spi

SCK, MOSI, MISO, CS = 0, 1, 6, 4
F = 1e6


def wave(mode=0, rate=F * 10, cs_active_low=True, **kw):
    return Wave(channels=10, sample_rate_hz=rate, idle={SCK: mode >> 1, CS: 1 if cs_active_low else 0}, **kw)


def words(d, kind):
    return [a.value for a in d.of(kind)]


@pytest.mark.parametrize("mode", [0, 1, 2, 3])
@pytest.mark.parametrize("msb_first", [True, False])
@pytest.mark.parametrize("word_bits", [8, 12, 16])
def test_round_trip_every_mode(mode, msb_first, word_bits):
    rng = random.Random(mode * 100 + word_bits)
    tx = [rng.randrange(1 << word_bits) for _ in range(6)]
    rx = [rng.randrange(1 << word_bits) for _ in range(6)]
    w = wave(mode)
    end = spi(w, 1e-6, sck=SCK, mosi=MOSI, miso=MISO, cs=CS, mosi_words=tx, miso_words=rx, clock_hz=F, mode=mode,
              word_bits=word_bits, msb_first=msb_first)
    d = decode_spi(w.build(end_s=end + 1e-6), sck=SCK, mosi=MOSI, miso=MISO, cs=CS, mode=mode, msb_first=msb_first,
                   word_bits=word_bits)
    assert words(d, "mosi") == tx and words(d, "miso") == rx and d.errors() == []
    (tr,) = d.of("transfer")
    assert tr.fields == {"mosi": tx, "miso": rx}


def test_wrong_phase_reads_shifted_data():
    # CPHA = 1 のデータは前縁で変わる。CPHA = 0 として前縁の直前を読むと 1 ビット前の値になる。
    # （逆に CPHA = 0 のデータは後縁で変わるので、どちらの辺の直前でも同じ値に見える: 実際の機器と同じ）
    w = wave(1)
    end = spi(w, 1e-6, sck=SCK, mosi=MOSI, cs=CS, mosi_words=[0x96, 0x3C], clock_hz=F, mode=1)
    t = w.build(end_s=end + 1e-6)
    assert words(decode_spi(t, sck=SCK, mosi=MOSI, cs=CS, mode=1), "mosi") == [0x96, 0x3C]
    assert words(decode_spi(t, sck=SCK, mosi=MOSI, cs=CS, mode=0), "mosi") == [0x96 >> 1, 0x3C >> 1]   # 前の語の最後のビット（0）が入る


def test_active_high_cs_and_several_transfers():
    w = wave(cs_active_low=False)
    t = spi(w, 1e-6, sck=SCK, mosi=MOSI, cs=CS, mosi_words=[1, 2], clock_hz=F, cs_active_low=False)
    t = spi(w, t + 3e-6, sck=SCK, mosi=MOSI, cs=CS, mosi_words=[3], clock_hz=F, cs_active_low=False)
    d = decode_spi(w.build(end_s=t + 1e-6), sck=SCK, mosi=MOSI, cs=CS, cs_active_low=False)
    assert [a.fields["mosi"] for a in d.of("transfer")] == [[1, 2], [3]]
    assert all(a.fields["miso"] == [] for a in d.of("transfer")) and d.of("miso") == []


def test_clock_while_cs_inactive_is_ignored():
    w = wave()
    t = spi(w, 1e-6, sck=SCK, mosi=MOSI, cs=CS, mosi_words=[0xFF], clock_hz=F, select=False, release=False)
    t = spi(w, t + 2e-6, sck=SCK, mosi=MOSI, cs=CS, mosi_words=[0xA5], clock_hz=F)
    d = decode_spi(w.build(end_s=t + 1e-6), sck=SCK, mosi=MOSI, cs=CS)
    assert words(d, "mosi") == [0xA5]


def test_cs_released_mid_word_gives_an_incomplete_word():
    w = wave()
    end = spi(w, 1e-6, sck=SCK, mosi=MOSI, cs=CS, mosi_words=[0x12, 0x34, 0x56], clock_hz=F)
    d = decode_spi(w.build(end_s=end + 1e-6), sck=SCK, mosi=MOSI, cs=CS, word_bits=16)
    m = d.of("mosi")
    assert (m[0].value, m[0].error) == (0x1234, None)
    assert (m[1].value, m[1].error, m[1].fields) == (0x56, "incomplete", {"bits": 8})


def test_capture_starting_and_ending_inside_a_transfer():
    w = wave()
    t = spi(w, 1e-6, sck=SCK, mosi=MOSI, cs=CS, mosi_words=[0x11, 0x22], clock_hz=F)
    t = spi(w, t + 2e-6, sck=SCK, mosi=MOSI, cs=CS, mosi_words=[0x33, 0x44, 0x55], clock_hz=F)
    d = decode_spi(w.build(start_s=1.5e-6, end_s=t - 9e-6), sck=SCK, mosi=MOSI, cs=CS)
    first, second = d.of("transfer")
    assert first.error == "truncated" and first.start == 0                  # CS は先頭ですでに有効
    assert second.error == "truncated" and second.end == d.length          # CS が有効なまま終わる
    assert second.fields["mosi"][:1] == [0x33]
    assert d.of("mosi")[-1].error == "incomplete"


def test_without_cs_words_are_counted_from_the_first_edge():
    w = wave(mode=3)
    end = spi(w, 1e-6, sck=SCK, mosi=MOSI, miso=MISO, mosi_words=[0xDE, 0xAD], miso_words=[0xBE, 0xEF],
              clock_hz=F, mode=3)
    d = decode_spi(w.build(end_s=end + 1e-6), sck=SCK, mosi=MOSI, miso=MISO, mode=3)
    assert words(d, "mosi") == [0xDE, 0xAD] and words(d, "miso") == [0xBE, 0xEF] and d.of("transfer") == []


@pytest.mark.parametrize("mode", [0, 1, 2, 3])
@pytest.mark.parametrize("rate", [2 * F, 2.6 * F, 3 * F])
def test_low_oversampling(mode, rate):
    rng = random.Random(int(rate) + mode)
    tx = [rng.randrange(256) for _ in range(50)]
    rx = [rng.randrange(256) for _ in range(50)]
    w = wave(mode, rate=rate)
    end = spi(w, 2e-6, sck=SCK, mosi=MOSI, miso=MISO, cs=CS, mosi_words=tx, miso_words=rx, clock_hz=F, mode=mode)
    d = decode_spi(w.build(end_s=end + 2e-6), sck=SCK, mosi=MOSI, miso=MISO, cs=CS, mode=mode)
    assert words(d, "mosi") == tx and words(d, "miso") == rx


def test_word_extent_and_text():
    w = wave()
    end = spi(w, 1e-6, sck=SCK, mosi=MOSI, miso=MISO, cs=CS, mosi_words=[0x0A], miso_words=[0xB0], clock_hz=F)
    d = decode_spi(w.build(end_s=end + 1e-6), sck=SCK, mosi=MOSI, miso=MISO, cs=CS)
    (m,) = d.of("mosi")
    assert m.text == "0x0A" and d.of("miso")[0].text == "0xB0"
    assert m.start == 20 and m.end == 91                        # 最初の辺（1.5 + 0.5 us）〜最後の読みの辺（9 us）+ 1
    assert d.of("transfer")[0].text == "MOSI 0A / MISO B0"


def test_bad_parameters():
    t = wave().build(end_s=1e-5)
    with pytest.raises(ValueError):
        decode_spi(t, sck=SCK, mosi=MOSI, mode=4)
    with pytest.raises(ValueError):
        decode_spi(t, sck=SCK)                                 # データの線が無い
    with pytest.raises(ValueError):
        decode_spi(t, sck=SCK, mosi=MOSI, word_bits=0)
