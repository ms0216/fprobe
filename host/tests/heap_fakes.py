"""試験用: ヒープの偽のメモリを組み立てる（FreeRTOS heap_4 / heap_5、newlib-nano、newlib の dlmalloc）。

それぞれのアロケータのソースと同じ形にブロックを並べる（heap_4.c・heap_5.c・nano-mallocr.c・mallocr.c）。
"""

from __future__ import annotations

RAM = 0x2000_0000
RAM_SIZE = 0x10000
ALLOCATED = 1 << 31


class FakeMemory:
    def __init__(self, base: int = RAM, size: int = RAM_SIZE):
        self.base, self.data = base, bytearray(size)

    def read(self, address: int, length: int) -> bytes:
        off = address - self.base
        if off < 0 or off + length > len(self.data):
            raise ValueError(f"0x{address:08X} は RAM の外")
        return bytes(self.data[off:off + length])

    def u32(self, address: int, value: int) -> None:
        off = address - self.base
        self.data[off:off + 4] = (value & 0xFFFF_FFFF).to_bytes(4, "little")

    def get(self, address: int) -> int:
        return int.from_bytes(self.read(address, 4), "little")


class FakeSymbols:
    def __init__(self):
        self.table: dict[str, tuple[int, int]] = {}

    def add(self, name: str, address: int, size: int = 4) -> int:
        self.table[name] = (address, size)
        return address

    def address(self, name: str) -> int | None:
        return self.table[name][0] if name in self.table else None

    def size(self, name: str) -> int | None:
        return self.table[name][1] if name in self.table else None


class FreeRTOSHeap:
    """heap_4（ucHeap あり）。blocks: [(大きさ, 使用中)] を先頭から並べ、空きは番地順のリストに繋ぐ。"""

    def __init__(self, blocks: list[tuple[int, bool]], heap_size: int = 0x2000, canary: int | None = None,
                 misalign: int = 4):
        self.mem, self.syms = FakeMemory(), FakeSymbols()
        s = self.syms
        self.start_var = s.add("xStart", RAM + 0x100, 8)
        self.end_var = s.add("pxEnd", RAM + 0x108)
        self.free_var = s.add("xFreeBytesRemaining", RAM + 0x10C)
        self.min_var = s.add("xMinimumEverFreeBytesRemaining", RAM + 0x110)
        self.allocs_var = s.add("xNumberOfSuccessfulAllocations", RAM + 0x114)
        self.frees_var = s.add("xNumberOfSuccessfulFrees", RAM + 0x118)
        self.canary = canary
        if canary is not None:
            self.mem.u32(s.add("xHeapCanary", RAM + 0x11C), canary)
        self.heap = s.add("ucHeap", RAM + 0x200 + misalign, heap_size)
        start = (self.heap + 7) & ~7
        end = (self.heap + heap_size - 8) & ~7
        self.start, self.end = start, end
        self.layout(blocks)

    def protect(self, p: int) -> int:
        return p ^ self.canary if self.canary is not None else p

    def layout(self, blocks: list[tuple[int, bool]]) -> None:
        mem, a = self.mem, self.start
        self.addresses = []
        free = []
        for size, used in blocks:
            self.addresses.append(a)
            mem.u32(a + 4, size | (ALLOCATED if used else 0))
            mem.u32(a, 0)
            if not used:
                free.append(a)
            a += size
        if a != self.end:                                       # 残りは空き
            self.addresses.append(a)
            mem.u32(a + 4, self.end - a)
            free.append(a)
        mem.u32(self.end, self.protect(0))
        mem.u32(self.end + 4, 0)
        previous = self.start_var
        for f in free:
            mem.u32(previous, self.protect(f))
            previous = f
        mem.u32(previous, self.protect(self.end))
        mem.u32(self.end_var, self.end)
        total_free = sum(self.mem.get(f + 4) for f in free)
        used = sum(1 for _, u in blocks if u)
        mem.u32(self.free_var, total_free)
        mem.u32(self.min_var, total_free - 64)
        mem.u32(self.allocs_var, used + 3)
        mem.u32(self.frees_var, 3)


class NanoHeap:
    """newlib-nano: __malloc_sbrk_start から大きさの語 + 本体のかたまりが並び、空きは番地順の __malloc_free_list。"""

    def __init__(self, chunks: list[tuple[int, bool, int]], end_symbol: str | None = "__sbrk_heap_end"):
        """chunks: [(大きさ, 使用中, 揃えの詰め物（0 か 4）)]。"""
        self.mem, self.syms = FakeMemory(), FakeSymbols()
        s, mem = self.syms, self.mem
        self.list_var = s.add("__malloc_free_list", RAM + 0x100)
        self.start_var = s.add("__malloc_sbrk_start", RAM + 0x104)
        self.base = RAM + 0x400
        mem.u32(self.start_var, self.base)
        a, free, self.pointers = self.base, [], []
        for size, used, pad in chunks:
            mem.u32(a, size)
            if pad:
                mem.u32(a + pad, -pad)                           # 詰め物があれば ptr − 4 に負の位置
            if used:
                self.pointers.append(a + 4 + pad)
            else:
                free.append(a)
            a += size
        self.top = a
        if end_symbol:
            mem.u32(s.add(end_symbol, RAM + 0x108), a)
        previous = self.list_var
        for f in free:
            mem.u32(previous if previous == self.list_var else previous + 4, f)
            previous = f
        mem.u32(previous if previous == self.list_var else previous + 4, 0)


class DlHeap:
    """newlib の dlmalloc（mallocr.c）: かたまり = prev_size, size（bit 0 = 前が使用中）, fd, bk。最後が top。"""

    def __init__(self, chunks: list[tuple[int, bool]], top_size: int = 0x200):
        self.mem, self.syms = FakeMemory(), FakeSymbols()
        s, mem = self.syms, self.mem
        self.av = s.add("__malloc_av_", RAM + 0x100, 8 * 128 + 8)
        self.base_var = s.add("__malloc_sbrk_base", RAM + 0x900)
        info = s.add("__malloc_current_mallinfo", RAM + 0x904, 40)
        self.base = RAM + 0x1004
        mem.u32(self.base_var, self.base)
        a = (self.base + 7) & ~7
        self.first = a
        self.addresses, previous_used = [], True
        frees = []
        for size, used in chunks:
            self.addresses.append(a)
            mem.u32(a + 4, size | int(previous_used))
            if not used:
                frees.append(a)
                mem.u32(a + size, size)                           # 次の prev_size = 足（空きのとき）
            previous_used = used
            a += size
        self.top = a
        mem.u32(a + 4, top_size | int(previous_used))
        mem.u32(self.av + 8, a)                                   # top = av_[2]
        mem.u32(info, a + top_size - self.base)
        # 空きは 1 つの箱（av_ の bin 1 の頭）に双方向に繋ぐ
        head = self.av + 8 * 1 + 8 - 8                            # bin_at(1) = &av_[4] − 8
        ring = [head] + frees + [head]
        for x, y in zip(ring, ring[1:]):
            mem.u32(x + 8, y)                                     # fd
            mem.u32(y + 12, x)                                    # bk
