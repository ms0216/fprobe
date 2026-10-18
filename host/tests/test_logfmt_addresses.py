"""ログの文字の中のコードの番地を関数・ファイル:行にする（fprobe.logfmt.addresses）。rtt_demo の ELF を使う。"""

from pathlib import Path

import pytest

from fprobe.logfmt.addresses import AddressAnnotator
from fprobe.symbols import Symbols

ELF = Path(__file__).resolve().parents[1] / "firmware" / "rtt_demo" / "rtt_demo.elf"


@pytest.fixture(scope="module")
def annotator():
    return AddressAnnotator(Symbols(ELF))


def test_a_code_address_becomes_function_and_line(annotator):
    syms = Symbols(ELF)
    f = syms.symbol("rtt_read")
    text = f"Guru: PC=0x{f.address + 2:08x} done"
    hits = annotator.find(text)
    assert [(h.address, h.function, h.file, h.line) for h in hits] == [(f.address + 2, "rtt_read", "rtt_demo.c", 131)]
    assert hits[0].start == text.index("0x") and hits[0].end == text.index(" done")
    assert annotator.annotate(text) == f"Guru: PC=0x{f.address + 2:08x} [rtt_read+2 rtt_demo.c:131] done"


def test_the_thumb_bit_is_dropped(annotator):
    f = Symbols(ELF).symbol("systick_handler")
    hits = annotator.find(f"LR 0x{f.address + 3:08X}")
    assert hits[0].function == "systick_handler" and hits[0].offset == 2 and hits[0].address == f.address + 3


def test_data_and_out_of_range_numbers_are_left_alone(annotator):
    syms = Symbols(ELF)
    data = syms.variable("_SEGGER_RTT").address
    assert annotator.find(f"cb at 0x{data:08x}, len 0x10, sp 0x2000ffff") == []


def test_backtrace_pairs_decode_only_the_code_side(annotator):
    syms = Symbols(ELF)
    a, b = syms.symbol("rtt_write").address + 4, syms.symbol("reset_handler").address + 8
    line = f"Backtrace: 0x{a:08x}:0x20007f00 0x{b:08x}:0x20007f20"
    assert [h.function for h in annotator.find(line)] == ["rtt_write", "reset_handler"]


def test_hits_are_jsonable(annotator):
    f = Symbols(ELF).symbol("rtt_read")
    d = annotator.find(f"0x{f.address:08x}")[0].to_dict()
    assert d == {"address": f.address, "where": "rtt_read+0 (rtt_demo.c:131)", "function": "rtt_read", "offset": 0,
                 "file": "rtt_demo.c", "line": 131}
