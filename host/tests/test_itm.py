"""fprobe.itm: SWO のバイト列（ITM / DWT のパケット）の解読。バイト列は ARMv7-M ARM の Appendix D4 に従い手で組み立てる。"""

import pytest

from fprobe.itm import (DataTraceAddress, DataTracePc, DataTraceValue, DecodeError, EventCounter, ExceptionAction,
                        ExceptionTrace, Extension, GlobalTimestamp1, GlobalTimestamp2, Instrumentation, ItmDecoder,
                        LocalTimestamp, Overflow, PcSample, PortText, Sync, TextLine, TimestampRelation, Timestamper)

SYNC = bytes([0, 0, 0, 0, 0, 0x80])


def stim(port: int, value: int, size: int) -> bytes:
    """ソフトウェアのソースのパケット（ITM のスティミュラス・ポート）。"""
    return bytes([(port << 3) | {1: 1, 2: 2, 4: 3}[size]]) + value.to_bytes(size, "little")


def hw(ident: int, payload: bytes) -> bytes:
    """ハードウェアのソースのパケット（DWT）。"""
    return bytes([(ident << 3) | 0x04 | {1: 1, 2: 2, 4: 3}[len(payload)]]) + payload


def decode(data: bytes, **kw) -> list:
    return ItmDecoder(**kw).feed(data)


# --- パケットの種類 -----------------------------------------------------------------------

def test_sync_and_overflow():
    assert decode(SYNC + b"\x70") == [Sync(), Overflow()]
    assert decode(bytes(9) + b"\x80") == [Sync()]                         # 0 が 47 ビットより多くてもよい


def test_instrumentation_packets_of_each_size():
    data = stim(0, 0x41, 1) + stim(5, 0x1234, 2) + stim(31, 0xDEADBEEF, 4)
    assert decode(data) == [Instrumentation(0, 0x41, 1), Instrumentation(5, 0x1234, 2),
                            Instrumentation(31, 0xDEADBEEF, 4)]
    assert Instrumentation(31, 0xDEADBEEF, 4).data == bytes.fromhex("EFBEADDE")


def test_event_counter_and_exception_trace():
    packets = decode(hw(0, b"\x21") + hw(1, bytes([0x0F, 0x10])) + hw(1, bytes([0x0F, 0x20]))
                     + hw(1, bytes([0x00, 0x31])))
    assert packets == [EventCounter(0x21), ExceptionTrace(15, ExceptionAction.ENTER),
                       ExceptionTrace(15, ExceptionAction.EXIT), ExceptionTrace(256, ExceptionAction.RETURN)]
    assert packets[0].cyc and packets[0].cpi and not packets[0].sleep


def test_pc_sampling():
    assert decode(hw(2, (0x08000123).to_bytes(4, "little")) + hw(2, b"\x00")) == [PcSample(0x08000123), PcSample(None)]


def test_data_trace():
    data = (hw(8 + 2 * 1, (0x20000010).to_bytes(4, "little"))          # 比較器 1 の PC
            + hw(9 + 2 * 3, (0x0124).to_bytes(2, "little"))             # 比較器 3 の番地の下位
            + hw(16 + 2 * 2 + 1, (0xCAFE).to_bytes(2, "little"))        # 比較器 2 の書き込みの値
            + hw(16 + 2 * 0 + 0, b"\x7F"))                               # 比較器 0 の読みの値
    assert decode(data) == [DataTracePc(1, 0x20000010), DataTraceAddress(3, 0x0124),
                            DataTraceValue(2, True, 0xCAFE, 2), DataTraceValue(0, False, 0x7F, 1)]


def test_local_timestamps():
    # 形式 2（1 バイト、差分 3）、形式 1（TC=1、差分 = 0x05 | 0x02 << 7）、形式 1（1 バイト、TC=0）
    data = bytes([0x30]) + bytes([0xD0, 0x85, 0x02]) + bytes([0xC0, 0x10])
    assert decode(data) == [LocalTimestamp(3, TimestampRelation.SYNC),
                            LocalTimestamp(0x05 | 0x02 << 7, TimestampRelation.TIMESTAMP_DELAYED),
                            LocalTimestamp(0x10, TimestampRelation.SYNC)]


def test_global_timestamps():
    gts1 = bytes([0x94, 0x81, 0x82, 0x83, 0x60 | 0x05])                 # 4 バイト目: Wrap=1 ClkCh=1 TS[25:21]=5
    gts1_short = bytes([0x94, 0x81, 0x02])                                # 2 バイト（下位 14 ビット）
    gts2 = bytes([0xB4, 0x81, 0x80, 0x80, 0x01])                          # 上位 [47:26]
    value = 1 | 2 << 7 | 3 << 14 | 5 << 21
    assert decode(gts1 + gts1_short + gts2) == [GlobalTimestamp1(value, 26, wrap=True, clock_change=True),
                                                GlobalTimestamp1(1 | 2 << 7, 14),
                                                GlobalTimestamp2(1 | 1 << 21)]


def test_extension_packet():
    assert decode(bytes([0x08 | 0x30]) + bytes([0x88 | 0x10, 0x01])) == [Extension(3, 0), Extension(1 | 1 << 3, 0)]
    assert decode(bytes([0x0C])) == [Extension(0, 1)]


# --- 増分・同期 ---------------------------------------------------------------------------

STREAM = (SYNC + stim(0, ord("h"), 1) + bytes([0xC0, 0x85, 0x01]) + hw(1, bytes([0x0F, 0x10])) + b"\x70"
          + stim(1, 0x01020304, 4) + bytes([0x94, 0x81, 0x82, 0x83, 0x05]) + hw(2, b"\x00"))


def test_split_anywhere_gives_same_packets():
    whole = decode(STREAM)
    assert len(whole) == 8 and not any(isinstance(p, DecodeError) for p in whole)
    one_by_one = ItmDecoder()
    assert [p for b in STREAM for p in one_by_one.feed(bytes([b]))] == whole
    for cut in range(len(STREAM)):
        dec = ItmDecoder()
        assert dec.feed(STREAM[:cut]) + dec.feed(STREAM[cut:]) == whole


def test_offsets_count_from_the_first_byte():
    dec = ItmDecoder()
    first = dec.feed(SYNC + stim(0, 1, 1))
    second = dec.feed(stim(0, 2, 2))
    assert [p.offset for p in first + second] == [0, 6, 8]


def test_stream_starting_midway_is_skipped_until_sync():
    garbage = bytes([0x03, 0x11, 0x22, 0x00, 0x70, 0xFF])                 # どこかのパケットの途中
    dec = ItmDecoder(synced=False, resync=False)                          # 同期パケットを待つ作り
    assert dec.feed(garbage) == [] and not dec.synced
    packets = dec.feed(SYNC + stim(0, 0x41, 1))
    assert packets == [Sync(), Instrumentation(0, 0x41, 1)] and dec.synced
    assert packets[0].skipped == len(garbage) and packets[0].offset == len(garbage)


def test_sync_split_across_feeds_while_hunting():
    dec = ItmDecoder(synced=False, resync=False)
    assert dec.feed(b"\x55\x00\x00\x00") == []
    assert dec.feed(b"\x00\x00\x80" + stim(2, 7, 1)) == [Sync(), Instrumentation(2, 7, 1)]


def test_reserved_header_loses_sync_and_recovers():
    data = stim(0, 1, 1) + b"\x80" + b"\x01\x02" + SYNC + stim(0, 2, 1)   # 0x80 は予約
    packets = decode(data, resync=False)
    assert packets[0] == Instrumentation(0, 1, 1)
    assert isinstance(packets[1], DecodeError) and packets[1].offset == 2
    assert packets[2:] == [Sync(), Instrumentation(0, 2, 1)] and packets[2].skipped == 2


def test_invalid_hardware_packets_are_errors():
    assert isinstance(decode(hw(0, b"\x01\x02"))[0], DecodeError)          # イベント・カウンタは 1 バイト
    assert isinstance(decode(hw(5, b"\x01"))[0], DecodeError)              # 識別子 5 は未定義
    assert isinstance(decode(hw(1, b"\x0F\x00"))[0], DecodeError)          # 例外トレースの機能 0 は予約


def test_short_zero_run_is_an_error():
    packets = decode(b"\x00\x00\x41")
    assert isinstance(packets[0], DecodeError)


def test_too_long_continuation_is_an_error():
    packets = decode(bytes([0xC0, 0x81, 0x81, 0x81, 0x81, 0x01]))          # ローカルのタイムスタンプは最大 4 バイト
    assert isinstance(packets[0], DecodeError)


def test_incomplete_packet_waits():
    dec = ItmDecoder()
    assert dec.feed(b"\x03\x01\x02") == []
    assert dec.feed(b"\x03\x04") == [Instrumentation(0, 0x04030201, 4)]


# --- 時刻の付与 ---------------------------------------------------------------------------

def test_timestamper_assigns_cumulative_local_time_to_preceding_packets():
    packets = decode(stim(0, 1, 1) + stim(0, 2, 1) + bytes([0x30]) + stim(0, 3, 1) + bytes([0xC0, 0x0A])
                     + stim(0, 4, 1))
    ts = Timestamper()
    timed = ts.feed(packets)
    assert [(t.packet.value, t.local_time) for t in timed] == [(1, 3), (2, 3), (3, 13)]
    rest = ts.flush()
    assert [(t.packet.value, t.local_time) for t in rest] == [(4, None)]


def test_timestamper_global_time_and_overflow():
    gts1 = bytes([0x94, 0x81, 0x80, 0x80, 0x00])
    gts2 = bytes([0xB4, 0x02])
    packets = decode(gts1 + gts2 + b"\x70" + stim(0, 9, 1) + bytes([0x10]))
    timed = Timestamper().feed(packets)
    assert [type(t.packet) for t in timed] == [Overflow, Instrumentation]
    assert all(t.after_overflow for t in timed)
    assert timed[1].global_time == (2 << 26) | 1 and timed[1].local_time == 1


def test_timestamper_clock_change_invalidates_global_time():
    packets = decode(bytes([0xB4, 0x02]) + bytes([0x94, 0x81, 0x80, 0x80, 0x20]) + stim(0, 1, 1) + bytes([0x10]))
    timed = Timestamper().feed(packets)
    assert timed[0].global_time is None


# --- ポートごとの文字列 -------------------------------------------------------------------

def test_port_text_assembles_lines_per_port():
    data = b"".join(stim(0, c, 1) for c in b"hel") + stim(1, ord("x"), 1) + stim(0, int.from_bytes(b"lo\r\n", "little"), 4)
    text = PortText()
    assert text.feed(decode(data)) == [TextLine(0, "hello")]
    assert text.pending(1) == "x" and text.pending(0) == ""


def test_port_text_handles_utf8_split_across_packets_and_port_filter():
    utf8 = "あ\n".encode()
    packets = decode(b"".join(stim(3, b, 1) for b in utf8) + stim(4, ord("z"), 1) + stim(4, 0x0A, 1))
    assert PortText(ports={3}).feed(packets) == [TextLine(3, "あ")]


def test_port_text_replaces_invalid_utf8():
    assert PortText().feed(decode(stim(0, 0xFF, 1) + stim(0, 0x0A, 1))) == [TextLine(0, "�")]


@pytest.mark.parametrize("size", [1, 2, 4])
def test_instrumentation_data_length(size):
    assert len(Instrumentation(0, 0, size).data) == size


# --- 同期を失った後の復帰（同期パケットを待たない） -----------------------------------------------

# 実機（2026-10-18）で見た流れ: 例外トレース（入る・出る・戻る）とローカルのタイムスタンプが続く
_ROUND = bytes.fromhex("0e0f10 c0e27c 0e0f20 c011 0e0030 c00a")


def test_dropped_bytes_are_recovered_without_waiting_for_a_sync_packet():
    """2 バイトが落ちて解読がずれても、続くパケットが正しく解ける位置から再開する（Resync で知らせる）。"""
    from fprobe.itm import ExceptionTrace, Resync
    stream = _ROUND * 4
    broken = stream[:len(_ROUND) + 12] + stream[len(_ROUND) + 14:]         # 2 巡目の 0e の後の 00 30 が落ちた（実機と同じ）
    out = ItmDecoder().feed(broken)
    assert any(isinstance(p, Resync) for p in out)
    exceptions = [p for p in out if isinstance(p, ExceptionTrace)]
    assert len(exceptions) >= 10                                          # 落ちた分を除いて解け続ける
    strict = ItmDecoder(resync=False).feed(broken)                        # 同期パケットを待つ作り: その後は解けない
    assert sum(isinstance(p, ExceptionTrace) for p in strict) < len(exceptions)


def test_resync_waits_for_enough_bytes_and_is_incremental():
    """1 バイトずつ与えても、まとめて与えたのと同じ結果になる。"""
    from fprobe.itm import Resync
    stream = _ROUND * 4
    broken = stream[:len(_ROUND) + 12] + stream[len(_ROUND) + 14:]
    whole = ItmDecoder().feed(broken)
    d = ItmDecoder()
    pieces = [p for b in broken for p in d.feed(bytes([b]))]
    assert [type(p).__name__ for p in pieces] == [type(p).__name__ for p in whole]
    assert any(isinstance(p, Resync) for p in pieces)
