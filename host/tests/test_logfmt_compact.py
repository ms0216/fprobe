"""Compact logging の解読（fprobe.logfmt.compact）: 書式の文字列の番地と引数の語（TZ-11）、Memfault の compact log
（log_fmt の区画と CBOR、MF-07）。ELF は host/firmware/compact_demo（zig cc）。"""

import base64
import struct
from pathlib import Path

import pytest

from fprobe.logfmt.compact import (AddressLogDecoder, ElfStrings, MemfaultDecoder, MemfaultTable, cbor_decode,
                                   cbor_encode)
from fprobe.symbols import Symbols

ELF = Path(__file__).resolve().parents[1] / "firmware" / "compact_demo" / "compact_demo.elf"


@pytest.fixture(scope="module")
def syms():
    return Symbols(ELF)


def words(*values: int) -> bytes:
    return b"".join(struct.pack("<I", v & 0xFFFFFFFF) for v in values)


def test_strings_are_read_from_the_elf(syms):
    s = ElfStrings(ELF)
    assert s.get(syms.variable("fmt_temp").address) == "temp=%d.%02u C"
    assert s.is_start(syms.variable("name_run").address)
    assert not s.is_start(syms.variable("fmt_temp").address + 2)
    assert s.get(0x2000_0000) is None


def test_address_logs(syms):
    d = AddressLogDecoder(Symbols(ELF))
    temp, state = syms.variable("fmt_temp").address, syms.variable("fmt_state").address
    idle, run = syms.variable("name_idle").address, syms.variable("name_run").address
    out = d.feed(words(temp, 21, 5, state, idle)[:-2])
    assert [r.text for r in out] == ["temp=21.05 C"]
    out = d.feed(words(idle, run)[2:])
    assert [(r.text, r.id, r.format) for r in out] == [("motor idle -> run", state, "compact")]
    assert out[0].location == {"symbol": "fmt_state"}


def test_address_logs_with_64_bit_arguments(syms):
    d = AddressLogDecoder(Symbols(ELF))
    lo, hi = struct.unpack("<II", struct.pack("<q", -7))
    f0, f1 = struct.unpack("<II", struct.pack("<d", 0.5))
    [r] = d.feed(words(syms.variable("fmt_wide").address, lo, hi, f0, f1))
    assert r.text == "big -7 0.500000"


def test_address_logs_resynchronize_on_unknown_words(syms):
    d = AddressLogDecoder(Symbols(ELF))
    temp = syms.variable("fmt_temp").address
    out = d.feed(words(0xDEADBEEF, 0x12345678, temp, 1, 2))
    assert [r.kind for r in out] == ["undecoded", "log"] and out[1].text == "temp=1.02 C"
    assert out[0].raw == words(0xDEADBEEF, 0x12345678)


# --- Memfault ---------------------------------------------------------------------------------------

def test_cbor_round_trip():
    for value in [0, 23, 24, 255, 256, 65536, 2**32, -1, -500, "héllo", b"\x01\x02", [1, [2, 3], "x"], 1.5,
                  {0: 1, 1: 300}]:
        encoded = cbor_encode(value)
        assert cbor_decode(encoded) == (value, len(encoded))


def test_cbor_known_bytes():
    assert cbor_decode(bytes.fromhex("820a0b")) == ([10, 11], 3)                      # README の例
    assert cbor_decode(bytes.fromhex("fb3ff8000000000000"))[0] == 1.5
    assert cbor_decode(bytes.fromhex("f93e00"))[0] == 1.5                             # half
    assert cbor_decode(bytes.fromhex("9f0102ff")) == ([1, 2], 4)                      # 長さを決めない配列


def test_memfault_table_from_the_elf():
    t = MemfaultTable.from_elf(ELF)
    assert t.entries[8] == (1, "app/main.c", 21, "boot count %d")
    sensor = next(i for i, e in t.entries.items() if e[3] == "%s: %d (%f)")
    assert sensor == 8 + len("1;app/main.c;21;boot count %d") + 1


def test_memfault_cbor_stream():
    t = MemfaultTable.from_elf(ELF)
    sensor = next(i for i, e in t.entries.items() if e[3] == "%s: %d (%f)")
    ready = next(i for i, e in t.entries.items() if e[3] == "ready")
    d = MemfaultDecoder(t)
    data = cbor_encode([8, 7]) + cbor_encode([sensor, "temp", [-3], 21.5]) + cbor_encode([ready])
    out = d.feed(data[:5]) + d.feed(data[5:])
    assert [r.text for r in out] == ["boot count 7", "temp: -3 (21.500000)", "ready"]
    assert out[0].location == {"file": "app/main.c", "line": 21} and out[1].args == ("temp", -3, 21.5)


def test_memfault_text_lines_and_truncated_records():
    t = MemfaultTable.from_elf(ELF)
    d = MemfaultDecoder(t, transport="text")
    chunk = base64.b64encode(cbor_encode([8, 42])).decode()
    big = base64.b64encode(cbor_encode([8, {0: 1, 1: 900}])).decode()
    out = d.feed(f"boot\r\nML:{chunk}:\nML:{big}:\n".encode())
    assert [(r.format, r.text) for r in out] == [("text", "boot"), ("memfault", "boot count 42"),
                                                 ("memfault", "boot count <?>")]
    assert out[2].notes and "900" in out[2].notes[0]


def test_memfault_unknown_offsets_are_undecoded():
    d = MemfaultDecoder(MemfaultTable.from_elf(ELF))
    [r] = d.feed(cbor_encode([3, 1]))
    assert r.kind == "undecoded" and "3" in r.error
