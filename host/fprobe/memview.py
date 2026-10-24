"""メモリの読み手（抽象）・ELF の像・16 進ダンプ・差分。

逆アセンブル（fprobe.disasm）と周辺レジスタ（fprobe.svd）はメモリを直接触らず、ここの約束で読む:

    MemoryReader.read_memory(address, length) -> bytes       # 中身（命令・データ）
    RegisterAccess.read(address, width) -> int                # 周辺レジスタ（アクセス幅を守る 1 回の読み）

実機（pyOCD の target）は TargetMemory、ELF の中身は ElfImage、保存したダンプ・試験は BytesMemory。

仕様: docs/superpowers/specs/2026-10-17-f2-disasm-svd-design.md
"""

import bisect
import string
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

__all__ = ["BytesMemory", "Change", "CodeMap", "DumpRow", "ElfImage", "ElfSymbol", "MemoryReadError", "MemoryReader",
           "Region", "RegisterAccess", "TargetMemory", "diff", "format_hexdump", "hexdump"]

_WIDTHS = (8, 16, 32)


class MemoryReadError(Exception):
    """読めない番地（像の外・バスエラーなど）。"""


@runtime_checkable
class MemoryReader(Protocol):
    def read_memory(self, address: int, length: int) -> bytes: ...


@runtime_checkable
class RegisterAccess(Protocol):
    def read(self, address: int, width: int) -> int: ...

    def write(self, address: int, width: int, value: int) -> None: ...


def _check_width(width: int) -> None:
    if width not in _WIDTHS:
        raise ValueError(f"アクセス幅は 8 / 16 / 32 ビット: {width}")


class BytesMemory:
    """base 番地から始まるバイト列（保存したダンプ・試験）。"""

    def __init__(self, base: int, data: bytes):
        self.base, self.data = base, bytes(data)

    def read_memory(self, address: int, length: int) -> bytes:
        start = address - self.base
        if start < 0 or start + length > len(self.data) or length < 0:
            raise MemoryReadError(f"0x{address:08X} から {length} バイトは範囲の外")
        return self.data[start:start + length]


class TargetMemory:
    """pyOCD の target を MemoryReader / RegisterAccess に合わせる。"""

    def __init__(self, target):
        self.target = target

    def read_memory(self, address: int, length: int) -> bytes:
        return bytes(self.target.read_memory_block8(address, length))

    def read(self, address: int, width: int) -> int:
        _check_width(width)
        return self.target.read_memory(address, transfer_size=width)

    def write(self, address: int, width: int, value: int) -> None:
        _check_width(width)
        self.target.write_memory(address, value, transfer_size=width)
        self.target.flush()                        # pyOCD は書き込みを溜める。戻る時点で書き終えておく


# --- ELF の像 ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ElfSymbol:
    name: str
    address: int                 # 関数は Thumb のビットを落とした番地
    size: int
    kind: str                    # "function" / "object"


@dataclass(frozen=True)
class Region:
    address: int                 # 実行時の番地（VMA）
    data: bytes
    name: str


class CodeMap:
    """ELF のマッピングシンボル（$t = Thumb、$a = A32、$d = データ）の区間。

    印は「ここから次の印まで」を表す。区画（セクション）の終わりで区間も終わる。"""

    def __init__(self, marks: list[tuple[int, str]], ends: list[tuple[int, int]] = ()):
        # ends: (区画の始まり, 終わり)。区画の外は印が届かない
        self._marks = sorted(marks)
        self._starts = [address for address, _ in self._marks]
        self._sections = sorted(ends)

    def __bool__(self) -> bool:
        return bool(self._marks)

    def _section_end(self, address: int) -> int | None:
        for start, end in self._sections:
            if start <= address < end:
                return end
        return None if self._sections else 1 << 64

    def _index(self, address: int) -> int | None:
        i = bisect.bisect_right(self._starts, address) - 1
        if i < 0:
            return None
        end = self._section_end(self._starts[i])
        if end is None or address >= end:
            return None
        return i

    def kind_at(self, address: int) -> str | None:
        """"t" / "a" / "d"、印の無い所は None。"""
        i = self._index(address)
        return None if i is None else self._marks[i][1]

    def region_end(self, address: int) -> int | None:
        """address を含む区間の終わり（次の印か区画の終わり）。"""
        i = self._index(address)
        if i is None:
            return None
        end = self._section_end(self._starts[i])
        return min(end, self._starts[i + 1]) if i + 1 < len(self._starts) else end

    def last_code_start(self, address: int) -> int | None:
        """address を含むコードの区間の始まり（確かな命令の境界）。コードでなければ None。"""
        i = self._index(address)
        if i is None or self._marks[i][1] == "d":
            return None
        return self._starts[i]


class ElfImage:
    """ELF の割り当てる区画（中身のあるもの）を実行時の番地で読む MemoryReader。"""

    def __init__(self, path: str | Path):
        from elftools.elf.constants import SH_FLAGS             # pyelftools（fprobe[debug]）は ELF を読むときだけ要る
        from elftools.elf.elffile import ELFFile
        from elftools.elf.sections import SymbolTableSection

        from .symbols import is_network_path
        if is_network_path(path):
            raise ValueError(f"外の共有の ELF は開かない: {path}")
        self.path = Path(path)
        with open(self.path, "rb") as f:
            elf = ELFFile(f)
            sections = [s for s in elf.iter_sections() if s["sh_flags"] & SH_FLAGS.SHF_ALLOC]
            self.regions = tuple(sorted((Region(s["sh_addr"], s.data(), s.name) for s in sections
                                         if s["sh_type"] != "SHT_NOBITS" and s["sh_size"] > 0),
                                        key=lambda r: r.address))
            marks, named = [], {}
            for table in elf.iter_sections():
                if not isinstance(table, SymbolTableSection):
                    continue
                for sym in table.iter_symbols():
                    if not isinstance(sym["st_shndx"], int):                # 未定義・絶対（__stack_top 等）は除く
                        continue
                    kind = sym.name[1:2] if sym.name[:1] == "$" else ""
                    if kind in ("t", "a", "d") and sym.name[2:3] in ("", "."):
                        marks.append((sym["st_value"] & ~1 if kind == "t" else sym["st_value"], kind))
                        continue
                    kind = {"STT_FUNC": "function", "STT_OBJECT": "object"}.get(sym["st_info"]["type"])
                    if kind and sym.name:
                        address = sym["st_value"] & ~1 if kind == "function" else sym["st_value"]
                        named.setdefault(sym.name, ElfSymbol(sym.name, address, sym["st_size"], kind))
            ends = [(s["sh_addr"], s["sh_addr"] + s["sh_size"]) for s in sections
                    if s["sh_flags"] & SH_FLAGS.SHF_EXECINSTR]
        self.code_map = CodeMap(marks, ends)
        self._starts = [r.address for r in self.regions]
        self._index_names(named)

    def _index_names(self, named: dict[str, "ElfSymbol"]) -> None:
        """名前 → シンボルと、symbol_at の索引（番地の順。同じ番地は大きいほうが先）。"""
        self._named = named
        self._by_address = sorted(named.values(), key=lambda n: (n.address, -n.size))
        self._name_starts = [n.address for n in self._by_address]
        self._name_extent = max((max(n.size, 1) for n in self._by_address), default=1)   # 後ろを探す距離（最大の大きさ）

    # --- シンボル（関数・変数）: 番地 → 名前 ---------------------------------------------

    def symbol(self, name: str) -> "ElfSymbol":
        try:
            return self._named[name]
        except KeyError:
            raise KeyError(f"シンボル {name!r} が {self.path.name} に無い") from None

    def symbol_at(self, address: int, kind: str | None = None) -> "ElfSymbol | None":
        """address を含む関数・変数（大きさ 0 のものは先頭の番地だけ）。kind で "function" / "object" に限る。"""
        i = bisect.bisect_right(self._name_starts, address) - 1
        while i >= 0 and address - self._by_address[i].address < self._name_extent:
            s = self._by_address[i]
            if (kind is None or s.kind == kind) and (address < s.address + s.size or address == s.address):
                return s
            i -= 1
        return None

    def name_of(self, address: int) -> str | None:
        """"sink" / "tick+4"。"""
        s = self.symbol_at(address)
        if s is None:
            return None
        return s.name if address == s.address else f"{s.name}+{address - s.address}"

    def _region(self, address: int) -> Region | None:
        i = bisect.bisect_right(self._starts, address) - 1
        if i >= 0 and address < self.regions[i].address + len(self.regions[i].data):
            return self.regions[i]
        return None

    def contains(self, address: int, length: int = 1) -> bool:
        region = self._region(address)
        return region is not None and address + length <= region.address + len(region.data)

    def read_memory(self, address: int, length: int) -> bytes:
        out = bytearray()
        while len(out) < length:                                    # 隣り合う区画にまたがってよい
            at = address + len(out)
            region = self._region(at)
            if region is None:
                raise MemoryReadError(f"0x{at:08X} は {self.path.name} の中身の外")
            start = at - region.address
            out += region.data[start:start + length - len(out)]
        return bytes(out)


# --- 16 進ダンプ・差分 ---------------------------------------------------------------------

@dataclass(frozen=True)
class DumpRow:
    address: int
    values: tuple[int, ...]      # 単位（8/16/32 ビット）ごとの値。16/32 は小さい端が先（リトルエンディアン）
    text: str                    # ASCII（表示できない文字は "."）
    changed: tuple[bool, ...]    # 単位ごとに、前回の読みと違うか（前回が無ければすべて False）


_PRINTABLE = set(string.printable) - set("\t\n\r\x0b\x0c")


def hexdump(data: bytes, base: int, *, width: int = 16, unit: int = 8,
            previous: bytes | None = None) -> list[DumpRow]:
    """data を width バイトずつの行に。unit の単位で値を並べる（長さは単位の倍数）。"""
    _check_width(unit)
    size = unit // 8
    if width <= 0 or width % size:
        raise ValueError(f"幅 {width} は単位 {size} バイトの倍数で")
    if len(data) % size:
        raise ValueError(f"長さ {len(data)} は単位 {size} バイトの倍数で")
    rows = []
    for offset in range(0, len(data), width):
        chunk = data[offset:offset + width]
        values, changed = [], []
        for i in range(0, len(chunk), size):
            values.append(int.from_bytes(chunk[i:i + size], "little"))
            at = offset + i
            changed.append(previous is not None and previous[at:at + size] != chunk[i:i + size])
        text = "".join(chr(b) if chr(b) in _PRINTABLE else "." for b in chunk)
        rows.append(DumpRow(base + offset, tuple(values), text, tuple(changed)))
    return rows


def format_hexdump(rows: list[DumpRow], unit: int = 8, *, marks: bool | None = None) -> str:
    """"20000000: 48 65 ...  |He..|"。marks（既定: 変わった単位があるとき）なら各単位の後ろに印の欄（変わったら "*"）。"""
    _check_width(unit)
    if not rows:
        return ""
    digits = unit // 4
    marks = any(any(r.changed) for r in rows) if marks is None else marks
    columns = max(len(r.values) for r in rows)
    lines = []
    for r in rows:
        cells = [f"{v:0{digits}x}" + (("*" if c else " ") if marks else "") for v, c in zip(r.values, r.changed)]
        cells += [" " * (digits + marks)] * (columns - len(cells))      # 最後の短い行も ASCII の欄を揃える
        lines.append(f"{r.address:08x}: {' '.join(cells)}  |{r.text}|")
    return "\n".join(lines)


@dataclass(frozen=True)
class Change:
    address: int
    before: bytes
    after: bytes


def diff(previous: bytes, current: bytes, base: int) -> list[Change]:
    """前回の読みと今回の読みで変わった連続区間。"""
    if len(previous) != len(current):
        raise ValueError(f"長さが違う: {len(previous)} と {len(current)}")
    out, start = [], None
    for i in range(len(current) + 1):
        differs = i < len(current) and previous[i] != current[i]
        if differs and start is None:
            start = i
        elif not differs and start is not None:
            out.append(Change(base + start, previous[start:i], current[start:i]))
            start = None
    return out
