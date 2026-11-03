"""Test helper: build ARM short-descriptor page tables inside SimCores' physical memory."""
import struct


PGD = 0x4000


def _w32(cores, addr, value):
    cores.write_phys(addr, struct.pack("<I", value))


def map_page(cores, va, pa, pgd=PGD):
    """Map the 4 KiB page of `va` to `pa` (small page) in the first-level table `pgd`."""
    l2s = cores.__dict__.setdefault("_l2", {})
    key = (pgd, va >> 20)
    if key not in l2s:
        l2s[key] = 0x8000 + 0x400 * len(l2s)
        _w32(cores, pgd + 4 * (va >> 20), l2s[key] | 1)
    _w32(cores, l2s[key] + 4 * ((va >> 12) & 0xFF), (pa & 0xFFFFF000) | 2)
