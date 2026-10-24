"""ヒープの解析（U27）: アロケータの管理の形を辿る走査（使用量・断片化・壊れの検出）と、malloc / free の記録からの
呼び出し元ごとの生きている確保（リークかは判定しない）。SystemView のヒープ監視・Tracealyzer の Heap Usage・CSIDE のヒープのあふれ検出の相当。

走査（`scan`）: ターゲットのメモリと ELF のシンボル（番地・大きさ。DWARF は要らない: newlib は -g 無しが普通）から。
- FreeRTOS heap_4 / heap_5（portable/MemMang）: ブロック = {pxNextFreeBlock, xBlockSize}（使用中は最上位ビット、
  次は NULL）。領域を先頭から物理的に辿り、番地順の空きのリスト（xStart → pxEnd。heap_5 は領域の終わりの大きさ 0 の
  印をはさむ）と突き合わせる。configENABLE_HEAP_PROTECTOR のポインタは xHeapCanary で戻す。heap_5 の領域は
  引数 regions か、使う側が名前を渡すアプリの表（regions_symbol。vPortDefineHeapRegions に渡した HeapRegion_t の配列。
  名前はアプリが決めるので慣習の名前を探さない）から（無ければ空きのリストだけ）。カウンタ（残量・最小残量・回数）は
  rtos/freertos_objects（U08）と同じ変数（FreeRTOS の heap_4.c / heap_5.c が定める名前）。
- newlib-nano（nano-mallocr.c）: かたまり = {long size, 本体}、本体は 8 に揃え、詰め物があれば ptr − 4 に負の位置。
  __malloc_sbrk_start から program break まで並び、空きは番地順の __malloc_free_list（次は本体の位置）。
  break は引数 end か、使う側が名前を渡す sbrk の実装の変数（break_symbol。sbrk はアプリ・BSP が書くので慣習の名前を
  探さない）。どちらも無ければ形の崩れた所で止まり、complete = false（そこが終わりか壊れかは決めない）。
- newlib（mallocr.c、dlmalloc 2.6）: かたまり = {prev_size, size（bit 0 = 前が使用中）, fd, bk}。
  __malloc_sbrk_base から top（__malloc_av_[2]）まで。使用中かは次のかたまりの PREV_INUSE。空きは双方向の箱。

壊れの検出: 大きさが不正・領域の外（previous_used = 直前の使用中のブロック。壊した者は推定しない）、空きのリストの輪・
順序・外、空きなのにリストに無い・リストにあるのに使用中、繋がれていない隣の空き、使用中の印の不整合、
dlmalloc の fd / bk・足、カウンタ（残量・回数）との不一致。

記録（`AllocationTracker`）: alloc / free の出来事（どこから呼ばれたか付き）から、生きている確保・最大・二重の解放・
失敗・呼び出し元ごとの生きている確保（リークかは判定しない）。出来事の取り方は probe_heap（ブレークポイント）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Protocol

from .rtos.freertos_objects import HEAP_ALLOCATED_BIT, MAX_FREE_BLOCKS, PORT_BYTE_ALIGNMENT
from .rtos.structs import Memory

__all__ = ["ALLOCATORS", "AllocationTracker", "Block", "HeapError", "HeapScan", "HeapSymbols", "SymbolTable",
           "detect", "scan"]

ALLOCATORS = ("freertos", "newlib_nano", "newlib")
POINTERS = 8                             # 呼び出し元ごとに返す生きている番地の数（表示の量。切ったら pointers_truncated）
MAX_BLOCKS = 1 << 16                     # 物理的に辿るブロックの上限（壊れた大きさで止まらないとき）
BLOCK_HEADER = 8                         # heap_4/5 の xHeapStructSize（32 ビット・8 揃え）
MIN_BLOCK = 2 * BLOCK_HEADER             # heapMINIMUM_BLOCK_SIZE
NANO_CHUNK_OFFSET, NANO_ALIGN, NANO_MIN_CHUNK = 4, 8, 16
DL_SIZE_SZ, DL_ALIGN, DL_MIN_CHUNK, DL_PREV_INUSE, DL_FLAGS = 4, 8, 16, 1, 3
MAX_REGIONS = 16
ISSUES_LIMIT = 200                       # 記録の誤りを持つ数の既定（AllocationTracker の issues_limit。総数は数える）
SERIES_POINTS = 512                      # 使用量の推移を返す点の数の既定（report の series_points）


class HeapError(RuntimeError):
    """その種類のアロケータが ELF に無い。"""


class HeapSymbols(Protocol):
    def address(self, name: str) -> int | None: ...
    def size(self, name: str) -> int | None: ...


class SymbolTable:
    """fprobe.symbols.Symbols（ELF のシンボル表）を HeapSymbols に。static の変数（heap_4.c の xStart 等）も引ける。"""

    def __init__(self, symbols):
        self.symbols = symbols

    def _get(self, name: str):
        try:
            return self.symbols.symbol(name)
        except KeyError:
            return None

    def address(self, name: str) -> int | None:
        s = self._get(name)
        return None if s is None else s.address

    def size(self, name: str) -> int | None:
        s = self._get(name)
        return None if s is None else s.size


@dataclass
class Block:
    address: int                         # 管理の頭の番地
    size: int                            # 頭を含む大きさ
    used: bool
    pointer: int | None = None           # アプリに渡した番地（使用中）

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class HeapScan:
    allocator: str
    scheme: str
    regions: list[tuple[int, int]] = field(default_factory=list)
    blocks: list[Block] = field(default_factory=list)
    issues: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    counters: dict = field(default_factory=dict)
    complete: bool = True                # 領域を最後まで辿れた（壊れ・上限で止まっていない）

    def issue(self, kind: str, address: int | None, detail: str, **extra) -> None:
        self.issues.append({"kind": kind, "address": address, "detail": detail, **extra})

    def to_dict(self, blocks: int = 0) -> dict:
        """要約（blocks > 0 ならブロックの一覧も先頭からその数まで）。"""
        used = [b for b in self.blocks if b.used]
        free = [b for b in self.blocks if not b.used]
        free_bytes = sum(b.size for b in free)
        largest = max((b.size for b in free), default=0)
        histogram: dict[str, int] = {}
        for b in used:
            bucket = 1 << max(4, (b.size - 1).bit_length())
            histogram[f"<={bucket}"] = histogram.get(f"<={bucket}", 0) + 1
        out = {"allocator": self.allocator, "scheme": self.scheme, "regions": [list(r) for r in self.regions],
               "total": sum(b.size for b in self.blocks), "used": sum(b.size for b in used), "free": free_bytes,
               "used_blocks": len(used), "free_blocks": len(free), "largest_free": largest,
               "fragmentation": 0.0 if not free_bytes else 1 - largest / free_bytes,
               "overhead": len(used) * self._header(), "histogram": dict(sorted(histogram.items(),
                                                                          key=lambda kv: int(kv[0][2:]))),
               "counters": dict(self.counters), "complete": self.complete, "issues": list(self.issues),
               "notes": list(self.notes)}
        if blocks:
            out["blocks"] = [b.to_dict() for b in self.blocks[:blocks]]
            out["blocks_truncated"] = len(self.blocks) > blocks
        return out

    def _header(self) -> int:
        return {"freertos": BLOCK_HEADER, "newlib_nano": NANO_CHUNK_OFFSET, "newlib": DL_SIZE_SZ}.get(self.allocator, 0)


def _align_up(x: int, a: int) -> int:
    return (x + a - 1) & ~(a - 1)


def _align_down(x: int, a: int) -> int:
    return x & ~(a - 1)


def _signed(x: int) -> int:
    return x - (1 << 32) if x & 0x8000_0000 else x


def detect(symbols: HeapSymbols) -> list[str]:
    """ELF にあるアロケータ（FreeRTOS の heap_4/5、newlib-nano、newlib）。"""
    out = []
    if all(symbols.address(n) is not None for n in ("xStart", "pxEnd", "xMinimumEverFreeBytesRemaining")):
        out.append("freertos")
    if symbols.address("__malloc_free_list") is not None and symbols.address("__malloc_sbrk_start") is not None:
        out.append("newlib_nano")
    if symbols.address("__malloc_av_") is not None and symbols.address("__malloc_sbrk_base") is not None:
        out.append("newlib")
    return out


def scan(read_memory: Callable[[int, int], bytes], symbols: HeapSymbols, allocator: str,
         regions: list[tuple[int, int]] | None = None, end: int | None = None, regions_symbol: str | None = None,
         break_symbol: str | None = None) -> HeapScan:
    """ヒープを走査する。allocator: freertos / newlib_nano / newlib。regions: heap_5 の領域 [(先頭, 大きさ)]、
    regions_symbol: heap_5 の領域の表（HeapRegion_t の配列）の変数の名前、end: newlib-nano の program break、
    break_symbol: program break を持つ変数の名前（sbrk の実装の変数）。名前は使う側が明示する（慣習の名前を探さない）。"""
    mem = Memory(read_memory)
    if allocator == "freertos":
        return _FreeRTOS(mem, symbols).scan(regions, regions_symbol)
    if allocator == "newlib_nano":
        if end is None and break_symbol is not None:
            address = symbols.address(break_symbol)
            if address is None:
                raise ValueError(f"break_symbol {break_symbol!r} が ELF に無い")
            end = mem.u32(address)
            if not end:
                raise ValueError(f"break_symbol {break_symbol!r} が 0（sbrk がまだ呼ばれていない）")
        return _scan_nano(mem, symbols, end)
    if allocator == "newlib":
        return _scan_dlmalloc(mem, symbols)
    raise ValueError(f"allocator は {' / '.join(ALLOCATORS)}")


# --- FreeRTOS heap_4 / heap_5 ---------------------------------------------------------------------

class _FreeRTOS:
    def __init__(self, mem: Memory, symbols: HeapSymbols):
        self.mem, self.sym = mem, symbols
        for name in ("xStart", "pxEnd", "xFreeBytesRemaining", "xMinimumEverFreeBytesRemaining"):
            if symbols.address(name) is None:
                raise HeapError(f"FreeRTOS の heap_4 / heap_5 が ELF に無い（{name} が無い）")
        canary = symbols.address("xHeapCanary")
        self.canary = mem.u32(canary) if canary is not None else 0

    def unprotect(self, p: int) -> int:
        return p ^ self.canary

    def scan(self, regions: list[tuple[int, int]] | None, regions_symbol: str | None = None) -> HeapScan:
        sym, mem = self.sym, self.mem
        heap = sym.address("ucHeap")
        out = HeapScan("freertos", "heap_4" if heap is not None else "heap_5")
        out.counters = {"free_bytes": mem.u32(sym.address("xFreeBytesRemaining")),
                        "min_free_bytes": mem.u32(sym.address("xMinimumEverFreeBytesRemaining"))}
        for name, key in (("xNumberOfSuccessfulAllocations", "allocations"), ("xNumberOfSuccessfulFrees", "frees")):
            if sym.address(name) is not None:
                out.counters[key] = mem.u32(sym.address(name))
        if self.canary:
            out.notes.append("configENABLE_HEAP_PROTECTOR: ポインタを xHeapCanary で戻して辿った")
        end = mem.u32(sym.address("pxEnd"))
        if not end:
            out.notes.append("ヒープはまだ初期化されていない（最初の pvPortMalloc の前）")
            return out
        out.regions = self._regions(out, heap, end, regions, regions_symbol)
        listed = self._free_list(out, end)
        if not out.regions:
            out.complete = False
            out.blocks = [Block(a, s, False) for a, s in listed if s]
            out.notes.append("heap_5 の領域を渡していない（regions か、領域の表の変数の名前 regions_symbol）: 空きの"
                             "リストだけ")
            return out
        for start, stop in out.regions:
            self._walk(out, start, stop)
        self._cross_check(out, listed)
        return out

    def _regions(self, out: HeapScan, heap: int | None, end: int, given: list[tuple[int, int]] | None,
                 table_symbol: str | None) -> list[tuple[int, int]]:
        """[(最初のブロック, 終わりの印)]。heap_4 は ucHeap と pxEnd、heap_5 は引数か、使う側が名前を渡した領域の表。"""
        if heap is not None:
            return [(_align_up(heap, PORT_BYTE_ALIGNMENT), end)]
        pairs = list(given or [])
        if not pairs and table_symbol is not None:
            table = self.sym.address(table_symbol)
            if table is None:
                raise ValueError(f"regions_symbol {table_symbol!r} が ELF に無い")
            for i in range(MAX_REGIONS):
                start, size = self.mem.u32(table + 8 * i), self.mem.u32(table + 8 * i + 4)
                if not size:
                    break
                pairs.append((start, size))
            else:                                               # 終わりの印（大きさ 0）が無い: 切ったことを黙らない
                out.complete = False
                out.notes.append(f"{table_symbol} の先頭 {MAX_REGIONS} 個に終わりの印（大きさ 0）が無い: その先は読んで"
                                 "いない（regions で渡せる）")
        out_regions = []
        for start, size in pairs:
            first = _align_up(start, PORT_BYTE_ALIGNMENT)
            stop = _align_down(first + size - (first - start) - BLOCK_HEADER, PORT_BYTE_ALIGNMENT)
            out_regions.append((first, stop))
        return out_regions

    def _free_list(self, out: HeapScan, end: int) -> list[tuple[int, int]]:
        """空きのリスト [(番地, 大きさ)]（heap_5 の領域の終わりの印は大きさ 0）。"""
        mem, items, seen = self.mem, [], set()
        block = self.unprotect(mem.u32(self.sym.address("xStart")))
        previous = 0
        while block != end:
            if block in seen:
                out.issue("free_list_loop", block, "空きのリストが輪になっている")
                break
            if len(items) >= MAX_FREE_BLOCKS:
                _free_list_limit(out)
                break
            if not block or not self._inside(out, block):
                out.issue("free_list_outside", block, "空きのリストがヒープの外を指す（ポインタが壊れた）")
                break
            if block <= previous:
                out.issue("free_list_unsorted", block, "空きのリストが番地順でない")
            seen.add(block)
            raw = mem.u32(block + 4)
            if raw & HEAP_ALLOCATED_BIT:
                out.issue("listed_not_free", block, "空きのリストにあるのに使用中の印")
            items.append((block, raw & ~HEAP_ALLOCATED_BIT))
            previous = block
            block = self.unprotect(mem.u32(block))
        return items

    @staticmethod
    def _inside(out: HeapScan, address: int) -> bool:
        return not out.regions or any(a <= address <= b for a, b in out.regions)

    def _walk(self, out: HeapScan, start: int, stop: int) -> None:
        mem, a = self.mem, start
        last_used: Block | None = None
        while a < stop:
            if len(out.blocks) >= MAX_BLOCKS:
                out.complete = False
                out.notes.append(f"ブロックが {MAX_BLOCKS} 個を超えたので止めた")
                return
            raw = mem.u32(a + 4)
            used, size = bool(raw & HEAP_ALLOCATED_BIT), raw & ~HEAP_ALLOCATED_BIT
            if size < MIN_BLOCK or size % PORT_BYTE_ALIGNMENT or a + size > stop:
                _bad_size(out, a, raw, last_used)
                return
            block = Block(a, size, used, a + BLOCK_HEADER if used else None)
            if used and mem.u32(a):
                out.issue("bad_link", a, "使用中のブロックの次のポインタが NULL でない（heap_4 の使用中のブロックは NULL）",
                          previous_used=last_used.pointer if last_used else None)
            out.blocks.append(block)
            last_used = block if used else last_used
            a += size
        if mem.u32(stop + 4) & ~HEAP_ALLOCATED_BIT:
            out.issue("bad_end", stop, "領域の終わりの印の大きさが 0 でない",
                      previous_used=last_used.pointer if last_used else None)

    def _cross_check(self, out: HeapScan, listed: list[tuple[int, int]]) -> None:
        listed_set = {a for a, s in listed if s}
        free_blocks = [b for b in out.blocks if not b.used]
        for b in free_blocks:
            if b.address not in listed_set:
                out.issue("free_not_listed", b.address, "空きのブロックが空きのリストに無い（失われた空き）")
        physical = {b.address for b in out.blocks}
        for a in listed_set - physical:
            if out.complete:
                out.issue("listed_not_free", a, "空きのリストの項目がブロックの境に無い")
        for x, y in zip(out.blocks, out.blocks[1:]):
            if not x.used and not y.used and x.address + x.size == y.address:
                out.issue("unmerged_free", y.address, "隣り合う空きが繋がれていない（heap_4/5 は解放のときに繋ぐ）")
        if out.complete:
            free = sum(b.size for b in free_blocks)
            if free != out.counters["free_bytes"]:
                out.issue("counter_mismatch", None,
                          f"空きの合計 {free} が xFreeBytesRemaining {out.counters['free_bytes']} と合わない")
            if "allocations" in out.counters and "frees" in out.counters:
                live = out.counters["allocations"] - out.counters["frees"]
                used = sum(1 for b in out.blocks if b.used)
                if live != used:
                    out.issue("counter_mismatch", None, f"確保 − 解放の回数 {live} が使用中のブロック {used} と合わない")


def _bad_size(out: HeapScan, address: int, raw: int, last_used: Block | None) -> None:
    out.complete = False
    detail = f"ブロックの大きさ 0x{raw:08X} が不正（ここから先は辿れない）"
    if last_used is not None:
        detail += f"。直前の使用中のブロックは 0x{last_used.pointer:08X}"
    out.issue("bad_size", address, detail, previous_used=last_used.pointer if last_used else None)


# --- newlib-nano -------------------------------------------------------------------------------------

def _scan_nano(mem: Memory, sym: HeapSymbols, end: int | None) -> HeapScan:
    lst, start_var = sym.address("__malloc_free_list"), sym.address("__malloc_sbrk_start")
    if lst is None or start_var is None:
        raise HeapError("newlib-nano の malloc が ELF に無い（__malloc_free_list / __malloc_sbrk_start）")
    out = HeapScan("newlib_nano", "nano-mallocr")
    start = mem.u32(start_var)
    if not start:
        out.notes.append("まだ malloc が呼ばれていない（__malloc_sbrk_start が 0）")
        return out
    start = _align_up(start, 4)
    if end is not None:
        out.counters["break"] = end
    # 空きのリスト
    listed, seen, previous = [], set(), 0
    node = mem.u32(lst)
    while node:
        if node in seen:
            out.issue("free_list_loop", node, "空きのリストが輪になっている")
            break
        if len(listed) >= MAX_FREE_BLOCKS:
            _free_list_limit(out)
            break
        if node < start or (end is not None and node >= end):
            out.issue("free_list_outside", node, "空きのリストがヒープの外を指す（ポインタが壊れた）")
            break
        if node <= previous:
            out.issue("free_list_unsorted", node, "空きのリストが番地順でない")
        seen.add(node)
        listed.append(node)
        previous = node
        node = mem.u32(node + NANO_CHUNK_OFFSET)
    free_set = set(listed)
    out.regions = [(start, end if end is not None else start)]
    a, last_used = start, None
    limit = end if end is not None else 1 << 32
    while a < limit:
        if len(out.blocks) >= MAX_BLOCKS:
            out.complete = False
            out.notes.append(f"かたまりが {MAX_BLOCKS} 個を超えたので止めた")
            break
        try:
            size = _signed(mem.u32(a))
        except Exception as e:                             # 読めない（RAM の外）: 終わりが分からないときの止まり
            if end is None:
                _stopped_without_end(out, a, f"読めない（{type(e).__name__}）")
            break
        if size < NANO_MIN_CHUNK or size % 4 or a + size > limit:
            if end is None:                                # 終わりを渡されていない: 終わりか壊れかは決めない
                _stopped_without_end(out, a, f"大きさの語 0x{size & 0xFFFF_FFFF:08X} がかたまりにならない")
                break
            _bad_size(out, a, size & 0xFFFF_FFFF, last_used)
            break
        used = a not in free_set
        pointer = _align_up(a + NANO_CHUNK_OFFSET, NANO_ALIGN) if used else None
        if used and pointer != a + NANO_CHUNK_OFFSET:
            pad = pointer - (a + NANO_CHUNK_OFFSET)
            if _signed(mem.u32(a + pad)) != -pad:
                out.issue("bad_padding", a, "揃えの詰め物の印（ptr − 4 の負の位置）が壊れている（free が誤る）",
                          previous_used=last_used.pointer if last_used else None)
        block = Block(a, size, used, pointer)
        out.blocks.append(block)
        last_used = block if used else last_used
        a += size
    if end is None:
        out.regions = [(start, a)]
    for node in free_set - {b.address for b in out.blocks}:
        if out.complete:
            out.issue("listed_not_free", node, "空きのリストの項目がかたまりの境に無い")
    for x, y in zip(out.blocks, out.blocks[1:]):
        if not x.used and not y.used and x.address + x.size == y.address:
            out.issue("unmerged_free", y.address, "隣り合う空きが繋がれていない（nano の free は繋ぐ）")
    return out


def _free_list_limit(out: HeapScan) -> None:
    """空きのリストを辿る数の上限に着いた（壊れとは言わない。輪・外は別に見つける）。"""
    out.complete = False
    out.notes.append(f"空きのリストが {MAX_FREE_BLOCKS} 個を超えたので、その先は辿っていない")


def _stopped_without_end(out: HeapScan, address: int, why: str) -> None:
    out.complete = False
    out.notes.append(f"ヒープの終わり（program break）を渡していない（end か break_symbol）: 0x{address:08X} で{why}"
                     "ので止めた（そこが終わりか、壊れたかは決めない）")


# --- newlib（dlmalloc） ------------------------------------------------------------------------------

def _scan_dlmalloc(mem: Memory, sym: HeapSymbols) -> HeapScan:
    av, base_var = sym.address("__malloc_av_"), sym.address("__malloc_sbrk_base")
    if av is None or base_var is None:
        raise HeapError("newlib の malloc（dlmalloc）が ELF に無い（__malloc_av_ / __malloc_sbrk_base）")
    out = HeapScan("newlib", "dlmalloc")
    info = sym.address("__malloc_current_mallinfo")
    if info is not None:
        out.counters["arena"] = mem.u32(info)                 # sbrk で得た量（mallinfo.arena）
    for name, key in (("__malloc_max_sbrked_mem", "max_sbrked"), ("__malloc_max_total_mem", "max_total")):
        if sym.address(name) is not None:
            out.counters[key] = mem.u32(sym.address(name))
    base = mem.u32(base_var)
    if base in (0, 0xFFFF_FFFF):
        out.notes.append("まだ malloc が呼ばれていない（__malloc_sbrk_base が未設定）")
        return out
    top = mem.u32(av + 2 * DL_SIZE_SZ)                         # top = bin_at(0)->fd = av_[2]
    a = _align_up(base, DL_ALIGN)
    top_size = mem.u32(top + DL_SIZE_SZ) & ~DL_FLAGS
    out.regions = [(a, top + top_size)]
    last_used: Block | None = None
    while a < top:
        if len(out.blocks) >= MAX_BLOCKS:
            out.complete = False
            out.notes.append(f"かたまりが {MAX_BLOCKS} 個を超えたので止めた")
            break
        size = mem.u32(a + DL_SIZE_SZ) & ~DL_FLAGS
        if size == DL_SIZE_SZ:
            out.complete = False
            out.notes.append(f"0x{a:08X} にフェンスポスト（ほかの sbrk の呼び出しで途切れた）: ここで止めた")
            break
        if size < DL_MIN_CHUNK or size % DL_ALIGN or a + size > top:
            _bad_size(out, a, mem.u32(a + DL_SIZE_SZ), last_used)
            break
        used = bool(mem.u32(a + size + DL_SIZE_SZ) & DL_PREV_INUSE)
        block = Block(a, size, used, a + 2 * DL_SIZE_SZ if used else None)
        if not used:
            fd, bk = mem.u32(a + 8), mem.u32(a + 12)
            try:
                links_ok = mem.u32(fd + 12) == a and mem.u32(bk + 8) == a
            except Exception:                                  # 読めない番地を指す
                links_ok = False
            if not links_ok:
                out.issue("bad_links", a, "空きのかたまりの fd / bk が壊れている（双方向のリストが合わない）",
                          previous_used=last_used.pointer if last_used else None)
            if mem.u32(a + size) != size:
                out.issue("bad_footer", a, "空きのかたまりの足（次の prev_size）が大きさと合わない",
                          previous_used=last_used.pointer if last_used else None)
        out.blocks.append(block)
        last_used = block if used else last_used
        a += size
    if a == top:
        out.blocks.append(Block(top, top_size, False))
    elif out.complete:
        out.complete = False
    for x, y in zip(out.blocks, out.blocks[1:]):
        if not x.used and not y.used:
            out.issue("unmerged_free", y.address, "隣り合う空きが繋がれていない（dlmalloc は解放のときに繋ぐ）")
    return out


# --- malloc / free の記録 -------------------------------------------------------------------------

@dataclass
class _Live:
    t: float
    size: int
    caller: str


class AllocationTracker:
    """alloc / free の出来事（時刻は秒）から、生きている確保・最大・誤り・呼び出し元ごとの生きている確保を出す。
    誤りは issues_limit 件まで持つ（総数は issues_total に数える）。"""

    def __init__(self, issues_limit: int = ISSUES_LIMIT):
        if issues_limit < 0:
            raise ValueError("issues_limit は 0 以上")
        self.issues_limit = issues_limit
        self.issues_total = 0
        self.live: dict[int, _Live] = {}
        self.freed: set[int] = set()
        self.allocations = self.frees = self.untracked_frees = 0
        self.bytes = self.peak = 0
        self.issues: list[dict] = []
        self.series: list[tuple[float, int]] = []
        self.history: dict[str, list[tuple[float, int]]] = {}      # 呼び出し元 → (時刻, 生きている数)
        self._per_caller: dict[str, int] = {}
        self.first: float | None = None
        self.last: float | None = None

    def _issue(self, kind: str, t: float, pointer: int, caller: str, detail: str) -> None:
        self.issues_total += 1
        if len(self.issues) < self.issues_limit:
            self.issues.append({"kind": kind, "t": t, "pointer": pointer, "caller": caller, "detail": detail})

    def _count(self, caller: str, t: float, delta: int) -> None:
        n = self._per_caller.get(caller, 0) + delta
        self._per_caller[caller] = n
        self.history.setdefault(caller, []).append((t, n))

    def _seen(self, t: float) -> None:
        self.first = t if self.first is None else min(self.first, t)
        self.last = t if self.last is None else max(self.last, t)

    def alloc(self, t: float, pointer: int, size: int, caller: str = "?") -> None:
        self._seen(t)
        self.allocations += 1
        if not pointer:
            self._issue("failed_allocation", t, 0, caller, f"{size} バイトの確保に失敗（NULL）")
            return
        old = self.live.pop(pointer, None)
        if old is not None:
            self._issue("reused_live_pointer", t, pointer, caller,
                        f"解放されていない番地がまた返った（{old.caller} の確保。その間の free は記録に無い）")
            self.bytes -= old.size
            self._count(old.caller, t, -1)
        self.freed.discard(pointer)
        self.live[pointer] = _Live(t, size, caller)
        self.bytes += size
        self.peak = max(self.peak, self.bytes)
        self._count(caller, t, +1)
        self.series.append((t, self.bytes))

    def free(self, t: float, pointer: int, caller: str = "?") -> None:
        self._seen(t)
        if not pointer:
            return                                                  # free(NULL) は何もしない
        self.frees += 1
        entry = self.live.pop(pointer, None)
        if entry is None:
            if pointer in self.freed:
                self._issue("double_free", t, pointer, caller, "二重の解放")
            else:
                self.untracked_frees += 1                           # 記録に無いポインタの解放（いつの確保かは決めない）
            return
        self.freed.add(pointer)
        self.bytes -= entry.size
        self._count(entry.caller, t, -1)
        self.series.append((t, self.bytes))

    def report(self, now: float, min_age_s: float = 0.0, top: int = 20, series_points: int = SERIES_POINTS,
               live_points: int = 5) -> dict:
        """要約・呼び出し元ごとの生きている確保（live_by_caller: min_age_s より古いもの。バイトの多い順に top 個。
        callers_total = 切る前の呼び出し元の数、truncated = 切ったか、top = 使った値。pointers は先頭の POINTERS 個で、
        pointers_truncated = count より少ないか。live_counts = 最初と最後の出来事の間を 4 つに分けた 5 つの時点での、
        その呼び出し元の生きている数。リークかは判定しない）・誤り（issues。issues_total = 総数、issues_truncated =
        持つ上限で切ったか、issues_limit = 上限）・使用量の推移（series。series_points 点まで等間隔に間引く:
        series_total = 元の点の数、series_downsampled = 間引いたか、series_points = 使った点の数）。"""
        if series_points < 2 or live_points < 2:
            raise ValueError("series_points・live_points は 2 以上（最初と最後の点）")
        groups: dict[str, dict] = {}
        for pointer, e in self.live.items():
            if now - e.t < min_age_s:
                continue
            g = groups.setdefault(e.caller, {"caller": e.caller, "count": 0, "bytes": 0, "oldest_t": e.t,
                                             "pointers": []})
            g["count"] += 1
            g["bytes"] += e.size
            g["oldest_t"] = min(g["oldest_t"], e.t)
            if len(g["pointers"]) < POINTERS:
                g["pointers"].append(pointer)
        for g in groups.values():
            g["live_counts"] = self._live_counts(g["caller"], live_points)
            g["pointers_truncated"] = len(g["pointers"]) < g["count"]
        callers = sorted(groups.values(), key=lambda g: (-g["bytes"], g["caller"]))[:top]
        return {"callers_total": len(groups), "truncated": len(callers) < len(groups), "top": top,
                "allocations":self.allocations, "frees": self.frees, "untracked_frees": self.untracked_frees,
                "live": {"count": len(self.live), "bytes": self.bytes}, "peak_bytes": self.peak,
                "issues": list(self.issues), "issues_total": self.issues_total,
                "issues_truncated": self.issues_total > len(self.issues), "issues_limit": self.issues_limit,
                "live_by_caller": callers, "series": self._downsampled(series_points), "series_total": len(self.series),
                "series_downsampled": len(self.series) > series_points, "series_points": series_points,
                "live_points": live_points}                 # live_counts の点の数（最初から最後まで等間隔の時刻）

    def _live_counts(self, caller: str, points: int) -> list[tuple[float, int]]:
        """呼び出し元の生きている数を、最初から最後まで points 個の等間隔の時刻で（その時刻の値。間の山は落ちうる）。"""
        history = self.history.get(caller, [])
        if self.first is None:
            return []
        marks = [self.first + (self.last - self.first) * k / (points - 1) for k in range(points)]
        out, i, n = [], 0, 0
        for m in marks:
            while i < len(history) and history[i][0] <= m:
                n = history[i][1]
                i += 1
            out.append((m, n))
        return out

    def _downsampled(self, points: int) -> list[tuple[float, int]]:
        s = self.series
        if len(s) <= points:
            return list(s)
        step = len(s) / (points - 1)
        return [s[int(i * step)] for i in range(points - 1)] + [s[-1]]
