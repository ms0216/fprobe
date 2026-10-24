"""defmt の解読（fprobe.logfmt.defmt）。表の作り方・枠・引数の符号化・表示の規則は defmt の公式の解読器
（knurling-rs/defmt decoder/src/lib.rs の試験）に合わせる。ELF からの表は host/firmware/defmt_demo（zig cc）。"""

from pathlib import Path

import pytest

from fprobe.logfmt.defmt import DefmtDecoder, DefmtEntry, DefmtError, DefmtTable, parse_format
from fprobe.logfmt.framing import rzcobs_encode

ELF = Path(__file__).resolve().parents[1] / "firmware" / "defmt_demo" / "defmt_demo.elf"


def table(entries: dict, timestamp: str | None = "{=u8:us}", encoding: str = "raw") -> DefmtTable:
    return DefmtTable({i: (DefmtEntry(*e) if isinstance(e, tuple) else DefmtEntry("info", e))
                       for i, e in entries.items()}, timestamp=timestamp, encoding=encoding)


def line(t: DefmtTable, frame: bytes) -> str:
    record, used = t.decode_frame(frame)
    assert record.error is None, record.error
    ts = f"{record.timestamp_text} " if record.timestamp_text else ""
    level = f"{record.level.upper()} " if record.level and record.level != "println" else ""
    return ts + level + record.text


@pytest.mark.parametrize("fmt,data,expected", [
    ("i16 as hex {=i16:#x}", "000002ffff", "0.000002 INFO i16 as hex 0xffff"),
    ("my bool={=bool}", "00000201", "0.000002 INFO my bool=true"),
    ("x: {0=0..4:b}, y: {0=3..8:#b}", "000002e5", "0.000002 INFO x: 101, y: 0b11100"),
    ("#0: {0=7..12:b}, #1: {1=u8}, #2: {2=0..5:b}", "000002f0e52af1", "0.000002 INFO #0: 1011, #1: 42, #2: 10001"),
    ("bitfields {0=9..14:b}", "00000263", "0.000002 INFO bitfields 10001"),
    ("bitfields {0=0..2:b} {0=28..31:b}", "000002630f5ac3", "0.000002 INFO bitfields 11 100"),
    ("x={=[u8]}", "000002020000001" "72a", "0.000002 INFO x=[23, 42]"),
    ("Hello {=str} {=u8}", "00000205000000" + b"World".hex() + "7d", "0.000002 INFO Hello World 125"),
    ("Supports ASCII {=char} and Unicode {=char}", "000002610000009cf40100",
     "0.000002 INFO Supports ASCII a and Unicode \U0001f49c"),
    ("{=u64:iso8601ms}", "00000224bc97ee78010000", "0.000002 INFO 2021-04-20T09:23:44.804Z"),
    ("{=u32:#010x} {=u8:08b} {=i8}", "000002efbeadde05ff", "0.000002 INFO 0xdeadbeef 00000101 -1"),
])
def test_official_vectors(fmt, data, expected):
    assert line(table({0: fmt}), bytes.fromhex(data)) == expected


def test_nested_format_and_hint_propagation():
    t = table({0: "x={=?}", 1: ("derived", "Foo {{ x: {=u8} }}")})
    assert line(t, bytes.fromhex("000002010" "02a")) == "0.000002 INFO x=Foo { x: 42 }"
    inner_wins = table({0: "x={:b}", 1: ("derived", "S {{ x: {=u8:x} }}")})
    assert line(inner_wins, bytes.fromhex("00000201002a")) == "0.000002 INFO x=S { x: 2a }"
    outer = table({0: "x={:b}", 1: ("derived", "S {{ x: {=u8:?} }}")})
    assert line(outer, bytes.fromhex("00000201002a")) == "0.000002 INFO x=S { x: 101010 }"
    debug_str = table({0: "{}", 1: ("derived", "S {{ x: {=str:?} }}")})
    assert line(debug_str, bytes.fromhex("0000020100050000004865" "6c6c6f")) == '0.000002 INFO S { x: "Hello" }'


def test_slices_of_format_share_one_index():
    t = table({0: ("prim", "{=u8}"), 1: ("prim", "{=[?]}"), 2: ("derived", "Data {{ name: {=?:?} }}"),
               3: "{=[?]:a}"})
    frame = bytes.fromhex("0300" "02" "01000000" "0200" "0100" "02000000" "0000" "4869")
    assert line(t, frame) == '0.000002 INFO [Data { name: b"Hi" }]'


def test_enums_pick_the_variant_by_discriminant():
    t = table({4: "x={=?}", 3: ("derived", "None|Some({=?})"), 2: ("derived", "{=u8}")})
    assert line(t, bytes.fromhex("0400000300010200" "2a")) == "0.000000 INFO x=Some(42)"
    assert line(t, bytes.fromhex("040001030000")) == "0.000001 INFO x=None"


def test_format_sequence():
    t = table({0: ("derived", "{=__internal_FormatSequence}"), 1: ("write", "Foo"), 2: ("write", "Bar({=u8})"),
               5: "seq {=?}"}, timestamp=None)
    assert line(t, bytes.fromhex("0500" "0000" "0100" "0200" "2a" "0000")) == "INFO seq FooBar(42)"


def test_arguments_are_read_in_index_order():
    t = table({1: ("debug", "The answer is {1=u16} {0=u8} {1=u16}!")}, timestamp=None)
    record, used = t.decode_frame(bytes.fromhex("01002affff"))
    assert record.text == "The answer is 65535 42 65535!" and record.level == "debug" and used == 5
    assert record.args == (42, 0xFFFF) and record.template == "The answer is {1=u16} {0=u8} {1=u16}!"


def test_internal_debug_and_istr_and_println():
    t = table({0: ("println", "{=__internal_Debug} {=istr}"), 1: ("str", "interned")}, timestamp=None)
    record, _ = t.decode_frame(b"\x00\x00" + b"Some(1)" + b"\xff" + b"\x01\x00")
    assert record.text == "Some(1) interned" and record.level == "println"


def test_timestamp_forms():
    for hint, value, text in [("tms", 3_723_004, "01:02:03.004"), ("ms", 1234, "1.234"),
                              ("tus", 90_061_000_001, "1:01:01:01.000001"), ("ts", 59, "00:00:59")]:
        t = table({0: "x"}, timestamp=f"{{=u64:{hint}}}")
        record, _ = t.decode_frame(b"\x00\x00" + value.to_bytes(8, "little"))
        assert record.timestamp == value and record.timestamp_text == text, hint


def test_bad_frames_are_reported_not_raised():
    t = table({0: "{=u32}"}, timestamp=None)
    with pytest.raises(DefmtError):
        t.decode_frame(bytes.fromhex("0000aa"))                        # 引数が足りない
    with pytest.raises(DefmtError):
        t.decode_frame(bytes.fromhex("0700"))                          # 表に無い番号
    level_less = table({0: ("derived", "x")}, timestamp=None)
    with pytest.raises(DefmtError):
        level_less.decode_frame(b"\x00\x00")                           # 先頭がログでない


def test_parse_format_grammar():
    fragments = parse_format("a {{b}} {=u8:#04x} {1=[u8; 4]} {=0..3} {:?} {=[?; 2]}")
    params = [p for p in fragments if not isinstance(p, str)]
    assert [(p.index, p.type, p.hint.kind if p.hint else None) for p in params] == [
        (0, "u8", "x"), (1, "[u8;4]", None), (1, "bits", None), (2, "format", "?"), (3, "[?;2]", None)]
    assert params[0].hint.alternate and params[0].hint.zero_pad == 4 and params[2].bits == (0, 3)


# --- 流れ ---------------------------------------------------------------------------------------

def test_rzcobs_stream_decoder_splits_frames_across_chunks():
    t = table({1: "n={=u8}"}, timestamp=None, encoding="rzcobs")
    stream = b"\x00" + rzcobs_encode(b"\x01\x00\x07") + b"\x00" + rzcobs_encode(b"\x01\x00\x00") + b"\x00"
    d = DefmtDecoder(t)
    out = d.feed(stream[:4]) + d.feed(stream[4:])
    assert [r.text for r in out] == ["n=7", "n=0"]


def test_rzcobs_stream_keeps_going_after_a_bad_frame():
    t = table({1: "n={=u8}"}, timestamp=None, encoding="rzcobs")
    d = DefmtDecoder(t)
    out = d.feed(rzcobs_encode(b"\x09\x00") + b"\x00" + rzcobs_encode(b"\x01\x00\x05") + b"\x00")
    assert out[0].error and out[0].raw.startswith(b"\x09\x00") and out[1].text == "n=5"


def test_raw_stream_decoder_waits_for_the_whole_frame_and_stops_on_garbage():
    t = table({1: "n={=u16}"}, timestamp=None, encoding="raw")
    d = DefmtDecoder(t)
    assert d.feed(b"\x01\x00\x34") == []
    assert [r.text for r in d.feed(b"\x12\x01\x00")] == ["n=4660"]
    assert [r.text for r in d.feed(b"\x02\x00")] == ["n=2"]
    bad = d.feed(b"\x09\x00\x00")
    assert bad[0].error and d.synced is False and d.feed(b"\x01\x00\x01\x00") == []


# --- ELF ----------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def elf_table():
    return DefmtTable.from_elf(ELF)


def test_table_from_elf(elf_table):
    assert elf_table.version == "4" and elf_table.encoding == "rzcobs"
    assert elf_table.timestamp == "{=u32:us}"
    levels = {e.tag for e in elf_table.entries.values()}
    assert {"info", "warn", "error", "debug", "println", "prim", "derived", "str"} <= levels
    hello = elf_table.find("Hello {=str}, count={=u8}")
    entry = elf_table.entries[hello]
    assert entry.tag == "info" and entry.location["file"].endswith("defmt_demo.c") and entry.location["line"] > 0
    assert entry.package == "demo" and entry.crate == "demo"


def test_decode_with_the_elf_table(elf_table):
    hello = elf_table.find("Hello {=str}, count={=u8}")
    frame = hello.to_bytes(2, "little") + (1500).to_bytes(4, "little") + (3).to_bytes(4, "little") + b"you" + b"\x09"
    d = DefmtDecoder(elf_table)
    [record] = d.feed(b"\x00" + rzcobs_encode(frame) + b"\x00")
    assert (record.text, record.level, record.timestamp, record.timestamp_text) == \
        ("Hello you, count=9", "info", 1500, "0.001500")
    assert record.location["line"] == elf_table.entries[hello].location["line"]
    assert record.format == "defmt" and record.id == hello


def test_an_elf_without_defmt_is_refused():
    with pytest.raises(DefmtError):
        DefmtTable.from_elf(Path(__file__).resolve().parents[1] / "firmware" / "rtt_demo" / "rtt_demo.elf")


def test_framing_is_not_assumed_when_the_elf_does_not_say():
    """_defmt_encoding_ の無い表の枠を rzcobs と仮定しない（使う側が framing で明示する）。"""
    t = DefmtTable({1: DefmtEntry("info", "x")})
    assert t.encoding is None
    with pytest.raises(DefmtError, match="framing"):
        DefmtDecoder(t)


def test_rzcobs_stream_reports_bytes_dropped_without_a_delimiter():
    """区切りの来ない流れを上限（frame_limit）で捨てたら、その行に誤りと使った上限（cut_at）を出す。"""
    t = table({1: "n={=u8}"}, timestamp=None, encoding="rzcobs")
    d = DefmtDecoder(t, frame_limit=8)
    out = d.feed(b"\x11" * 9 + b"\x00" + rzcobs_encode(b"\x01\x00\x05") + b"\x00")
    assert out[0].error and out[0].cut_at == 8 and "9" in out[0].error and d.errors == 1
    assert out[-1].text == "n=5"
