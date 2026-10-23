import pytest

from fprobe.linux.ring import HEADER, RECORD, RingError, RingReader, encode_header, encode_record, parse_header

CAP = 8


def ring(records: dict[int, bytes], head: int) -> tuple[bytes, callable]:
    """capacity CAP の輪: records は seq → 記録のバイト（slot = seq % CAP）"""
    slots = [b"\0" * RECORD] * CAP
    for seq, raw in records.items():
        slots[seq % CAP] = raw
    data = encode_header(CAP, head) + b"".join(slots)

    def read(first: int, count: int) -> bytes:
        return data[HEADER + first * RECORD: HEADER + (first + count) * RECORD]
    return data, read


def rec(seq: int) -> bytes:
    return encode_record(seq, t_ns=1000 + seq, tid=7, bp=1, regs=[seq, 0, 0, 0, 0])


def test_header_round_trip():
    h = parse_header(encode_header(CAP, 5))
    assert h == {"version": 1, "record_size": RECORD, "capacity": CAP, "head": 5, "first": 0, "clock": "CLOCK_MONOTONIC_RAW"}
    assert parse_header(encode_header(CAP, 105, first=100))["first"] == 100


def test_first_seq_offsets_the_slots():
    """リングを開く前に出来事があった（first = 100）: seq 100 は slot 0"""
    slots = [rec(100 + i) for i in range(3)] + [b"\0" * RECORD] * (CAP - 3)
    data = encode_header(CAP, 103, first=100) + b"".join(slots)
    r = RingReader(CAP, first=100)
    out = r.take(103, lambda f, n: data[HEADER + f * RECORD: HEADER + (f + n) * RECORD])
    assert [x["seq"] for x in out["records"]] == [100, 101, 102] and out["lost"] == []


def test_bad_header_is_refused():
    with pytest.raises(RingError, match="magic"):
        parse_header(b"XXXX" + encode_header(CAP, 0)[4:])
    with pytest.raises(RingError, match="版"):
        parse_header(encode_header(CAP, 0)[:4] + (9).to_bytes(4, "little") + encode_header(CAP, 0)[8:])


def test_reads_new_records_in_order():
    _, read = ring({s: rec(s) for s in range(5)}, head=5)
    r = RingReader(CAP)
    out = r.take(5, read)
    assert [x["seq"] for x in out["records"]] == [0, 1, 2, 3, 4] and out["records"][2]["regs"][0] == 2
    assert out["lost"] == [] and out["torn"] == [] and out["corrupt"] == []
    assert r.take(5, read)["records"] == []                             # 新しいものが無い


def test_wraps_around():
    r = RingReader(CAP)
    _, read = ring({s: rec(s) for s in range(6)}, head=6)
    r.take(6, read)
    _, read = ring({s: rec(s) for s in range(6, 12)}, head=12)
    assert [x["seq"] for x in r.take(12, read)["records"]] == list(range(6, 12))


def test_torn_and_overrun_are_reported():
    r = RingReader(CAP)
    r.take(0, ring({}, 0)[1])
    torn = bytearray(rec(13))
    torn[0:8] = (0xFFFFFFFFFFFFFFFF).to_bytes(8, "little")                # 書きかけ
    corrupt = bytearray(rec(14))
    corrupt[30] ^= 0x40                                                 # 読みで壊れた（CRC が合わない）
    records = {s: rec(s) for s in range(12, 20)}
    records[13], records[14] = bytes(torn), bytes(corrupt)
    records[15] = rec(7)                                                # 前の周の記録が残っている
    _, read = ring(records, head=20)
    out = r.take(20, read)
    assert out["lost"] == [[0, 12]]                                     # 追い越された分
    assert out["torn"] == [13, 15] and out["corrupt"] == [14]
    assert [x["seq"] for x in out["records"]] == [12, 16, 17, 18, 19]


def test_limit_reads_oldest_first():
    _, read = ring({s: rec(s) for s in range(5)}, head=5)
    r = RingReader(CAP)
    assert [x["seq"] for x in r.take(5, read, limit=2)["records"]] == [0, 1]
    out = r.take(5, read, limit=2)
    assert [x["seq"] for x in out["records"]] == [2, 3] and out["left"] == 1
    assert [x["seq"] for x in r.take(5, read, limit=2)["records"]] == [4]


def test_header_crc_is_checked():
    h = bytearray(encode_header(CAP, 5))
    h[16] ^= 1
    with pytest.raises(RingError, match="CRC"):
        parse_header(bytes(h))
