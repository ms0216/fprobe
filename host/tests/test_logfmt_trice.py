"""Trice の解読（fprobe.logfmt.trice）。til.json / li.json と、TCOBS の枠・パケット（ID のセレクタ・時刻・NC）。
例 1・2 は trice の試験（internal/trexDecoder/trexDecoder_test.go の TestTREX）の受信バイトと出力。"""

import json
import struct

import pytest

from fprobe.logfmt.framing import cobs_encode
from fprobe.logfmt.trice import TriceDecoder, TriceError, TriceTable, go_format, trice_type

TIL = {
    "3713": {"Type": "TRICE16", "Strg": "MSG: 💚 START select = %d\\n"},
    "935": {"Type": "TRICE",
            "Strg": "MSG:triceFifoDepthMax = %d of max %d, triceStreamBufferDepthMax = %d of max %d\\n"},
    "13505": {"Type": "trice", "Strg": "inf:started\\n"},
    "14008": {"Type": "TRICE32", "Strg": "dbg:%d %d %d %d %d %d\\n"},
    "100": {"Type": "TRICE8", "Strg": "part %u, "},
    "101": {"Type": "TRICE8", "Strg": "end %x\\n"},
    "102": {"Type": "TRICE_S", "Strg": "rx:received command:%s\\n"},
    "103": {"Type": "TRICE64_1", "Strg": "err:value %f\\n"},
    "104": {"Type": "TRICE8_B", "Strg": "%02x "},
}
LI = {"3713": {"File": "src/main.c", "Line": 42}}


@pytest.fixture
def table(tmp_path):
    til, li = tmp_path / "til.json", tmp_path / "li.json"
    til.write_text(json.dumps(TIL, ensure_ascii=False), encoding="utf-8")
    li.write_text(json.dumps(LI), encoding="utf-8")
    return TriceTable.load(til, li)


def packet(trice_id: int, payload: bytes, selector: int = 1, ts: int = 0, cycle: int = 0xC0) -> bytes:
    head = struct.pack("<H", selector << 14 | trice_id)
    stamp = b"" if selector == 1 else struct.pack("<H", ts) if selector == 2 else struct.pack("<I", ts)
    return head + stamp + struct.pack("<H", len(payload) << 8 | cycle) + payload


def test_official_example_1(table):
    d = TriceDecoder(table)
    [r] = d.feed(bytes.fromhex("818e0923c002b801a400"))
    assert (r.text, r.id, r.timestamp, r.channel, r.args) == ("MSG: 💚 START select = 440", 3713, 9, None, (440,))
    assert r.location == {"file": "src/main.c", "line": 42} and r.format == "trice"
    assert r.template == "MSG: 💚 START select = %d\\n"


def test_official_example_2(table):
    d = TriceDecoder(table)
    [r] = d.feed(bytes.fromhex("a7831b23c1105c638061500562084100"))
    assert r.text == "MSG:triceFifoDepthMax = 92 of max 128, triceStreamBufferDepthMax = 1360 of max 2048"
    assert r.timestamp == 0x1B


def test_negative_values_and_lowercase_tags(table):
    # 行頭の「xxx:」をチャンネルとみなすのは、使う側が levels（タグ → 重要度。重要度なしは None）で明示したタグ
    # だけ。名前の形（小文字だけ等）で決めず、本文も書き換えない（"value: 3" のような普通の文を壊さない）
    raw = bytes.fromhex("c174e22300") + bytes.fromhex("b8767b1884fee1fde1fce1fbe1fae100")
    out = TriceDecoder(table).feed(raw)
    assert [(r.text, r.level, r.channel) for r in out] == [("inf:started", None, None),
                                                           ("dbg:-1 -2 -3 -4 -5 -6", None, None)]
    out = TriceDecoder(table, levels={"inf": "info", "dbg": None}).feed(raw)
    assert [(r.text, r.level, r.channel) for r in out] == [("inf:started", "info", "inf"),
                                                           ("dbg:-1 -2 -3 -4 -5 -6", None, "dbg")]
    with pytest.raises(TriceError, match="levels"):
        TriceDecoder(table, levels={"inf": "information"})


def test_a_trice_without_newline_joins_the_next_one(table):
    d = TriceDecoder(table, framing="cobs")
    frame = packet(100, b"\x07") + packet(101, b"\xff", cycle=0xC1)            # 1 つの枠に 2 つ（パディング無し）
    [r] = d.feed(cobs_encode(frame + b"\x00\x00") + b"\x00")                    # 末尾の 4 バイト未満の 0 は詰め物
    assert r.text == "part 7, end ff" and r.id == 100 and r.args == (7, 255)


def test_strings_floats_and_buffers(table):
    d = TriceDecoder(table, framing="none")
    data = (packet(102, b"reset") + packet(103, struct.pack("<d", 2.5), cycle=0xC1) +
            packet(104, b"\x01\xab\x10", cycle=0xC2))
    out = d.feed(data)
    assert [r.text for r in out[:2]] == ["rx:received command:reset", "err:value 2.500000"]
    assert out[1].level is None and out[1].channel is None
    assert d.feed(packet(101, b"\x05", cycle=0xC3))[0].text == "01 ab 10 end 5"


def test_32_bit_timestamps_and_big_endian(table):
    d = TriceDecoder(table, framing="none", big_endian=True)
    data = struct.pack(">HIH", 3 << 14 | 3713, 0x12345678, 2 << 8 | 0xC0) + struct.pack(">h", -3)
    [r] = d.feed(data)
    assert (r.text, r.timestamp) == ("MSG: 💚 START select = -3", 0x12345678)


def test_cycle_errors_are_noted(table):
    d = TriceDecoder(table, framing="none")
    d.feed(packet(100, b"\x01", cycle=0xC5) + packet(101, b"\x01", cycle=0xC6))
    [r] = d.feed(packet(101, b"\x02", cycle=0xC9))
    assert d.cycle_errors == 1 and any("2" in n for n in r.notes)
    # 0xC0 への飛びも、ほかの飛びと同じく数える（取りこぼしとリセットを番号だけでは区別できない。解釈しない）
    [r] = d.feed(packet(101, b"\x03", cycle=0xC0))
    assert d.cycle_errors == 2 and not any("リセット" in n for n in r.notes)
    assert any("0xCA" in n and "0xC0" in n for n in r.notes)


def test_unknown_ids_and_bad_lengths_are_undecoded(table):
    d = TriceDecoder(table, framing="cobs")
    out = d.feed(cobs_encode(packet(7777, b"\x01\x02")) + b"\x00" + cobs_encode(packet(3713, b"\x01")) + b"\x00")
    assert [r.kind for r in out] == ["undecoded", "undecoded"]
    assert "7777" in out[0].error and out[0].raw is not None


def test_types_are_normalized():
    assert trice_type("TRICE16", 1, 32) == ("fixed", 16)
    assert trice_type("trice", 2, 32) == ("fixed", 32)
    assert trice_type("Trice8_2", 2, 32) == ("fixed", 8)
    assert trice_type("TRICE_S", 1, 32) == ("string", 0)
    assert trice_type("triceN", 1, 32) == ("string", 0)
    assert trice_type("TRICE16_B", 1, 32) == ("buffer", 16)
    assert trice_type("TRice", 0, 16) == ("fixed", 16)
    assert trice_type("triceAssertTrue", 1, 32) == ("fixed", 32)
    with pytest.raises(TriceError):
        trice_type("TRICE8_3", 2, 32)


def test_go_like_formatting():
    assert go_format("%d %u %x %X %o %b %t %c %5d|%-4d|%08.3f %q %%", [
        (-1, 32), (4294967295, 32), (255, 32), (255, 32), (8, 32), (5, 32), (1, 32), (65, 32), (42, 32), (7, 32),
        (3.14159, 32), ("hi", 0)], unsigned=True) == \
        "-1 4294967295 ff FF 10 101 true A    42|7   |0003.142 \"hi\" %"
    assert go_format("%lu %llx %p %zd", [(1, 32), (255, 64), (16, 32), (-2, 32)], unsigned=True) == "1 ff 10 -2"


def test_framed_stream_reports_bytes_dropped_without_a_delimiter(table):
    d = TriceDecoder(table, frame_limit=8)
    out = d.feed(b"\x11" * 9 + b"\x00")
    assert len(out) == 1 and out[0].error and out[0].cut_at == 8 and d.errors == 1
