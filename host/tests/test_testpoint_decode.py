"""テストポイント: 解読（GPIO 並列・2 線、ITM、RTT）。synth で作った波形・バイト列から ID の列と時刻を取り出す。"""

import pytest

from fprobe.decode.synth import Wave
from fprobe.itm import ItmDecoder as ItmPackets, Overflow, Timestamper
from fprobe.testpoint import synth
from fprobe.testpoint.decode import (ItmDecoder, ParallelDecoder, RttDecoder, SerialDecoder, decode_gpio_parallel,
                                     decode_gpio_serial, decode_itm, decode_rtt)
from fprobe.testpoint.trace import Hit, Loss

RATE = 100e6
RECORDS = [(1e-6, 1, None), (3e-6, 0x1234, None), (6e-6, 7, 0xDEADBEEF), (12e-6, 0xFFFF, 0), (20e-6, 2, None)]


def ids(items):
    return [(i.id, i.value) for i in items if isinstance(i, Hit)]


# --- 並列 ---

@pytest.mark.parametrize("data", [[1, 5, 6], [0, 1, 2, 3, 5, 6, 7], [2, 3]])
def test_parallel_roundtrip(data):
    wave = Wave(8, RATE)
    synth.gpio_parallel(wave, RECORDS, data, strobe_channel=4)
    dec = decode_gpio_parallel(wave.build(end_s=30e-6), data, 4)
    assert ids(dec.items) == [(i, v) for _, i, v in RECORDS]
    assert not dec.losses and dec.hz == RATE
    # 時刻は最初の記号のストローブの立ち上がり（書き込みの 50 ns 後）
    assert [h.t for h in dec.hits] == [round((t + 50e-9) * RATE) for t, _, _ in RECORDS]


def test_parallel_capture_starting_mid_record_resyncs():
    wave = Wave(8, RATE)
    synth.gpio_parallel(wave, RECORDS, [1, 5, 6], 4)
    tr = wave.build(start_s=6.3e-6, end_s=30e-6)            # 値付きの記録の途中から
    dec = ParallelDecoder([1, 5, 6], 4)
    items = dec.feed(tr) + dec.flush()
    assert ids(items) == [(0xFFFF, 0), (2, None)] and dec.unsynced > 0


def test_parallel_chunks_split_anywhere():
    wave = Wave(8, RATE)
    synth.gpio_parallel(wave, RECORDS, [1, 5, 6], 4)
    whole = wave.build(end_s=30e-6)
    for cut_s in (1.05e-6, 6.2e-6, 12.31e-6):
        dec = ParallelDecoder([1, 5, 6], 4)
        a = wave.build(end_s=cut_s)
        b = wave.build(start_s=cut_s, end_s=30e-6)
        items = dec.feed(a, 0) + dec.feed(b, a.length) + dec.flush()
        assert items == decode_gpio_parallel(whole, [1, 5, 6], 4).items


def test_parallel_truncated_record_is_a_loss():
    wave = Wave(8, RATE)
    synth.gpio_parallel(wave, [(1e-6, 7, 0x12345678)], [1, 5, 6], 4)
    tr = wave.build(end_s=1.5e-6)                            # 値の途中で捕捉が終わる
    dec = decode_gpio_parallel(tr, [1, 5, 6], 4)
    assert not dec.hits and len(dec.losses) == 1


# --- 2 線 ---

def test_serial_roundtrip():
    wave = Wave(8, RATE, idle={4: 1, 5: 1})
    synth.gpio_serial(wave, RECORDS, clock_channel=4, data_channel=5)
    dec = decode_gpio_serial(wave.build(end_s=30e-6), 4, 5, guard=1)
    assert ids(dec.items) == [(i, v) for _, i, v in RECORDS] and not dec.losses
    assert [h.t for h in dec.hits] == [round(t * RATE) for t, _, _ in RECORDS]


def test_serial_tolerates_pin_skew():
    """DAT と CLK の同じ書き込みの変化が ±4 ns ずれても（100 MS/s で 1 サンプルずれることがある）誤らない。"""
    wave = Wave(8, RATE, idle={4: 1, 5: 1}, jitter=4e-9, seed=3)
    records = [(1e-6 + k * 12e-6, (k * 2654435761) & 0xFFFF or 1, k if k % 3 == 0 else None) for k in range(40)]
    synth.gpio_serial(wave, records, 4, 5)
    dec = decode_gpio_serial(wave.build(end_s=records[-1][0] + 12e-6), 4, 5, guard=1)
    assert ids(dec.items) == [(i, v) for _, i, v in records] and not dec.losses


def test_serial_chunks_and_mid_record_start():
    wave = Wave(8, RATE, idle={4: 1, 5: 1})
    synth.gpio_serial(wave, RECORDS, 4, 5)
    a = wave.build(start_s=1.5e-6, end_s=6.4e-6)            # 最初の記録の途中から、3 つ目の途中まで
    b = wave.build(start_s=6.4e-6, end_s=30e-6)
    dec = SerialDecoder(4, 5, guard=1)
    items = dec.feed(a, 150) + dec.feed(b, 150 + a.length) + dec.flush()
    assert ids(items) == [(0x1234, None), (7, 0xDEADBEEF), (0xFFFF, 0), (2, None)]
    assert [h.t for h in items if isinstance(h, Hit)][0] == 300


def test_serial_record_without_stop_is_a_loss():
    wave = Wave(8, RATE, idle={4: 1, 5: 1})
    synth.gpio_serial(wave, [(1e-6, 5, None)], 4, 5)
    dec = decode_gpio_serial(wave.build(end_s=2e-6), 4, 5, guard=1)
    assert not dec.hits and len(dec.losses) == 1


# --- ITM ---

def test_itm_from_bytes_with_local_timestamps():
    records = [(100, 1, None), (250, 9, 0xCAFEBABE), (100_000, 3, None)]
    raw = synth.itm_bytes(records, port=1)
    packets = ItmPackets().feed(raw)
    timed = Timestamper().feed(packets)
    dec = decode_itm(timed, hz=16e6, port=1)
    assert [(h.t, h.id, h.value) for h in dec.hits] == [(100, 1, None), (250, 9, 0xCAFEBABE), (100_000, 3, None)]


def test_itm_other_ports_ignored_and_overflow_is_a_loss():
    raw = synth.itm_bytes([(10, 4, None)], port=1, timestamps=False) + \
        synth.itm_bytes([(20, 5, 77)], port=0, timestamps=False, sync=False)
    packets = ItmPackets().feed(raw)
    dec = ItmDecoder(port=1)
    items = dec.feed([(7, p) for p in packets] + [(8, Overflow())])
    assert items == [Hit(7, 4, None), Loss(8, "ITM のオーバーフロー")]


def test_itm_decode_errors_and_resyncs_are_losses():
    # 解読の誤り・同期の取り直し（バイトを捨てた）は、預かった値を捨てて Loss にする（誤りから同期までで 1 回）
    from fprobe.itm import DecodeError, Instrumentation, Sync
    dec = ItmDecoder(port=1)
    items = dec.feed([(1, Instrumentation(1, 77, 4)), (2, DecodeError("x")), (3, Sync(skipped=5)),
                      (4, Instrumentation(1, 6, 2))])
    assert items == [Loss(2, "SWO のバイトを解読できない（同期を取り直す）"), Hit(4, 6, None)]
    assert dec.feed([(5, Sync())]) == []                    # 捨てていない同期は何もしない
    assert [type(i).__name__ for i in ItmDecoder().feed([(6, Sync(skipped=3))])] == ["Loss"]   # 途中から受けた


def test_itm_value_without_id():
    raw = synth.itm_bytes([(10, 4, 1)], timestamps=False)[:-3]      # ID の書き込みを落とす
    dec = decode_itm(ItmPackets().feed(raw), hz=1.0)
    assert not dec.hits and len(dec.losses) == 1


# --- RTT ---

def test_rtt_roundtrip_with_wraparound_and_split_feeds():
    records = [(0xFFFF_FF00, 1, None), (0x10, 2, 5), (0x2000, 3, None)]      # 2 つ目で回り込む
    raw = synth.rtt_bytes(records)
    dec = RttDecoder()
    items = []
    for k in range(0, len(raw), 5):                          # 5 バイトずつ
        items += dec.feed(raw[k:k + 5])
    items += dec.flush()
    assert items == [Hit(0xFFFF_FF00, 1, None), Hit((1 << 32) + 0x10, 2, 5), Hit((1 << 32) + 0x2000, 3, None)]
    # 回り込みは「記録の間が 2^32 クロックより短い」前提で戻した: 仮定した回数を結果に出す（黙ってずらさない）
    assert dec.wraps == 1
    assert decode_rtt(raw, hz=1.0).wraps == 1 and decode_rtt(synth.rtt_bytes(records[1:]), hz=1.0).wraps == 0


def test_rtt_resync_after_garbage():
    raw = synth.rtt_bytes([(100, 1, None)]) + b"\x01\x02\x03" + synth.rtt_bytes([(200, 2, None)])
    dec = decode_rtt(raw, hz=84e6)
    assert ids(dec.items) == [(1, None), (2, None)] and len(dec.losses) == 1


def test_rtt_truncated_tail():
    dec = decode_rtt(synth.rtt_bytes([(100, 1, 9)])[:-2], hz=1.0)
    assert not dec.hits and len(dec.losses) == 1


def test_the_guard_is_given_by_the_user():
    # 総点検 S6: CLK と DAT のずれを何サンプルまで許すか（guard）は暗黙の既定にしない
    with pytest.raises(TypeError):
        SerialDecoder(4, 5)
    with pytest.raises(TypeError):
        decode_gpio_serial(Wave(channels=8, sample_rate_hz=1e6).build(end_s=1e-6), 4, 5)
