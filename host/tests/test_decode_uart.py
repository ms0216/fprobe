"""fprobe.decode.uart: 発生器 → 解読の往復、誤り、端の条件、ボーレートの推定（ハードウェア不要）。"""

import random
import time

import pytest

from fprobe.decode import decode_uart
from fprobe.decode.synth import Wave, uart, uart_break

BAUD = 115200
T0 = 2 / BAUD                     # 最初のフレームの前の休止（捕捉の先頭の変化は見えないため）
F8N1 = {"data_bits": 8, "parity": "none", "stop_bits": 1, "invert": False, "lsb_first": True}   # 枠の設定（必須）


def wave(rate=BAUD * 16, invert=False, ch=5, **kw):
    return Wave(channels=10, sample_rate_hz=rate, idle={ch: 0 if invert else 1}, **kw)


def data_of(d):
    return [a.value for a in d.of("data")]


@pytest.mark.parametrize("data_bits", [5, 6, 7, 8, 9])
@pytest.mark.parametrize("parity", ["none", "even", "odd", "mark", "space"])
def test_round_trip_every_frame_format(data_bits, parity):
    rng = random.Random(data_bits)
    data = [rng.randrange(1 << data_bits) for _ in range(40)]
    for stop_bits in (1, 1.5, 2):
        w = wave()
        end = uart(w, 5, data, 10e-6, baud=BAUD, data_bits=data_bits, parity=parity, stop_bits=stop_bits)
        d = decode_uart(w.build(end_s=end + 20e-6), 5, baud=BAUD, data_bits=data_bits, parity=parity,
                        stop_bits=stop_bits, invert=False, lsb_first=True)
        assert data_of(d) == data and d.errors() == []


def test_inverted_and_msb_first():
    data = list(b"Hello\x00\xff")
    w = wave(invert=True)
    end = uart(w, 5, data, T0, baud=BAUD, **(F8N1 | {"invert": True, "lsb_first": False}))
    d = decode_uart(w.build(end_s=end), 5, baud=BAUD, **(F8N1 | {"invert": True, "lsb_first": False}))
    assert data_of(d) == data and d.errors() == []


def test_annotation_extent_text_and_times():
    w = wave()
    end = uart(w, 5, b"A", T0, baud=BAUD, **F8N1)
    t = w.build(end_s=end + 1e-5, trigger_s=end)
    d = decode_uart(t, 5, baud=BAUD, **F8N1)
    (a,) = d.annotations
    assert a.kind == "data" and a.text == "0x41 'A'" and a.start == 32 and a.end == 192    # 10 ビット × 16 サンプル
    assert d.since_trigger(a.end) == pytest.approx(0, abs=1e-9)
    assert d.params["baud"] == BAUD and "baud_estimated" not in d.params


def test_parity_and_frame_errors_and_break():
    w = wave()
    t = uart(w, 5, [0x11, 0x22, 0x33], T0, baud=BAUD, parity="even", bad_parity={1})
    t = uart(w, 5, [0x44], t, baud=BAUD, parity="even", bad_stop={0})
    t = uart_break(w, 5, t + 5e-6, 300e-6)
    t = uart(w, 5, [0x55], t + 20e-6, baud=BAUD, **(F8N1 | {"parity": "even"}))
    d = decode_uart(w.build(end_s=t), 5, baud=BAUD, **(F8N1 | {"parity": "even"}))
    kinds = [(a.kind, a.value, a.error) for a in d]
    assert kinds == [("data", 0x11, None), ("data", 0x22, "parity"), ("data", 0x33, None), ("data", 0x44, "frame"),
                     ("break", None, None), ("data", 0x55, None)]
    brk = d.of("break")[0]
    assert (brk.end - brk.start) / 16 * (1 / BAUD) == pytest.approx(300e-6, rel=0.05)


def test_frame_error_then_resynchronizes():
    # 停止ビットが 0 のフレームの後、線が 1 に戻ってから同期し直す
    w = wave()
    t = uart(w, 5, [0x0F], T0, baud=BAUD, bad_stop={0})
    t = uart(w, 5, [0xF0, 0x5A], t + 3 / BAUD, baud=BAUD, **F8N1)
    d = decode_uart(w.build(end_s=t), 5, baud=BAUD, **F8N1)
    assert [(a.value, a.error) for a in d.of("data")][-2:] == [(0xF0, None), (0x5A, None)]
    assert d.of("data")[0].error == "frame"


def test_capture_starting_mid_frame_recovers_after_idle():
    data = list(range(0x30, 0x40))
    w = wave()
    end = uart(w, 5, data, T0, baud=BAUD, gap_bits=12)
    d = decode_uart(w.build(start_s=4.5 / BAUD, end_s=end), 5, baud=BAUD, **F8N1)   # 最初のフレームの途中から
    good = [a.value for a in d.of("data") if a.error is None]
    assert good[-15:] == data[1:]


def test_capture_ending_mid_frame_is_truncated():
    w = wave()
    end = uart(w, 5, [0x12, 0x34], T0, baud=BAUD, **F8N1)
    d = decode_uart(w.build(end_s=end - 5 / BAUD), 5, baud=BAUD, **F8N1)
    assert [(a.value, a.error) for a in d][0] == (0x12, None)
    last = d.annotations[-1]
    assert last.error == "truncated" and last.end == d.length


def test_glitch_on_idle_line_is_ignored():
    w = wave()
    w.set(5, 0, 1e-6)
    w.set(5, 1, 1e-6 + 1 / (BAUD * 16))           # 1 サンプルの 0（開始ビットの中央は 1）
    end = uart(w, 5, b"Z", 20e-6, baud=BAUD, **F8N1)
    assert data_of(decode_uart(w.build(end_s=end), 5, baud=BAUD, **F8N1)) == [ord("Z")]


# 4 倍での余裕（停止ビットで）: 変化点の量子化 0.125 + 中央の丸め 0.125 + クロックの差 9.5 × 1% + 揺れ 2 × 5% ≈ 0.45 ビット < 0.5
@pytest.mark.parametrize("oversampling", [4.0, 4.37, 5.5])
def test_low_oversampling_with_clock_error_and_jitter(oversampling):
    rng = random.Random(1)
    data = [rng.randrange(256) for _ in range(300)]
    w = wave(rate=BAUD * oversampling, jitter=0.05 / BAUD, seed=3)       # 変化の時刻を ±5% 揺らす
    end = uart(w, 5, data, T0, baud=BAUD * 1.01)                          # 送り手のクロックが 1% 速い
    d = decode_uart(w.build(end_s=end), 5, baud=BAUD, **F8N1)
    assert data_of(d) == data and d.errors() == []


def test_two_uarts_on_different_channels():
    w = Wave(channels=10, sample_rate_hz=BAUD * 16, idle={5: 1, 7: 1})
    uart(w, 5, b"ping", T0, baud=BAUD, **F8N1)
    end = uart(w, 7, b"pong!", 13e-6, baud=BAUD, **F8N1)
    t = w.build(end_s=end)
    assert bytes(data_of(decode_uart(t, 5, baud=BAUD, **F8N1))) == b"ping"
    assert bytes(data_of(decode_uart(t, 7, baud=BAUD, **F8N1))) == b"pong!"


def test_the_baud_is_required_and_never_guessed():
    # ボーレートを波形から推定しない（雑音とみなす割合・標準値に寄せる等の経験則が要るため。CLAUDE.md）。
    # 1 ビットの長さは la.measure 等でパルスの幅を測って、使う側が決める
    w = wave()
    end = uart(w, 5, b"hi", T0, baud=BAUD, **F8N1)
    t = w.build(end_s=end)
    with pytest.raises(TypeError, match="baud"):
        decode_uart(t, 5, **F8N1)
    assert decode_uart(t, 5, baud=BAUD, **F8N1).params["baud"] == BAUD


def test_bad_parameters_are_rejected():
    t = wave().build(end_s=1e-5)
    for kw in ({"data_bits": 4}, {"data_bits": 10}, {"parity": "weird"}, {"stop_bits": 3}, {"baud": 0}):
        with pytest.raises(ValueError):
            decode_uart(t, 5, **({"baud": BAUD} | F8N1 | kw))


def test_a_hundred_thousand_bytes_decode_quickly():
    rng = random.Random(7)
    data = bytes(rng.randrange(256) for _ in range(100_000))
    w = wave(rate=BAUD * 8)
    end = uart(w, 5, data, T0, baud=BAUD, **F8N1)
    t = w.build(end_s=end)
    t0 = time.process_time()
    d = decode_uart(t, 5, baud=BAUD, **F8N1)
    elapsed = time.process_time() - t0
    assert bytes(data_of(d)) == data
    assert elapsed < 5.0, elapsed                     # 手元で約 2 秒（送るのにかかる 8.7 秒より速い）
