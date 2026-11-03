"""fprobe.linux_halt.mmu against synthetic translation tables (physical reads). Not a hardware test.

The AArch64 and LPAE walks are NOT verified on a board; the short-descriptor walk is the one the Pi 2B runs."""
import struct

import pytest

from fprobe.linux_halt import mmu
from fprobe.linux_halt.mmu import AF, NG, AddressRange, Unmapped


class Mem:
    """Descriptors by physical address; a read of 4 or 8 bytes returns the descriptor written there (0 = fault)."""

    def __init__(self):
        self.d = {}

    def __setitem__(self, a, v):
        self.d[a] = v

    def __getitem__(self, a):
        return self.d[a]

    def __delitem__(self, a):
        del self.d[a]

    def read(self, addr, n):
        assert n in (4, 8), "the walk reads one descriptor at a time"
        return struct.pack("<I" if n == 4 else "<Q", self.d.get(addr, 0))

    def or_(self, a, v):
        self.d[a] |= v


def table_desc(addr):
    return addr | 3


def build_a64(m, va, pa, bits, base=0x100000, page_attrs=0):
    start, start_bits = mmu.a64_levels_for(bits)
    tables = {lvl: base + 0x1000 * (lvl - start) for lvl in range(start, 4)}
    for lvl in range(start, 4):
        shift = 12 + 9 * (3 - lvl)
        idx = (va >> shift) & ((1 << (start_bits if lvl == start else 9)) - 1)
        m[tables[lvl] + 8 * idx] = table_desc(tables[lvl + 1]) if lvl < 3 else (pa & mmu.A64_OA_MASK) | 3 | page_attrs
    return tables[start], tables


def a64(m, pgd, va, tsz=16, region="user"):
    return mmu.translate(m.read, pgd, va, fmt="aarch64-4k", tsz=tsz, region=region)


# --- ARM short descriptor (Pi 2B) ------------------------------------------------------------------------------

def test_short_small_page():
    m = Mem()
    pgd, l2 = 0x4000, 0x8000
    m[pgd + 4 * 0x123] = l2 | 1
    m[l2 + 4 * 0x45] = 0x30000000 | 2
    assert mmu.translate(m.read, pgd, 0x12345678, fmt="short") == 0x30000678


def test_short_large_page():
    m = Mem()
    pgd, l2 = 0x4000, 0x8000
    m[pgd + 4 * 0x123] = l2 | 1
    m[l2 + 4 * 0x45] = 0x30010000 | 1
    assert mmu.translate(m.read, pgd, 0x12345678, fmt="short") == 0x30015678


def test_short_section_and_supersection():
    m = Mem()
    pgd = 0x4000
    m[pgd + 4 * 0x123] = 0x80000000 | 2
    assert mmu.translate(m.read, pgd, 0x12345678, fmt="short") == 0x80045678
    m[pgd + 4 * 0x103] = 0xC0000000 | (1 << 18) | 2
    assert mmu.translate(m.read, pgd, 0x10345678, fmt="short") == 0xC0345678


def test_short_fault_at_each_level_raises_unmapped_with_level():
    m = Mem()
    pgd, l2 = 0x4000, 0x8000
    with pytest.raises(Unmapped) as e:
        mmu.translate(m.read, pgd, 0x12345678, fmt="short")
    assert (e.value.va, e.value.level) == (0x12345678, 1)
    m[pgd + 4 * 0x123] = l2 | 1
    with pytest.raises(Unmapped) as e:
        mmu.translate(m.read, pgd, 0x12345678, fmt="short")
    assert e.value.level == 2


def test_short_va_above_32_bits_is_an_error():
    with pytest.raises(AddressRange):
        mmu.translate(Mem().read, 0x4000, 0x1_0000_0000, fmt="short")


def test_unmapped_raises_not_zero():
    with pytest.raises(Unmapped):
        mmu.translate(Mem().read, 0x4000, 0x1000, fmt="short")


# --- AArch64 -----------------------------------------------------------------------------------------------------

def test_48_bit_page_walks_four_levels():
    m = Mem()
    va = 0x00007FFFDEADB123
    pgd, _ = build_a64(m, va, 0x80123000, 48)
    assert mmu.a64_levels_for(48) == (0, 9)
    assert a64(m, pgd, va) == 0x80123123


def test_39_and_40_bit_start_levels():
    m = Mem()
    va = 0x0000003FDEADB123
    pgd, _ = build_a64(m, va, 0x40001000, 39)
    assert mmu.a64_levels_for(39) == (1, 9)
    assert a64(m, pgd, va, tsz=25) == 0x40001123
    m2 = Mem()
    va = 0x000000FFDEADB123
    pgd, _ = build_a64(m2, va, 0x40002000, 40)
    assert mmu.a64_levels_for(40) == (0, 1)
    assert a64(m2, pgd, va, tsz=24) == 0x40002123


@pytest.mark.parametrize("tsz,start", [(16, 0), (24, 0), (25, 1), (33, 1), (34, 2), (42, 2)])
def test_start_level_follows_the_architecture_table(tsz, start):
    assert mmu.a64_levels_for(64 - tsz)[0] == start


def test_1_gib_block_ignores_low_descriptor_bits():
    m = Mem()
    l0, l1 = 0x100000, 0x101000
    m[l0] = table_desc(l1)
    m[l1 + 8] = 0xC0000000 | (1 << 25) | (1 << 14) | 1
    assert a64(m, l0, 0x40000000 + 0x678) == 0xC0000000 + 0x678


def test_2_mib_block():
    m = Mem()
    l0, l1, l2 = 0x100000, 0x101000, 0x102000
    m[l0], m[l1], m[l2 + 8 * 5] = table_desc(l1), table_desc(l2), 0x80600000 | 1
    assert a64(m, l0, 0x00A01234) == 0x80601234


def test_kernel_region_of_a_40_bit_space():
    m = Mem()
    va = 0xFFFFFF8000001234
    pgd, _ = build_a64(m, va & ((1 << 40) - 1), 0x40002000, 40)
    assert a64(m, pgd, va, tsz=24, region="kernel") == 0x40002234


def test_stray_high_and_attribute_bits_do_not_reach_the_address():
    m = Mem()
    va = 0x00007FFFDEADB123
    attrs = (1 << 54) | (1 << 53) | (3 << 6) | AF | NG
    pgd, tables = build_a64(m, va, 0x80123000, 48, page_attrs=attrs)
    m.or_(tables[3] + 8 * ((va >> 12) & 0x1FF), 1 << 50)
    m.or_(pgd + 8 * ((va >> 39) & 0x1FF), (1 << 63) | (1 << 60))
    assert a64(m, pgd, va) == 0x80123123


def test_walk_reports_level_and_descriptor():
    m = Mem()
    l0, l1 = 0x100000, 0x101000
    m[l0], m[l1 + 8] = table_desc(l1), 0xC0000000 | 1
    t = mmu.walk(m.read, l0, 0x40000000, fmt="aarch64-4k", tsz=16, region="user")
    assert (t.level, t.desc) == (1, 0xC0000001)


@pytest.mark.parametrize("lvl", range(4))
def test_a_fault_at_any_level_raises_unmapped_with_that_level(lvl):
    m = Mem()
    va = 0x00007FFFDEADB123
    pgd, tables = build_a64(m, va, 0x80123000, 48)
    del m[tables[lvl] + 8 * ((va >> (12 + 9 * (3 - lvl))) & 0x1FF)]
    with pytest.raises(Unmapped) as e:
        a64(m, pgd, va)
    assert e.value.level == lvl


def test_block_at_level_zero_and_reserved_level_three_encoding_fault():
    m = Mem()
    m[0x100000] = 0x80000000 | 1
    with pytest.raises(Unmapped):
        a64(m, 0x100000, 0x1000)
    m = Mem()
    va = 0x00007FFFDEADB123
    pgd, tables = build_a64(m, va, 0x80123000, 48)
    m[tables[3] + 8 * ((va >> 12) & 0x1FF)] = 0x80123000 | 1
    with pytest.raises(Unmapped):
        a64(m, pgd, va)


def test_va_outside_the_named_region_is_an_error_not_a_fault():
    m = Mem()
    with pytest.raises(AddressRange):
        a64(m, 0x100000, 0xFFFF000012345678)
    with pytest.raises(AddressRange):
        a64(m, 0x100000, 0x12345678, region="kernel")
    with pytest.raises(AddressRange):
        a64(m, 0x100000, 0x0001000000000000)


def test_unsupported_sizes_regions_and_formats_are_refused():
    m = Mem()
    for tsz in (15, 0, 49):
        with pytest.raises(ValueError):
            a64(m, 0x100000, 0x1000, tsz=tsz)
    with pytest.raises(ValueError):
        a64(m, 0x100000, 0x1000, region="middle")
    with pytest.raises(ValueError):
        mmu.translate(m.read, 0x100000, 0x1000, fmt="aarch64-4k")        # tsz and region are required, no default
    with pytest.raises(ValueError):
        mmu.translate(m.read, 0x100000, 0x1000, fmt="riscv")


# --- ARMv7 LPAE --------------------------------------------------------------------------------------------------

def lpae_tables():
    m = Mem()
    pgd, pmd, pte = 0x200000, 0x201000, 0x202000
    m[pgd + 8 * 3] = table_desc(pmd)
    m[pmd] = table_desc(pte)
    m[pte + 8 * 0x123] = 0x1234567000 | 3
    return m, pgd


def lpae(m, pgd, va):
    return mmu.translate(m.read, pgd, va, fmt="lpae")


def test_lpae_page_with_a_40_bit_physical_address():
    m, pgd = lpae_tables()
    assert lpae(m, pgd, 0xC0123456) == 0x1234567456


def test_lpae_2_mib_and_1_gib_blocks():
    m, pgd = lpae_tables()
    m[pgd + 8] = table_desc(0x203000)
    m[0x203000 + 8] = 0x80400000 | 1
    assert lpae(m, pgd, 0x40234567) == 0x80434567
    m[pgd + 16] = 0xC0000000 | (1 << 25) | 1
    t = mmu.walk(m.read, pgd, 0x80001234, fmt="lpae")
    assert (t.pa, t.level) == (0xC0001234, 1)


def test_lpae_va_above_32_bits_is_an_error():
    m, pgd = lpae_tables()
    with pytest.raises(AddressRange):
        lpae(m, pgd, 0x1_C0123456)


def test_lpae_faults_at_each_level():
    m, pgd = lpae_tables()
    with pytest.raises(Unmapped) as e:
        lpae(m, pgd, 0x00001000)
    assert e.value.level == 1
    m[0x201000] = 0
    with pytest.raises(Unmapped) as e:
        lpae(m, pgd, 0xC0123456)
    assert e.value.level == 2
    m[0x201000] = table_desc(0x202000)
    m[0x202000 + 8 * 0x123] = 0x1234567000 | 1
    with pytest.raises(Unmapped) as e:
        lpae(m, pgd, 0xC0123456)
    assert e.value.level == 3
