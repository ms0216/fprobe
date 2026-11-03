"""プロセスの仮想メモリを、ページテーブルのウォークと物理メモリの読み書きで読み書きする。

読み書きは全コアを止めているときだけ。キャッシュ（ページの中身・変換）は 1 回の停止の間だけ有効で、走らせる前に `invalidate()` で
捨てる。書き込みは `code_dirty` を立て、エンジンが走らせる前に `CoreAccess.sync_caches()` を呼ぶ。
"""
from __future__ import annotations

import struct

from .core_access import CoreAccess
from .mmu import Unmapped, translate


class UserMem:
    PAGE = 4096
    CHUNK = 256          # 読みの粒度: 1 回の読みは固定費が大きい（数 ms〜十数 ms）ので、256 バイト単位で持つ

    def __init__(self, cores: CoreAccess, table_pa: int, *, fmt: str, tsz: int | None = None,
                 region: str | None = None):
        self.c, self.table_pa, self.fmt, self.tsz, self.region = cores, table_pa, fmt, tsz, region
        self.chunks: dict[int, bytes] = {}
        self.pas: dict[int, int] = {}
        self.code_dirty = False

    def invalidate(self) -> None:
        self.chunks.clear()
        self.pas.clear()

    def pa(self, va: int) -> int:
        """va の物理アドレス。未マップは Unmapped（0 を返さない）。"""
        pg = va & ~0xFFF
        if pg not in self.pas:
            self.pas[pg] = translate(self.c.read_phys, self.table_pa, pg, fmt=self.fmt, tsz=self.tsz,
                                     region=self.region)
        return self.pas[pg] | (va & 0xFFF)

    def _within_page(self, va: int, k: int) -> bytes:
        c = self.CHUNK
        first, last = va & ~(c - 1), (va + k - 1) & ~(c - 1)
        missing = [a for a in range(first, last + 1, c) if a not in self.chunks]
        if missing:
            lo, hi = missing[0], missing[-1] + c
            data = self.c.read_phys(self.pa(lo), hi - lo)
            for a in range(lo, hi, c):
                self.chunks[a] = data[a - lo:a - lo + c]
        blob = b"".join(self.chunks[a] for a in range(first, last + 1, c))
        return blob[va - first:va - first + k]

    def read(self, va: int, n: int) -> bytes:
        out = bytearray()
        while n > 0:
            k = min(n, self.PAGE - (va & 0xFFF))
            out += self._within_page(va, k)
            va, n = va + k, n - k
        return bytes(out)

    def write(self, va: int, data: bytes) -> None:
        while data:
            k = min(len(data), self.PAGE - (va & 0xFFF))
            self.c.write_phys(self.pa(va), data[:k])
            for a in range(va & ~(self.CHUNK - 1), va + k, self.CHUNK):
                self.chunks.pop(a, None)
            self.code_dirty = True
            va, data = va + k, data[k:]

    def u32(self, va: int) -> int:
        return struct.unpack("<I", self.read(va, 4))[0]


__all__ = ["UserMem", "Unmapped"]
