"""fprobe.rtos.cortexm: 止まっているタスクの保存された文脈（port の積み方）とレジスタの読み手。"""

import pytest

from fprobe.dwarf.access import AccessError, ImageAccess, TargetAccess
from fprobe.rtos.cortexm import ContextAccess, read_saved_context, unstack_exception_frame

TOP = 0x2000_1000


def put(memory: ImageAccess, address: int, words: list[int]) -> int:
    for i, w in enumerate(words):
        memory.write_u32(address + 4 * i, w)
    return address + 4 * len(words)


def hw_frame(pc=0x0800_0101, xpsr=0x0100_0000):
    return [0xA0, 0xA1, 0xA2, 0xA3, 0xAC, 0x0800_0201, pc, xpsr]          # r0 r1 r2 r3 r12 lr pc xpsr


def test_armv7m_fp_without_fp_context():
    m = ImageAccess()
    end = put(m, TOP, [0x44, 0x55, 0x66, 0x77, 0x88, 0x99, 0xAA, 0xBB, 0xFFFF_FFFD] + hw_frame())
    ctx = read_saved_context(m.read_memory, TOP, "armv7m_fp")
    assert not ctx.fp and ctx.exc_return == 0xFFFF_FFFD
    r = ctx.registers
    assert [r[f"r{n}"] for n in range(4, 12)] == [0x44, 0x55, 0x66, 0x77, 0x88, 0x99, 0xAA, 0xBB]
    assert [r[f"r{n}"] for n in range(4)] == [0xA0, 0xA1, 0xA2, 0xA3] and r["r12"] == 0xAC
    assert r["lr"] == 0x0800_0201 and r["pc"] == 0x0800_0100 and r["xpsr"] == 0x0100_0000
    assert ctx.sp == end and r["sp"] == end and r["psp"] == end
    assert not any(name.startswith("s") and name[1:].isdigit() for name in r)


def test_armv7m_fp_with_extended_frame_and_padding():
    m = ImageAccess()
    s_high = [0x4000_0000 + n for n in range(16, 32)]
    s_low = [0x3F00_0000 + n for n in range(16)]
    words = [0x44, 0x55, 0x66, 0x77, 0x88, 0x99, 0xAA, 0xBB, 0xFFFF_FFED] + s_high
    words += hw_frame(xpsr=0x0100_0200) + s_low + [0x0300_0000, 0]          # FPSCR、予約。xpsr bit 9 = 詰め物
    end = put(m, TOP, words)
    ctx = read_saved_context(m.read_memory, TOP, "armv7m_fp")
    assert ctx.fp and ctx.exc_return == 0xFFFF_FFED
    assert ctx.sp == end + 4                                                 # 8 バイト揃えの詰め物
    assert [ctx.registers[f"s{n}"] for n in range(32)] == s_low + s_high
    assert ctx.registers["fpscr"] == 0x0300_0000


def test_armv7m_has_no_saved_exc_return():
    m = ImageAccess()
    end = put(m, TOP, [0x44, 0x55, 0x66, 0x77, 0x88, 0x99, 0xAA, 0xBB] + hw_frame())
    ctx = read_saved_context(m.read_memory, TOP, "armv7m")
    assert ctx.exc_return == 0xFFFF_FFFD and not ctx.fp and ctx.sp == end
    assert ctx.registers["r11"] == 0xBB and ctx.registers["r0"] == 0xA0


def test_unknown_layout_is_rejected():
    with pytest.raises(ValueError):
        read_saved_context(ImageAccess().read_memory, TOP, "armv8m")


def test_unstack_exception_frame_basic():
    m = ImageAccess()
    end = put(m, TOP, hw_frame())
    regs, sp = unstack_exception_frame(m.read_memory, TOP, 0xFFFF_FFFD)
    assert sp == end and regs["pc"] == 0x0800_0100 and "s0" not in regs


def test_context_access_registers():
    m = ImageAccess([(0x2000_0000, b"\x01\x02\x03\x04")])
    regs = {"r4": 4, "sp": 0x2000_0F00, "psp": 0x2000_0F00, "pc": 0x100, "s16": 0x1111_1111, "s17": 0x2222_2222}
    access = ContextAccess(m, regs)
    assert isinstance(access, TargetAccess)
    assert access.read_memory(0x2000_0000, 4) == b"\x01\x02\x03\x04"
    assert access.read_register("R4") == 4 and access.read_register("r13") == 0x2000_0F00
    assert access.read_register("d8") == 0x2222_2222_1111_1111
    for name in ("s0", "msp", "d0", "r5"):
        with pytest.raises(AccessError):
            access.read_register(name)
