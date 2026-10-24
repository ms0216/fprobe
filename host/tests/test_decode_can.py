"""fprobe.decode.can: CAN・CAN FD の発生器 → 解読の往復、CRC、スタッフ、誤り（ハードウェア不要）。"""

import random

import pytest

from fprobe.decode import decode
from fprobe.decode.can import CRC15, CRC17, CRC21, can_crc, decode_can
from fprobe.decode.synth import Wave, can

CH = 2
NB = 500_000


def bits_of(data: bytes):
    return [b >> i & 1 for b in data for i in reversed(range(8))]


def test_crc_matches_the_catalogue_check_values():
    check = bits_of(b"123456789")
    assert can_crc(check, CRC15) == 0x059E                       # CRC-15/CAN
    assert can_crc(check, (17, CRC17[1], 0)) == 0x04F03          # CRC-17/CAN-FD（初期値 0 の表の値）
    assert can_crc(check, (21, CRC21[1], 0)) == 0x0ED841         # CRC-21/CAN-FD


def build(frames, rate=NB * 16, invert=False, jitter=0.0, data_bitrate=None, tail=20):
    w = Wave(channels=4, sample_rate_hz=rate, idle={CH: 0 if invert else 1}, jitter=jitter, seed=3)
    t = 12 / NB
    for kw in frames:
        t = can(w, CH, t, bitrate=NB, invert=invert, data_bitrate=data_bitrate, **kw)
    return w.build(end_s=t + tail / NB)


def frames_of(d):
    return [a.fields for a in d.of("frame")]


@pytest.mark.parametrize("extended", [False, True])
@pytest.mark.parametrize("seed", range(4))
def test_classic_frames_round_trip(extended, seed):
    rng = random.Random(seed)
    sent = []
    for _ in range(3):
        n = rng.randrange(9)
        sent.append({"can_id": rng.randrange(1 << (29 if extended else 11)), "extended": extended,
                     "data": bytes(rng.randrange(256) for _ in range(n))})
    d = decode_can(build(sent), CH, bitrate=NB, sample_point=0.75, invert=False)
    assert d.errors() == []
    got = frames_of(d)
    assert [(f["id"], f["extended"], bytes(f["data"]), f["fd"], f["crc_ok"], f["ack"]) for f in got] == \
        [(s["can_id"], extended, s["data"], False, True, True) for s in sent]
    assert [a.value for a in d.of("data")] == [b for s in sent for b in s["data"]]


def test_stuffing_heavy_frame_and_inverted_line():
    sent = [{"can_id": 0x000, "data": b"\x00" * 8}, {"can_id": 0x7FF, "data": b"\xff" * 8}]
    for invert in (False, True):
        d = decode_can(build(sent, invert=invert), CH, bitrate=NB, sample_point=0.75, invert=invert)
        assert d.errors() == [] and [bytes(f["data"]) for f in frames_of(d)] == [s["data"] for s in sent]


def test_remote_frame_and_dlc_above_eight():
    d = decode_can(build([{"can_id": 0x123, "rtr": True, "dlc": 4}, {"can_id": 0x55, "data": b"\x01" * 8, "dlc": 12}]),
                   CH, bitrate=NB, sample_point=0.75, invert=False)
    rtr, big = frames_of(d)
    assert rtr["rtr"] and rtr["dlc"] == 4 and rtr["data"] == []
    assert big["dlc"] == 12 and big["data"] == [1] * 8 and d.errors() == []


@pytest.mark.parametrize("length", [0, 3, 8, 12, 16, 20, 32, 64])
@pytest.mark.parametrize("brs", [False, True])
def test_fd_frames_round_trip(length, brs):
    rng = random.Random(length)
    data = bytes(rng.randrange(256) for _ in range(length))
    sent = [{"can_id": 0x1ABCDE5, "extended": True, "fd": True, "brs": brs, "data": data},
            {"can_id": 0x0F0, "fd": True, "brs": brs, "esi": True, "data": data[:8]}]
    d = decode_can(build(sent, rate=2_000_000 * 20, data_bitrate=2_000_000), CH, bitrate=NB,
                   data_bitrate=2_000_000, sample_point=0.8, data_sample_point=0.7, invert=False)
    assert d.errors() == [], [(a.kind, a.text) for a in d.errors()]
    first, second = frames_of(d)
    assert (first["id"], first["fd"], first["brs"], bytes(first["data"])) == (0x1ABCDE5, True, brs, data)
    assert (second["esi"], bytes(second["data"])) == (True, data[:8])


def test_bad_crc_missing_ack_and_resync_after_an_error_frame():
    sent = [{"can_id": 0x10, "data": b"\x11\x22", "bad_crc": True},
            {"can_id": 0x20, "data": b"\x33", "ack": False},
            {"can_id": 0x30, "data": b"\x44\x55\x66", "error_at": 30},
            {"can_id": 0x40, "data": b"\x77"}]
    d = decode_can(build(sent), CH, bitrate=NB, sample_point=0.75, invert=False)
    frames = d.of("frame")
    assert [a.error for a in frames] == ["crc", "ack", "stuff", None]
    assert frames[3].fields["data"] == [0x77]
    assert d.of("nack")[0].error == "ack"
    assert [a.error for a in d.of("error")] == ["stuff"]


def test_tolerates_clock_error_and_jitter():
    sent = [{"can_id": 0x2AA, "data": bytes(range(8))}] * 3
    w = Wave(channels=4, sample_rate_hz=NB * 10, idle={CH: 1}, jitter=0.05 / NB, seed=1)
    t = 12 / NB
    for kw in sent:
        t = can(w, CH, t, bitrate=NB * 1.015, **kw)                # 送り手が 1.5% 速い
    d = decode_can(w.build(end_s=t + 20 / NB), CH, bitrate=NB, sample_point=0.75, invert=False)
    assert d.errors() == [] and [f["data"] for f in frames_of(d)] == [list(range(8))] * 3


def test_fd_with_brs_needs_the_data_bitrate_and_truncation_is_reported():
    tr = build([{"can_id": 1, "fd": True, "brs": True, "data": b"\x01\x02"}], rate=2_000_000 * 10,
               data_bitrate=2_000_000)
    d = decode_can(tr, CH, bitrate=NB, sample_point=0.75, invert=False)
    assert [a.error for a in d.of("frame")] == ["data_bitrate"]
    w = Wave(channels=4, sample_rate_hz=NB * 16, idle={CH: 1})
    t = can(w, CH, 12 / NB, bitrate=NB, can_id=0x123, data=b"\xaa\xbb")
    d = decode_can(w.build(end_s=t - 20 / NB), CH, bitrate=NB, sample_point=0.75, invert=False)
    (frame,) = d.of("frame")
    assert frame.error == "truncated" and frame.fields["id"] == 0x123


def test_decode_by_name_and_the_annotation_layout():
    tr = build([{"can_id": 0x123, "data": b"\x01\x02"}])
    d = decode("can", tr, channel=CH, bitrate=NB, sample_point=0.75, invert=False)
    kinds = [a.kind for a in d]
    assert kinds == ["frame", "sof", "id", "control", "data", "data", "crc", "ack", "eof"]
    frame = d.of("frame")[0]
    assert frame.text == "ID 0x123 [2] 01 02" and frame.start < d.of("eof")[0].end <= frame.end
    with pytest.raises(ValueError):
        decode_can(tr, CH, bitrate=0, sample_point=0.75, invert=False)


def test_the_sample_point_is_given_by_the_user():
    # 総点検 S1・?1: サンプル点は規格で決まらない（バスごとの設定）。既定 0.75 を持たず、範囲は定義（0 と 1 の間）だけ
    tr = build([{"can_id": 0x123, "data": b"\x01", "dlc": 1}])
    with pytest.raises(TypeError):
        decode_can(tr, CH, bitrate=NB, invert=False)
    assert decode_can(tr, CH, bitrate=NB, sample_point=0.97,
                      invert=False).params["sample_point"] == 0.97     # 以前は 0.95 まで
    with pytest.raises(ValueError):
        decode_can(tr, CH, bitrate=NB, sample_point=1.0, invert=False)


def _two_frames_with_gap(gap_bits):
    """1 つ目のフレームの ACK 区切りから 2 つ目の SOF までの劣勢が gap_bits ビット。"""
    w = Wave(channels=4, sample_rate_hz=NB * 16, idle={CH: 1}, seed=3)
    t = can(w, CH, 12 / NB, bitrate=NB, can_id=0x123, data=b"\x01")
    t = can(w, CH, t - (11 - gap_bits) / NB, bitrate=NB, can_id=0x456, data=b"\x02")
    return w.build(end_s=t + 20 / NB)


@pytest.mark.parametrize("sample_point, found", [(0.75, True), (0.875, False)])
def test_sof_needs_eleven_recessive_bits_read_at_the_sample_point(sample_point, found):
    """休止は ISO 11898-1 の劣勢 11 ビット。受信器と同じくサンプル点で 11 回劣勢を読んだか（経験の余裕 0.5 ビットでない）。
    劣勢 10.8 ビット: サンプル点 0.75 なら 11 個目のサンプル点（10.75 ビット）も劣勢、0.875 なら 11 個目は優勢。"""
    d = decode_can(_two_frames_with_gap(10.8), CH, bitrate=NB, sample_point=sample_point, invert=False)
    ids = [f["id"] for f in frames_of(d)]
    assert (0x456 in ids) is found
