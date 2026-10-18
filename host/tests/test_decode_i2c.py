"""fprobe.decode.i2c: 発生器 → 解読の往復、ACK/NACK、10 ビットのアドレス、引き伸ばし、端の条件（ハードウェア不要）。"""

import random

import pytest

from fprobe.decode import decode_i2c
from fprobe.decode.synth import Wave, i2c

SCL, SDA = 1, 2
F = 100e3


def wave(rate=F * 20, **kw):
    return Wave(channels=10, sample_rate_hz=rate, idle={SCL: 1, SDA: 1}, **kw)


def summary(d):
    return [(a.fields["rw"], a.fields["address"], a.fields["data"], a.error) for a in d.of("transfer")]


def test_write_then_read_with_repeated_start():
    w = wave()
    t = i2c(w, 10e-6, scl=SCL, sda=SDA, clock_hz=F, address=0x50, data=[0x00, 0x10], stop=False)
    t = i2c(w, t, scl=SCL, sda=SDA, clock_hz=F, address=0x50, read=True, data=[0xAA, 0xBB, 0xCC], restart=True)
    d = decode_i2c(w.build(end_s=t + 10e-6), scl=SCL, sda=SDA)
    assert summary(d) == [("W", 0x50, [0x00, 0x10], None), ("R", 0x50, [0xAA, 0xBB, 0xCC], None)]
    assert [a.kind for a in d.of("start", "restart", "stop")] == ["start", "restart", "stop"]
    assert [(a.value, a.fields) for a in d.of("address")] == [(0x50, {"rw": "W", "ten_bit": False, "ack": True}),
                                                             (0x50, {"rw": "R", "ten_bit": False, "ack": True})]
    assert [a.fields["ack"] for a in d.of("data")] == [True, True, True, True, False]   # 読みの最後は NACK（正常）
    assert len(d.of("nack")) == 1 and d.errors() == []
    assert d.of("address")[0].text == "W 0x50" and d.of("transfer")[1].text == "R 0x50: AA BB CC"


def test_address_nack_is_an_error():
    w = wave()
    t = i2c(w, 10e-6, scl=SCL, sda=SDA, clock_hz=F, address=0x3C, address_ack=False)
    d = decode_i2c(w.build(end_s=t + 10e-6), scl=SCL, sda=SDA)
    (tr,) = d.of("transfer")
    assert tr.error == "nack" and tr.fields["ack"] is False and "NACK" in tr.text
    assert d.of("address")[0].fields["ack"] is False


def test_data_nack_on_write():
    w = wave()
    t = i2c(w, 10e-6, scl=SCL, sda=SDA, clock_hz=F, address=0x20, data=[1, 2, 3], acks=[True, False, True])
    d = decode_i2c(w.build(end_s=t + 10e-6), scl=SCL, sda=SDA)
    assert [a.fields["ack"] for a in d.of("data")] == [True, False, True]
    assert d.of("transfer")[0].error == "nack"            # 書きのデータの NACK は誤り


def test_ten_bit_address_write_and_read():
    w = wave()
    t = i2c(w, 10e-6, scl=SCL, sda=SDA, clock_hz=F, address=0x2A5, ten_bit=True, data=[0x01], stop=False)
    t = i2c(w, t, scl=SCL, sda=SDA, clock_hz=F, address=0x2A5, ten_bit=True, read=True, data=[0x77], restart=True)
    d = decode_i2c(w.build(end_s=t + 10e-6), scl=SCL, sda=SDA)
    assert summary(d) == [("W", 0x2A5, [0x01], None), ("R", 0x2A5, [0x77], None)]
    assert all(a.fields["ten_bit"] for a in d.of("address"))
    assert d.of("address")[0].text == "W 0x2A5"


def test_clock_stretching():
    w = wave()
    t = i2c(w, 10e-6, scl=SCL, sda=SDA, clock_hz=F, address=0x68, data=[0x75, 0x00], stretch_s=137e-6)
    assert summary(decode_i2c(w.build(end_s=t + 10e-6), scl=SCL, sda=SDA)) == [("W", 0x68, [0x75, 0x00], None)]


def test_capture_starting_mid_transaction_waits_for_start():
    w = wave()
    t = i2c(w, 10e-6, scl=SCL, sda=SDA, clock_hz=F, address=0x50, data=[0xFF, 0x00, 0x55])
    t = i2c(w, t + 20e-6, scl=SCL, sda=SDA, clock_hz=F, address=0x51, data=[0x42])
    d = decode_i2c(w.build(start_s=150e-6, end_s=t + 10e-6), scl=SCL, sda=SDA)
    assert summary(d) == [("W", 0x51, [0x42], None)]


def test_capture_ending_mid_byte():
    w = wave()
    t = i2c(w, 10e-6, scl=SCL, sda=SDA, clock_hz=F, address=0x50, data=[0x12, 0x34])
    d = decode_i2c(w.build(end_s=t - 60e-6), scl=SCL, sda=SDA)          # 最後のバイトの途中で終わる
    (tr,) = d.of("transfer")
    assert tr.error == "truncated" and tr.fields["data"] == [0x12] and tr.end == d.length
    assert d.of("data")[-1].error == "incomplete"


def test_stop_in_the_middle_of_a_byte():
    w = wave()
    h = 1 / (2 * F)
    t = i2c(w, 10e-6, scl=SCL, sda=SDA, clock_hz=F, address=0x50, stop=False)
    for b in (1, 0, 1):                                                    # 3 ビットだけ送って STOP
        w.set(SDA, b, t + h / 2)
        w.set(SCL, 1, t + h)
        w.set(SCL, 0, t + 2 * h)
        t += 2 * h
    w.set(SDA, 0, t + h / 2)
    w.set(SCL, 1, t + h)
    w.set(SDA, 1, t + 2 * h)
    d = decode_i2c(w.build(end_s=t + 30e-6), scl=SCL, sda=SDA)
    part = d.of("data")[-1]
    assert part.error == "incomplete" and part.fields["bits"] == 3 and d.of("stop")


@pytest.mark.parametrize("rate", [4 * F, 5 * F, 7.3 * F])
def test_low_oversampling_and_jitter(rate):
    rng = random.Random(int(rate))
    data = [rng.randrange(256) for _ in range(40)]
    w = wave(rate=rate, jitter=0.03 / F, seed=5)
    t = i2c(w, 20e-6, scl=SCL, sda=SDA, clock_hz=F, address=0x11, data=data)
    t = i2c(w, t + 20e-6, scl=SCL, sda=SDA, clock_hz=F, address=0x11, read=True, data=data[:5])
    d = decode_i2c(w.build(end_s=t + 20e-6), scl=SCL, sda=SDA)
    assert summary(d) == [("W", 0x11, data, None), ("R", 0x11, data[:5], None)]


def test_times_relative_to_trigger():
    w = wave()
    t = i2c(w, 10e-6, scl=SCL, sda=SDA, clock_hz=F, address=0x50)
    d = decode_i2c(w.build(end_s=t + 10e-6, trigger_s=10e-6), scl=SCL, sda=SDA)
    j = d.to_dict()["annotations"]
    assert j[0]["kind"] == "transfer" and j[0]["start_rel_s"] == pytest.approx(0)
    assert j[1]["kind"] == "start"
