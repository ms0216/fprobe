import pytest

from fprobe.linux_halt.mmu import Unmapped
from fprobe.linux_halt.sim_cores import SimCores
from fprobe.linux_halt.usermem import UserMem
from .helpers import PGD, map_page


def setup():
    cores = SimCores(2, simultaneous=False, skew_upper_bound_ns=None)
    map_page(cores, 0x10000, 0x20000)
    map_page(cores, 0x11000, 0x30000)               # the next virtual page is NOT next to it physically
    cores.write_phys(0x20000, bytes(range(256)) * 16)
    cores.write_phys(0x30000, b"\xAA" * 4096)
    return cores, UserMem(cores, PGD, fmt="short")


def test_read_within_one_page():
    cores, mem = setup()
    assert mem.read(0x10010, 4) == bytes([0x10, 0x11, 0x12, 0x13])


def test_read_across_a_page_boundary_follows_each_pages_physical_address():
    cores, mem = setup()
    assert mem.read(0x10FFE, 4) == bytes([0xFE, 0xFF]) + b"\xAA\xAA"


def test_unmapped_read_raises_not_zeros():
    cores, mem = setup()
    with pytest.raises(Unmapped):
        mem.read(0x50000, 4)


def test_write_goes_to_the_physical_page_and_marks_code_dirty():
    cores, mem = setup()
    assert not mem.code_dirty
    mem.write(0x10020, b"\x01\x02")
    assert cores.read_phys(0x20020, 2) == b"\x01\x02"
    assert mem.code_dirty


def test_cache_is_valid_for_one_stop_only():
    cores, mem = setup()
    assert mem.read(0x10000, 1) == b"\x00"
    cores.write_phys(0x20000, b"\x77")                  # changed behind the cache (the cores ran)
    assert mem.read(0x10000, 1) == b"\x00"              # still the cached chunk
    mem.invalidate()
    assert mem.read(0x10000, 1) == b"\x77"


def test_u32_is_little_endian():
    cores, mem = setup()
    cores.write_phys(0x20040, b"\x78\x56\x34\x12")
    assert mem.u32(0x10040) == 0x12345678
