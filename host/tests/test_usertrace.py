"""ターゲットが出す値とイベントの解読（fprobe.usertrace）: ITM のポート・RTT のチャンネルを型で解釈し、fprobe_user.h の
名前付きイベント（名前は ELF の文字列）を読み、区間を対にして集計する。機器は使わない。"""

import re
import struct
from pathlib import Path

import pytest

from fprobe.itm import Instrumentation, Overflow
from fprobe.usertrace import (EVENT_PORT, KINDS, VALUE_PORT, Channel, UserDecoder, ValueType, elf_strings,
                              interval_stats, pair_intervals, record_rows, value_stats)

HEADER = Path(__file__).resolve().parents[1] / "firmware" / "freertos_demo" / "fprobe_user.h"
NAMES = {0x2000_0100: "adc", 0x2000_0110: "f:volt", 0x2000_0120: "button", 0x2000_0130: "i:count",
         0x2000_0140: "q:gain"}


def itm(t: int, port: int, value: int, size: int = 4):
    return (t, Instrumentation(port, value, size))


def event(t: int, address: int, kind: str) -> tuple:
    return itm(t, EVENT_PORT, address | KINDS.index(kind))


def test_constants_match_the_target_header():
    text = HEADER.read_text(encoding="utf-8")
    defines = {m[1]: int(m[2]) for m in re.finditer(r"#define FPROBE_USER_(\w+)\s+(\d+)u\b", text)}
    assert defines["EVENT_PORT"] == EVENT_PORT and defines["VALUE_PORT"] == VALUE_PORT
    assert [defines[k.upper()] for k in KINDS] == [0, 1, 2, 3]


# --- 型 -------------------------------------------------------------------------------------------

@pytest.mark.parametrize("spec, data, value", [
    ("i8", b"\xff", -1), ("u8", b"\xff", 255), ("i16", b"\x00\x80", -32768), ("u16", b"\x34\x12", 0x1234),
    ("i32", (-5).to_bytes(4, "little", signed=True), -5), ("u32", b"\xff\xff\xff\xff", 0xFFFF_FFFF),
    ("f32", struct.pack("<f", 1.5), 1.5), ("q16.16", (3 << 16 | 0x8000).to_bytes(4, "little"), 3.5),
    ("q1.15", (-16384 & 0xFFFF).to_bytes(2, "little"), -0.5), ("q8.8", b"\x80\x01", 1.5)])
def test_scalar_types(spec, data, value):
    t = ValueType.parse(spec)
    assert t.size == len(data) and t.decode(data) == value


def test_struct_type_and_errors():
    t = ValueType.parse({"type": "struct", "fields": [["x", "i16"], ["y", "i16"], ["v", "f32"]]})
    assert t.size == 8
    assert t.decode(struct.pack("<hhf", -1, 2, 0.25)) == {"x": -1, "y": 2, "v": 0.25}
    for bad in ("i64", "q16.15", {"type": "struct", "fields": []}, {"type": "struct", "fields": [["a", "nope"]]}):
        with pytest.raises(ValueError):
            ValueType.parse(bad)
    with pytest.raises(ValueError):
        Channel.from_dict({"name": "x", "source": "swd", "index": 1, "type": "i32"})


# --- ITM ------------------------------------------------------------------------------------------

def test_itm_ports_are_values_and_named_events_are_resolved_from_the_elf():
    dec = UserDecoder([Channel("temp", "itm", 1, ValueType.parse("f32")),
                       Channel("pos", "itm", 2, ValueType.parse({"type": "struct",
                                                                   "fields": [["x", "i16"], ["y", "i16"]]}))],
                      NAMES.get)
    records = dec.feed_itm([
        itm(10, 1, struct.unpack("<I", struct.pack("<f", 21.5))[0]),
        itm(20, 2, 0x0002, 2), itm(21, 2, 0xFFFF, 2),                    # struct は 2 回の書き込みで 4 バイト
        event(30, 0x2000_0100, "start"), event(40, 0x2000_0100, "stop"),
        event(50, 0x2000_0110, "value"), itm(51, VALUE_PORT, struct.unpack("<I", struct.pack("<f", 3.25))[0]),
        event(60, 0x2000_0130, "value"), itm(61, VALUE_PORT, (-7) & 0xFFFF_FFFF),
        event(70, 0x2000_0140, "value"), itm(71, VALUE_PORT, 0x0001_8000),
        event(80, 0x2000_0120, "mark"), itm(90, 0, 0x41, 1)])              # ポート 0（printf）は無視
    assert [(r.t, r.kind, r.name, r.value) for r in records] == [
        (10, "value", "temp", 21.5), (20, "value", "pos", {"x": 2, "y": -1}), (30, "start", "adc", None),
        (40, "stop", "adc", None), (50, "value", "volt", 3.25), (60, "value", "count", -7),
        (70, "value", "gain", 1.5), (80, "mark", "button", None)]
    assert all(r.source == "itm" for r in records) and dec.errors == {}


def test_itm_errors_size_mismatch_orphan_values_unknown_names_and_overflow():
    dec = UserDecoder([Channel("temp", "itm", 1, ValueType.parse("i32")),
                       Channel("pos", "itm", 2, ValueType.parse({"type": "struct", "fields": [["x", "i32"],
                                                                                                ["y", "i32"]]}))],
                      NAMES.get)
    records = dec.feed_itm([
        itm(1, 1, 5, 2),                                                  # 大きさが型と違う
        itm(2, VALUE_PORT, 1),                                            # 見出しの無い値
        event(3, 0x2000_0130, "value"), event(4, 0x2000_0100, "mark"),   # 値の来ない value
        event(5, 0x2000_0F00, "mark"),                                    # ELF に無い名前
        itm(6, 2, 1), (7, Overflow()), itm(8, 2, 2), itm(9, 2, 3)])       # あふれで半端な struct を捨てる
    assert [(r.kind, r.name, r.value) for r in records] == [("mark", "adc", None), ("mark", "0x20000F00", None),
                                                            ("value", "pos", {"x": 2, "y": 3})]
    assert dec.errors == {"size_mismatch": 1, "orphan_value": 1, "missing_value": 1, "unknown_name": 1, "lost": 1}


# --- RTT ------------------------------------------------------------------------------------------

def test_rtt_channels_are_fixed_size_records_and_events_use_the_same_words():
    dec = UserDecoder([Channel("adc", "rtt", 1, ValueType.parse("u16"))], NAMES.get, event_rtt_channel=2)
    out = dec.feed_rtt(1, 100, b"\x01\x00\x02", uncertainty=5)            # 半端は次へ
    out += dec.feed_rtt(1, 200, b"\x00")
    words = struct.pack("<II", 0x2000_0110 | 3, struct.unpack("<I", struct.pack("<f", 2.0))[0])
    out += dec.feed_rtt(2, 300, struct.pack("<I", 0x2000_0100 | 1) + words[:5])
    out += dec.feed_rtt(2, 400, words[5:])
    out += dec.feed_rtt(9, 500, b"ignored")
    assert [(r.t, r.kind, r.name, r.value, r.source, r.uncertainty) for r in out] == [
        (100, "value", "adc", 1, "rtt", 5), (200, "value", "adc", 2, "rtt", None), (300, "start", "adc", None, "rtt", None),
        (400, "value", "volt", 2.0, "rtt", None)]


# --- 区間・統計・記録 ------------------------------------------------------------------------------

def test_intervals_pair_last_in_first_out_per_name():
    dec = UserDecoder([], NAMES.get)
    records = dec.feed_itm([event(10, 0x2000_0100, "start"), event(20, 0x2000_0100, "start"),     # 入れ子
                            event(25, 0x2000_0120, "stop"),                                        # 窓の前に始まった
                            event(30, 0x2000_0100, "stop"), event(50, 0x2000_0100, "stop"),
                            event(55, 0x2000_0100, "stop"),                                        # 対の無い stop
                            event(60, 0x2000_0100, "start")])                                      # 開いたまま
    found = pair_intervals(records)
    assert [(i.name, i.t0, i.t1, i.depth) for i in found.intervals] == [("adc", 20, 30, 1), ("adc", 10, 50, 0)]
    # その名前の最初の start より前の stop は、取り始める前に始まった区間（窓の始めで切れた。open の逆）。2026-10-18 に
    # 実機で、取り始めに走っていた fpu_step の stop を「対の無い stop」と数えていた
    assert found.stops_before_first_start == {"button": 1} and found.unmatched_stops == {"adc": 1} \
        and found.open == {"adc": 1}
    stats = interval_stats(found.intervals, timebase_hz=1000)
    assert stats["adc"]["count"] == 2 and stats["adc"]["max"] == 40 and stats["adc"]["max_at"] == 10
    assert stats["adc"]["seconds"]["max"] == pytest.approx(0.04)


def test_value_stats_and_recording_rows():
    dec = UserDecoder([Channel("temp", "itm", 1, ValueType.parse("i32")),
                       Channel("pos", "itm", 2, ValueType.parse({"type": "struct", "fields": [["x", "i16"],
                                                                                                ["y", "i16"]]}))],
                      NAMES.get)
    records = dec.feed_itm([itm(1, 1, 5), itm(2, 1, 9), itm(3, 1, 1), itm(4, 2, 0x0003_0001),
                            event(5, 0x2000_0100, "start"), event(9, 0x2000_0100, "stop")])
    stats = value_stats(records)
    assert stats["temp"] == {"count": 3, "min": 1, "max": 9, "mean": 5, "last": 1, "last_t": 3, "source": "itm",
                             "index": 1}
    assert stats["pos.x"]["last"] == 1 and stats["pos.y"]["max"] == 3
    rows = record_rows(records, pair_intervals(records).intervals)
    assert ("user", "value", 1, 1, 5, {"name": "temp", "value": 5, "source": "itm", "index": 1}, None, None) in rows
    assert ("user", "start", 5, 0, 0, {"name": "adc", "source": "itm"}, None, None) in rows
    assert ("user", "interval", 5, 0, 0, {"name": "adc", "duration": 4, "depth": 0}, 9, None) in rows


def test_elf_strings_reads_nul_terminated_names_from_the_image():
    from fprobe.symbols import Segment
    image = type("Img", (), {"segments": (Segment(0x2000_0000, b"\x00" * 0x100 + b"adc\x00f:volt\x00"),)})()
    names = elf_strings(image)
    assert names(0x2000_0100) == "adc" and names(0x2000_0104) == "f:volt" and names(0x3000_0000) is None
