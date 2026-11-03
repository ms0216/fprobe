"""バイトを詰めたイベント（種類 13・14・15。SWO・UART の 1 件に最大 4 バイトと各バイトの時刻）の解読。
設計: docs/superpowers/specs/2026-11-01-bandwidth-observation-design.md 3.5。
解読はバイトごとの事実（古い種類 3・4・5 と同じ形の 1 バイト 1 件）に戻す。番号（書いた数）は記録の件数で数える。"""

from types import SimpleNamespace

import pytest

from fprobe.events import (EVENT_BYTES, KIND_SWO, KIND_SWO_PACKED, KIND_UART_RX, KIND_UART_RX_PACKED, KIND_UART_TX,
                           KIND_UART_TX_PACKED, PACK_DELTA_BITS, EventLog, decode_events, packed_record, resolve)
from fprobe.mcp_server import Probe
from test_events_overwrite import RingDevice
from caps_fakes import caps_for

MASK48 = (1 << 48) - 1


def packed(kind: int, t0: int, data: bytes, deltas: list[int]) -> bytes:
    """回路の形（event_collector.vhd の種類 13〜15）をここで別に組む（解読の試験の基準）。"""
    assert 1 <= len(data) <= 4 and len(deltas) == len(data) - 1
    v = t0 | kind << 120 | (len(data) - 1) << 116
    for i, b in enumerate(data):
        v |= b << (48 + 8 * i)
    for i, d in enumerate(deltas):
        v |= d << (80 + 12 * i)
    return v.to_bytes(EVENT_BYTES, "little")


def test_the_delta_is_12_bits():
    assert PACK_DELTA_BITS == 12


def test_four_swo_bytes_with_their_own_times():
    events = decode_events(packed(KIND_SWO_PACKED, 1000, b"ABCD", [40, 4095, 0]))
    assert [(e.kind, e.time, e.data) for e in events] == [
        (KIND_SWO, 1000, 0x41), (KIND_SWO, 1040, 0x42), (KIND_SWO, 5135, 0x43), (KIND_SWO, 5135, 0x44)]
    # 古い種類 5 と同じ形（番地・要求・ACK は 0）
    assert all((e.address, e.ap, e.read, e.register, e.ack, e.parity_error) == (0, False, False, 0, 0, False)
               for e in events)


@pytest.mark.parametrize("n", [1, 2, 3])
def test_fewer_bytes_ignore_the_unused_fields(n):
    v = int.from_bytes(packed(KIND_SWO_PACKED, 7, b"wxyz"[:n], [1, 2, 3][:n - 1]), "little")
    v |= 0xFF << (48 + 8 * 3) if n < 4 else 0                 # 使わない欄に何があっても読まない
    events = decode_events(v.to_bytes(EVENT_BYTES, "little"))
    assert [e.data for e in events] == list(b"wxyz"[:n])
    assert [e.time for e in events] == [7, 8, 10][:n]


def test_uart_bytes_decode_to_the_uart_kinds():
    data = packed(KIND_UART_RX_PACKED, 10, b"ok", [100]) + packed(KIND_UART_TX_PACKED, 20, b"!", [])
    assert [(e.kind, e.time, e.data) for e in decode_events(data)] == [
        (KIND_UART_RX, 10, ord("o")), (KIND_UART_RX, 110, ord("k")), (KIND_UART_TX, 20, ord("!"))]


def test_times_wrap_at_48_bits_like_every_event():
    events = decode_events(packed(KIND_SWO_PACKED, MASK48 - 10, b"ab", [20]))
    assert [e.time for e in events] == [MASK48 - 10, 9]


def test_packed_and_one_byte_records_mix_and_resolve():
    old = (KIND_SWO << 120 | 0x5A << 80 | 3).to_bytes(EVENT_BYTES, "little")
    data = old + packed(KIND_SWO_PACKED, 5, b"\x01\x02", [9])
    assert [(a.time, a.kind, a.value) for a in resolve(decode_events(data))] == [
        (3, "swo", 0x5A), (5, "swo", 1), (14, "swo", 2)]


def test_packed_record_builds_the_same_bits():
    # 試験の偽物が使う組み立て（packed_record）は回路の形と同じ
    assert packed_record(KIND_SWO_PACKED, 1000, b"ABCD", [40, 4095, 0]) == packed(KIND_SWO_PACKED, 1000, b"ABCD",
                                                                                 [40, 4095, 0])
    with pytest.raises(ValueError):
        packed_record(KIND_SWO_PACKED, 0, b"ab", [1 << PACK_DELTA_BITS])          # 差が 12 ビットに収まらない
    with pytest.raises(ValueError):
        packed_record(KIND_SWO_PACKED, 0, b"abcde", [1, 1, 1, 1])


class PackedRing(RingDevice):
    """記録の番号 i に、3 バイトを詰めた SWO の記録（先頭の時刻 = 10 i）を入れたリング。"""

    def read_stream(self, size, timeout_ms=2000):
        self.written += self.advance
        out = b""
        for k in range(self.count):
            pos = (self.first + k) % self.capacity
            newest = max(i for i in range(self.written) if i % self.capacity == pos)
            out += packed(KIND_SWO_PACKED, 10 * newest, bytes([newest & 0xFF] * 3), [1, 1])
        return out


def test_the_log_counts_records_not_bytes():
    dev = PackedRing(capacity=8, written=5, advance=0)
    log = EventLog(dev)
    events = log.read()
    assert len(events) == 15 and log.records == 5
    assert [e.time for e in events[:4]] == [0, 1, 2, 10]


def test_records_overwritten_while_reading_are_dropped_whole():
    dev = PackedRing(capacity=8, written=8, advance=5)
    log = EventLog(dev)
    events = log.read()                                   # 記録 0〜4 は 8〜12 に上書きされた
    assert log.overwritten == 5 and log.records == 3
    assert sorted({e.time // 10 for e in events}) == [5, 6, 7]


def test_events_read_numbers_count_records(monkeypatch):
    dev = PackedRing(capacity=16, written=6, advance=0)
    dev.caps = caps_for(timebase_hz=100_000_000)
    p = Probe("unused")
    p._session = SimpleNamespace(device=dev)
    p._la_idle = lambda: None
    r = p.events_read(since=0, last=4)
    assert (r["first"], r["next"]) == (0, 4) and len(r["items"]) == 12
    r = p.events_read(since=r["next"])
    assert (r["first"], r["next"]) == (4, 6) and len(r["items"]) == 6
    r = p.events_read(last=2)
    assert (r["first"], r["next"]) == (4, 6)


def test_capability_bit_is_parsed():
    from fprobe import generated as g
    from fprobe.caps import parse
    import struct

    def caps_with(features):
        tlv = struct.pack("<HHI", g.CAPS_FEATURES, 4, features) + struct.pack("<HH", g.CAPS_END, 0)
        return b"FCAP" + struct.pack("<I", 8 + len(tlv)) + tlv
    assert parse(caps_with(g.FEATURE_EVENT_BYTE_PACK)).event_byte_pack is True
    assert parse(caps_with(0)).event_byte_pack is False
