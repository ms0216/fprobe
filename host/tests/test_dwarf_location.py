"""fprobe.dwarf.location: 位置式のスタック機械と、場所からの読み出し（合成した式）。"""

import pytest
from elftools.dwarf.dwarf_expr import DWARFExprParser
from elftools.dwarf.structs import DWARFStructs

from fprobe.dwarf.access import AccessError, ImageAccess
from fprobe.dwarf.location import (EvalContext, MemoryPlace, NotAvailable, PiecesPlace, RegisterPlace, Unavailable,
                                   ValuePlace, evaluate_location, evaluate_value, read_place)

PARSER = DWARFExprParser(DWARFStructs(little_endian=True, dwarf_format=32, address_size=4))


def ops(*code: int):
    return PARSER.parse_expr(bytes(code))


def context(registers=None, memory=None, **kw):
    mem = ImageAccess(memory or [])
    regs = registers or {}

    def read_register(n):
        if n not in regs:
            raise AccessError(f"r{n}")
        return regs[n]
    return EvalContext(mem.read_memory, read_register, **kw)


def test_address_and_register_relative_locations():
    assert evaluate_location(ops(0x03, 0x00, 0x01, 0x00, 0x20), context()) == MemoryPlace(0x20000100)   # DW_OP_addr
    assert evaluate_location(ops(0x7D, 0x10), context({13: 0x2000_7F00})) == MemoryPlace(0x2000_7F10)   # breg13 +16
    assert evaluate_location(ops(0x7D, 0x7C), context({13: 0x2000_7F00})) == MemoryPlace(0x2000_7EFC)   # breg13 -4
    assert evaluate_location(ops(0x55), context()) == RegisterPlace(5)                                  # DW_OP_reg5
    assert evaluate_location(ops(0x90, 0x88, 0x02), context()) == RegisterPlace(264)                     # regx d8
    fb = context(frame_base=lambda: 0x1000)
    assert evaluate_location(ops(0x91, 0x08), fb) == MemoryPlace(0x1008)                                # fbreg +8
    assert evaluate_location(ops(0x9C), context(cfa=0x2000)) == MemoryPlace(0x2000)                    # call_frame_cfa


def test_arithmetic_stack_operations_and_branches():
    # lit5 lit3 minus → 2 / lit2 lit3 shl → 16 / consts -1 abs → 1
    assert evaluate_value(ops(0x35, 0x33, 0x1C), context()) == 2
    assert evaluate_value(ops(0x32, 0x33, 0x24), context()) == 16
    assert evaluate_value(ops(0x11, 0x7F, 0x19), context()) == 1
    # lit1 bra(+1) lit7 lit9 → 分岐して lit7 を飛ばす → 9
    assert evaluate_value(ops(0x31, 0x28, 0x01, 0x00, 0x37, 0x39), context()) == 9
    # lit0 bra(+1) lit7 → 分岐しない → 7
    assert evaluate_value(ops(0x30, 0x28, 0x01, 0x00, 0x37), context()) == 7
    # lit1 lit2 swap minus → 2 - 1 = 1、lit6 lit2 div → 3、consts -7 lit2 div → -3（0 に向かって）
    assert evaluate_value(ops(0x31, 0x32, 0x16, 0x1C), context()) == 1
    assert evaluate_value(ops(0x36, 0x32, 0x1B), context()) == 3
    assert evaluate_value(ops(0x11, 0x79, 0x32, 0x1B), context()) == (-3) & 0xFFFFFFFF


def test_deref_reads_memory_and_frame_base_registers_mean_their_value():
    mem = [(0x2000, (0x12345678).to_bytes(4, "little"))]
    assert evaluate_value(ops(0x0A, 0x00, 0x20, 0x06), context(memory=mem)) == 0x12345678       # const2u deref
    assert evaluate_value(ops(0x0A, 0x00, 0x20, 0x94, 0x02), context(memory=mem)) == 0x5678     # deref_size 2
    assert evaluate_value(ops(0x57), context({7: 0x2000_7000})) == 0x2000_7000                  # frame_base = reg7


def test_values_and_pieces():
    c = context({4: 3})
    place = evaluate_location(ops(0x74, 0x00, 0x9F), c)                       # breg4 stack_value
    assert isinstance(place, ValuePlace) and read_place(place, 4, c) == (3).to_bytes(4, "little")
    # corner = { breg4 → piece 2, 0 - breg4 → piece 2 }: int16 の (3, -3)
    corner = evaluate_location(ops(0x74, 0x00, 0x9F, 0x93, 0x02, 0x30, 0x74, 0x00, 0x1C, 0x9F, 0x93, 0x02), c)
    assert isinstance(corner, PiecesPlace)
    assert read_place(corner, 4, c) == (3).to_bytes(2, "little") + (-3 & 0xFFFF).to_bytes(2, "little")
    implicit = evaluate_location(ops(0x9E, 0x02, 0xAB, 0xCD), c)               # implicit_value 2 バイト
    assert read_place(implicit, 2, c) == b"\xAB\xCD"
    reg = evaluate_location(ops(0x54), c)
    assert read_place(reg, 2, c) == b"\x03\x00"                                 # レジスタの下位から


def test_convert_truncates_and_sign_extends():
    types = {0x2B: (4, False), 0x2F: (2, False), 0x30: (1, True)}
    c = context({4: 0x1_80FF}, base_type=types.__getitem__)
    assert evaluate_value(ops(0x74, 0x00, 0xA8, 0x2F, 0x9F), c) == 0x80FF                      # u16
    assert evaluate_value(ops(0x74, 0x00, 0xA8, 0x30, 0x9F), c) == 0xFFFFFFFF                  # s8 の -1


def test_unavailable_values_say_why():
    assert evaluate_location([], context()) == Unavailable("最適化で消えた")
    entry = evaluate_location(ops(0xA3, 0x01, 0x50, 0x9F), context())                  # entry_value(reg0)
    assert isinstance(entry, Unavailable) and "entry value" in entry.reason
    missing = evaluate_location(ops(0x70, 0x00), context())                            # breg0、r0 が分からない
    assert isinstance(missing, Unavailable) and "r0" in missing.reason
    with pytest.raises(NotAvailable):
        read_place(RegisterPlace(1), 4, context())
    with pytest.raises(NotAvailable):
        read_place(MemoryPlace(0x4000), 4, context())
