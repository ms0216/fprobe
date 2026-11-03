"""ページテーブルのウォーク（物理アドレスで直接読む）。ARM 短い記述子（Pi 2B）、ARMv7 LPAE、AArch64（4 KiB）。

STATUS: 短い記述子は Pi 2B の実機で動いたウォーク。**LPAE と AArch64 は実機で未確認**（合成テーブルのテストのみ。TCR の T0SZ/T1SZ
は読んで渡す。仮定しない）。16/64 KiB グラニュール、LPA2、ステージ 2、アクセスフラグ・権限の判定は対象外（記述子は返すので呼び出し側が見る）。

テーブルは物理アドレスで読む（`read_phys(addr, n) -> bytes`）。呼び出し側がテーブルの物理アドレス（TTBR、または `mm->pgd` を線形マップで
物理に直した値）を渡す。未マップは `Unmapped`（どの段で止まったかを持つ）で、0 や None を返さない。
"""
from __future__ import annotations

import struct
from collections import namedtuple
from typing import Callable

ReadPhys = Callable[[int, int], bytes]
Mapping = namedtuple("Mapping", "pa level desc")      # level: 最後の記述子の段（短い記述子は 1 = セクション、2 = ページ）

A64_OA_MASK = 0x0000FFFFFFFFF000
A64_BLOCK_L1 = 0x0000FFFFC0000000
A64_BLOCK_L2 = 0x0000FFFFFFE00000
LPAE_OA_MASK = 0x000000FFFFFFF000
LPAE_BLOCK_L1 = 0x000000FFC0000000
LPAE_BLOCK_L2 = 0x000000FFFFE00000

AF = 1 << 10                                          # アクセスフラグ
NG = 1 << 11


class Unmapped(IOError):
    """仮想アドレスに有効な対応がない（触られたことがない、ページアウト、など）。level は止まった段。"""

    def __init__(self, va: int, level: int):
        super().__init__("0x%x is not mapped (fault at level %d)" % (va, level))
        self.va, self.level = va, level


class AddressRange(ValueError):
    """VA が、呼び出し側の言う変換領域（アドレス幅・TTBR0/TTBR1）に属さない。"""


def _rd32(read_phys: ReadPhys, pa: int) -> int:
    return struct.unpack("<I", read_phys(pa, 4))[0]


def _rd64(read_phys: ReadPhys, pa: int) -> int:
    return struct.unpack("<Q", read_phys(pa, 8))[0]


def _short(read_phys: ReadPhys, pgd: int, va: int) -> Mapping:
    if va >> 32:
        raise AddressRange("0x%x does not fit a 32-bit virtual address" % va)
    d = _rd32(read_phys, pgd + 4 * (va >> 20))
    if d & 3 == 2:                                    # セクション / スーパーセクション（拡張アドレスビットは無視）
        if d & (1 << 18):
            return Mapping((d & 0xFF000000) | (va & 0x00FFFFFF), 1, d)
        return Mapping((d & 0xFFF00000) | (va & 0x000FFFFF), 1, d)
    if d & 3 != 1:
        raise Unmapped(va, 1)
    e = _rd32(read_phys, (d & 0xFFFFFC00) + 4 * ((va >> 12) & 0xFF))
    if e & 2:                                         # スモールページ
        return Mapping((e & 0xFFFFF000) | (va & 0xFFF), 2, e)
    if e & 3 == 1:                                    # ラージページ
        return Mapping((e & 0xFFFF0000) | (va & 0xFFFF), 2, e)
    raise Unmapped(va, 2)


def a64_levels_for(va_bits: int) -> tuple[int, int]:
    """(開始段, 開始段の添字ビット数)。4 KiB グラニュール、VA が va_bits ビット。"""
    if not 16 <= va_bits <= 48:
        raise ValueError("VA size %d bits: only 16..48 are supported (4 KiB granule, no LPA2)" % va_bits)
    n = va_bits - 12
    levels = -(-n // 9)
    return 4 - levels, n - 9 * (levels - 1)


def _a64(read_phys: ReadPhys, pgd: int, va: int, tsz: int, region: str) -> Mapping:
    if region not in ("user", "kernel"):
        raise ValueError("region must be 'user' or 'kernel'")
    bits = 64 - tsz
    start, start_bits = a64_levels_for(bits)
    if va >> bits != (0 if region == "user" else (1 << tsz) - 1):
        raise AddressRange("0x%016x is not in the %s region of a %d-bit address space" % (va, region, bits))
    level, table = start, pgd
    while True:
        shift = 12 + 9 * (3 - level)
        idx = (va >> shift) & ((1 << (start_bits if level == start else 9)) - 1)
        d = _rd64(read_phys, table + 8 * idx)
        kind = d & 3
        if level == 3:
            if kind == 3:
                return Mapping((d & A64_OA_MASK) | (va & 0xFFF), 3, d)
            raise Unmapped(va, 3)
        if kind == 3:
            table = d & A64_OA_MASK
            level += 1
            continue
        if kind == 1 and level in (1, 2):             # ブロック（段 0 では不可）
            mask = A64_BLOCK_L1 if level == 1 else A64_BLOCK_L2
            return Mapping((d & mask) | (va & ((1 << shift) - 1)), level, d)
        raise Unmapped(va, level)


def _lpae(read_phys: ReadPhys, pgd: int, va: int) -> Mapping:
    if va >> 32:
        raise AddressRange("0x%x does not fit a 32-bit virtual address" % va)
    d = _rd64(read_phys, pgd + 8 * ((va >> 30) & 3))
    if d & 3 != 3:
        if d & 3 == 1:
            return Mapping((d & LPAE_BLOCK_L1) | (va & 0x3FFFFFFF), 1, d)
        raise Unmapped(va, 1)
    d = _rd64(read_phys, (d & LPAE_OA_MASK) + 8 * ((va >> 21) & 0x1FF))
    if d & 3 == 1:
        return Mapping((d & LPAE_BLOCK_L2) | (va & 0x1FFFFF), 2, d)
    if d & 3 != 3:
        raise Unmapped(va, 2)
    d = _rd64(read_phys, (d & LPAE_OA_MASK) + 8 * ((va >> 12) & 0x1FF))
    if d & 3 == 3:
        return Mapping((d & LPAE_OA_MASK) | (va & 0xFFF), 3, d)
    raise Unmapped(va, 3)


def walk(read_phys: ReadPhys, table_pa: int, va: int, *, fmt: str, tsz: int | None = None,
         region: str | None = None) -> Mapping:
    """fmt: "short" / "lpae" / "aarch64-4k"。aarch64-4k では tsz と region が必須（既定なし）。"""
    if fmt == "short":
        return _short(read_phys, table_pa, va)
    if fmt == "lpae":
        return _lpae(read_phys, table_pa, va)
    if fmt == "aarch64-4k":
        if tsz is None or region is None:
            raise ValueError("aarch64-4k needs tsz (TCR T0SZ/T1SZ) and region ('user'/'kernel')")
        return _a64(read_phys, table_pa, va, tsz, region)
    raise ValueError("unknown translation format %r" % fmt)


def translate(read_phys: ReadPhys, table_pa: int, va: int, *, fmt: str, tsz: int | None = None,
              region: str | None = None) -> int:
    return walk(read_phys, table_pa, va, fmt=fmt, tsz=tsz, region=region).pa
