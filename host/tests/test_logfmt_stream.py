"""ログの形式の選び方と流れの組み立て（fprobe.logfmt.stream）。"""

import json
from pathlib import Path

import pytest

from fprobe.logfmt.framing import rzcobs_encode
from fprobe.logfmt.record import LogRecord, level_at_least
from fprobe.logfmt.stream import LogStream, TextDecoder
from fprobe.symbols import Symbols

FW = Path(__file__).resolve().parents[1] / "firmware"


def test_text_lines_across_chunks_and_long_lines():
    d = TextDecoder()
    assert d.feed(b"ab") == []
    assert [r.text for r in d.feed(b"c\r\nde\nf")] == ["abc", "de"]
    long = d.feed(b"x" * 5000)
    assert len(long) == 1 and len(long[0].text) == 4096


def test_text_with_addresses_is_annotated():
    syms = Symbols(FW / "rtt_demo" / "rtt_demo.elf")
    stream = LogStream.create("text", symbols=syms)
    a = syms.symbol("rtt_read").address
    [r] = stream.feed(f"PC 0x{a:08x}\n".encode())
    assert r.addresses[0].function == "rtt_read"
    assert r.to_dict()["addresses"][0]["function"] == "rtt_read"


def test_defmt_stream_from_the_elf():
    stream = LogStream.create("defmt", elf=FW / "defmt_demo" / "defmt_demo.elf")
    table = stream.decoder.table
    warn = table.find("temperature {=i16} is high")
    frame = warn.to_bytes(2, "little") + (10).to_bytes(4, "little") + (-5).to_bytes(2, "little", signed=True)
    [r] = stream.feed(b"\x00" + rzcobs_encode(frame) + b"\x00")
    assert (r.text, r.level) == ("temperature -5 is high", "warn")


def test_trice_needs_a_til(tmp_path):
    with pytest.raises(ValueError):
        LogStream.create("trice")
    til = tmp_path / "til.json"
    til.write_text(json.dumps({"5": {"Type": "trice", "Strg": "hi\\n"}}))
    stream = LogStream.create("trice", til=til, framing="none")
    assert [r.text for r in stream.feed(bytes.fromhex("0540c000"))] == ["hi"]


def test_unknown_formats_and_missing_elf_are_refused():
    with pytest.raises(ValueError):
        LogStream.create("syslog")
    with pytest.raises(ValueError):
        LogStream.create("defmt")


def test_levels_filter():
    assert level_at_least("error", "warn") and not level_at_least("debug", "info")
    assert level_at_least("println", "error") and level_at_least(None, "error") and level_at_least("trace", None)
    assert LogRecord("x", "defmt", error="bad").kind == "undecoded"
