"""ELF のメモリ使用量（Build Analyzer 相当、U26）: 区画・メモリ領域ごとの使用量と使用率・シンボルの大きさ、
ベアメタルの MSP のスタックの範囲（使う側が名前を渡すリンカのシンボル・番地を渡すベクタ表から）。

ELF には領域（リンカスクリプトの MEMORY）が残らないので、領域は別に渡す: リンカスクリプト（parse_linker_regions）、
GNU ld のマップファイル（parse_map_regions）、接続中のターゲットのメモリマップ、または直接。

    layout = ElfLayout.from_elf("app.elf")
    regions = parse_linker_regions(Path("STM32F401RETX_FLASH.ld").read_text())
    size_report(layout, regions, top=20)          # 領域ごとの使用量・使用率、区画、大きいシンボル
    stack_bounds(layout, regions, top_symbol="_estack", size_symbol="_Min_Stack_Size")   # MSP のスタックの範囲

領域の使用量: 読み込む区画（SHF_ALLOC）は実行時の番地（VMA）の領域を使い、中身のある区画で読み込み番地（LMA）が
違うもの（.data の初期値）は LMA の領域も使う（Flash の .data の写し）。
"""

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path

from elftools.elf.constants import SH_FLAGS
from elftools.elf.elffile import ELFFile
from elftools.elf.sections import SymbolTableSection

from .symbols import is_network_path

__all__ = ["ElfLayout", "ElfSection", "ElfSymbol", "MemoryRegion", "StackBounds", "parse_linker_regions",
           "parse_map_regions", "size_report", "stack_bounds"]

STACK_ALIGN = 8


@dataclass(frozen=True)
class MemoryRegion:
    name: str
    origin: int
    length: int
    attributes: str = ""

    @property
    def end(self) -> int:
        return self.origin + self.length

    def contains(self, address: int) -> bool:
        return self.origin <= address < self.end

    def overlap(self, start: int, size: int) -> int:
        return max(0, min(self.end, start + size) - max(self.origin, start))


@dataclass(frozen=True)
class ElfSection:
    name: str
    address: int                     # 実行時の番地（VMA）
    load_address: int                # 読み込み番地（LMA）。中身の無い区画は VMA と同じ
    size: int
    has_content: bool                # PROGBITS（False = NOBITS: .bss など、0 で埋める・何もしない）
    writable: bool
    executable: bool

    @property
    def end(self) -> int:
        return self.address + self.size


@dataclass(frozen=True)
class ElfSymbol:
    name: str
    address: int                     # 関数は Thumb のビットを落とした番地
    size: int
    kind: str                        # function / object
    section: str                     # 区画の名前（"ABS" / "COMMON"）
    local: bool


@dataclass
class ElfLayout:
    path: Path
    entry: int
    sections: list[ElfSection]
    symbols: list[ElfSymbol]
    values: dict[str, int]           # 名前のあるシンボル（ラベル・リンカの定義を含む）→ 値（最初のもの）
    absolute: dict[str, int]         # そのうち絶対（SHN_ABS）のもの

    @classmethod
    def from_elf(cls, path: str | Path) -> "ElfLayout":
        if is_network_path(path):
            raise ValueError(f"外の共有の ELF は開かない: {path}")
        with open(path, "rb") as f:
            elf = ELFFile(f)
            segments = [s for s in elf.iter_segments() if s["p_type"] == "PT_LOAD"]
            sections, names = [], {}
            for index, s in enumerate(elf.iter_sections()):
                names[index] = s.name
                if not s["sh_flags"] & SH_FLAGS.SHF_ALLOC or s["sh_size"] == 0:
                    continue
                if s["sh_flags"] & SH_FLAGS.SHF_TLS and s["sh_type"] == "SHT_NOBITS":
                    continue                                    # .tbss は番地を占めない（各スレッドの写しの型紙）
                content = s["sh_type"] != "SHT_NOBITS"
                address = s["sh_addr"]
                sections.append(ElfSection(s.name, address, _load_address(segments, address) if content else address,
                                           s["sh_size"], content, bool(s["sh_flags"] & SH_FLAGS.SHF_WRITE),
                                           bool(s["sh_flags"] & SH_FLAGS.SHF_EXECINSTR)))
            symbols, values, absolute = [], {}, {}
            for section in elf.iter_sections():
                if not isinstance(section, SymbolTableSection):
                    continue
                for s in section.iter_symbols():
                    if not s.name or s.name.startswith("$"):
                        continue
                    kind = {"STT_FUNC": "function", "STT_OBJECT": "object"}.get(s["st_info"]["type"])
                    value = s["st_value"] & ~1 if kind == "function" else s["st_value"]
                    shndx = s["st_shndx"]
                    values.setdefault(s.name, value)
                    if shndx == "SHN_ABS":
                        absolute.setdefault(s.name, value)
                    if kind is not None and s["st_size"] > 0:
                        where = shndx if isinstance(shndx, str) else names.get(shndx, "?")
                        symbols.append(ElfSymbol(s.name, value, s["st_size"], kind,
                                                 {"SHN_ABS": "ABS", "SHN_COMMON": "COMMON"}.get(where, where),
                                                 s["st_info"]["bind"] == "STB_LOCAL"))
            return cls(Path(path), elf["e_entry"] & ~1, sorted(sections, key=lambda s: s.address), symbols, values,
                       absolute)

    def symbol(self, name: str) -> ElfSymbol:
        for s in self.symbols:
            if s.name == name:
                return s
        raise KeyError(f"シンボル {name!r} が無い（大きさのある関数・変数）")


def _load_address(segments, address: int) -> int:
    for seg in segments:
        if seg["p_vaddr"] <= address < seg["p_vaddr"] + max(seg["p_memsz"], 1):
            return seg["p_paddr"] + address - seg["p_vaddr"]
    return address


def _read_word(path: Path, address: int) -> int | None:
    """ELF の中身のある区画（SHF_ALLOC）の address の 32 ビットの語（リトルエンディアン）。無ければ None。"""
    with open(path, "rb") as f:
        for s in ELFFile(f).iter_sections():
            if not s["sh_flags"] & SH_FLAGS.SHF_ALLOC or s["sh_type"] == "SHT_NOBITS":
                continue
            if s["sh_addr"] <= address and address + 4 <= s["sh_addr"] + s["sh_size"]:
                offset = address - s["sh_addr"]
                return int.from_bytes(s.data()[offset:offset + 4], "little")
    return None


# --- 領域 ---------------------------------------------------------------------------------------

_SUFFIX = {"": 1, "K": 1024, "M": 1024 * 1024, "G": 1024 ** 3}
_TERM = re.compile(r"\s*([+-]?)\s*(0[xX][0-9a-fA-F]+|\d+)\s*([KkMmGg]?)")


def _number(text: str) -> int:
    """リンカスクリプトの数（0x…・10 進・K / M 接尾辞）の足し引き。"""
    text = text.strip()
    total, at = 0, 0
    while at < len(text):
        m = _TERM.match(text, at)
        if m is None or (at and not m.group(1)):
            raise ValueError(f"数として読めない: {text!r}")
        value = int(m.group(2), 0) * _SUFFIX[m.group(3).upper()]
        total += -value if m.group(1) == "-" else value
        at = m.end()
    return total


_MEMORY_BLOCK = re.compile(r"\bMEMORY\s*\{(.*?)\}", re.S)
_MEMORY_LINE = re.compile(r"^\s*([A-Za-z_.][\w.$-]*)\s*(?:\(\s*([!A-Za-z]*)\s*\))?\s*:\s*"
                          r"(?:ORIGIN|org|o)\s*=\s*([^,]+?)\s*,\s*(?:LENGTH|len|l)\s*=\s*(.+?)\s*$")


def parse_linker_regions(text: str) -> list[MemoryRegion]:
    """リンカスクリプトの MEMORY { 名前 (属性) : ORIGIN = …, LENGTH = … }。数の足し引きと K / M まで（関数は読まない）。"""
    m = _MEMORY_BLOCK.search(re.sub(r"/\*.*?\*/", "", text, flags=re.S))
    if m is None:
        return []
    out = []
    for line in m.group(1).splitlines():
        line = line.split("//")[0]
        if not line.strip():
            continue
        r = _MEMORY_LINE.match(line)
        if r is None:
            raise ValueError(f"MEMORY の行が読めない: {line.strip()!r}")
        out.append(MemoryRegion(r.group(1), _number(r.group(3)), _number(r.group(4)), r.group(2) or ""))
    return out


def parse_map_regions(text: str) -> list[MemoryRegion]:
    """GNU ld のマップファイル（-Map）の "Memory Configuration" の表（*default* は除く）。"""
    lines = text.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == "Memory Configuration")
    except StopIteration:
        return []
    out = []
    for line in lines[start + 1:]:
        parts = line.split()
        if not parts:
            if out:
                break
            continue
        if parts[0] == "Name" or parts[0] == "*default*":
            continue
        if line.startswith("Linker script"):
            break
        if len(parts) >= 3:
            out.append(MemoryRegion(parts[0], int(parts[1], 16), int(parts[2], 16), parts[3] if len(parts) > 3 else ""))
    return out


# --- 集計 ---------------------------------------------------------------------------------------

def _region_of(regions: list[MemoryRegion], address: int) -> str | None:
    for r in regions:
        if r.contains(address):
            return r.name
    return None


def size_report(layout: ElfLayout, regions: list[MemoryRegion], *, top: int = 50, section: str | None = None,
                region: str | None = None, kind: str | None = None, name: str | None = None) -> dict:
    """領域ごとの使用量・空き・使用率と内訳、区画の一覧、領域の外の区画、大きいシンボル（section・region・
    kind（function / object）・name（glob）で絞り、大きい順に top 個）。"""
    out_regions = []
    covered: dict[tuple[str, str], int] = {}
    for r in regions:
        parts = []
        for s in layout.sections:
            run = r.overlap(s.address, s.size)
            if run:
                parts.append({"section": s.name, "as": "run", "address": s.address, "size": run})
                covered[(s.name, "run")] = covered.get((s.name, "run"), 0) + run
            if s.has_content and s.load_address != s.address:
                load = r.overlap(s.load_address, s.size)
                if load:
                    parts.append({"section": s.name, "as": "load", "address": s.load_address, "size": load})
                    covered[(s.name, "load")] = covered.get((s.name, "load"), 0) + load
        used = sum(p["size"] for p in parts)
        out_regions.append({"name": r.name, "origin": r.origin, "length": r.length, "attributes": r.attributes,
                            "used": used, "free": r.length - used, "fraction": used / r.length if r.length else 0.0,
                            "sections": parts})
    outside = []
    for s in layout.sections:
        footprints = [("run", s.address)] + ([("load", s.load_address)]
                                             if s.has_content and s.load_address != s.address else [])
        for kind_, address in footprints:
            missing = s.size - covered.get((s.name, kind_), 0)
            if missing > 0:
                outside.append({"section": s.name, "as": kind_, "address": address, "size": missing})
    sections = [{"name": s.name, "address": s.address, "load_address": s.load_address, "size": s.size,
                 "type": "progbits" if s.has_content else "nobits", "writable": s.writable,
                 "executable": s.executable, "region": _region_of(regions, s.address),
                 "load_region": _region_of(regions, s.load_address) if s.has_content else None}
                for s in layout.sections]
    chosen = [s for s in layout.symbols
              if (section is None or s.section == section) and (kind is None or s.kind == kind)
              and (name is None or fnmatch.fnmatchcase(s.name, name))
              and (region is None or _region_of(regions, s.address) == region)]
    chosen.sort(key=lambda s: (-s.size, s.address, s.name))
    notes = []
    if not regions:
        notes.append("領域（リンカスクリプトの MEMORY）が分からないので使用率を出せない: linker_script・map_file・"
                     "regions を渡すか、ターゲットに接続する（メモリマップを使う）")
    if outside and regions:
        notes.append("どの領域にも入らない区画がある（outside）: 領域の指定が足りないか、リンカの配置の誤り")
    return {"elf": str(layout.path), "regions": out_regions, "sections": sections, "outside": outside,
            "symbols": [{"name": s.name, "address": s.address, "size": s.size, "kind": s.kind, "section": s.section,
                         "region": _region_of(regions, s.address)} for s in chosen[:top]],
            "symbols_total": len(chosen), "notes": notes}


# --- スタックの範囲 -----------------------------------------------------------------------------

@dataclass
class StackBounds:
    top: int                          # 最初の SP（スタックは下へ伸びる。この番地は使わない）
    bottom: int                       # 塗る・測る下端
    nominal_size: int | None          # リンカが決めた大きさ（分かるとき）
    top_source: str
    bottom_source: str
    notes: list[str] = field(default_factory=list)

    @property
    def size(self) -> int:
        return self.top - self.bottom

    def as_dict(self) -> dict:
        return {"top": self.top, "bottom": self.bottom, "size": self.size, "nominal_size": self.nominal_size,
                "top_source": self.top_source, "bottom_source": self.bottom_source, "notes": list(self.notes)}


def _symbol_value(layout: ElfLayout, name: str) -> int:
    try:
        return layout.values[name]
    except KeyError:
        raise ValueError(f"シンボル {name!r} が ELF に無い") from None


def stack_bounds(layout: ElfLayout, regions: list[MemoryRegion], *, top: int | None = None,
                 top_symbol: str | None = None, vector_table: int | None = None, limit_symbol: str | None = None,
                 size_symbol: str | None = None) -> StackBounds:
    """MSP のスタックの範囲。シンボルの名前・ベクタ表の番地は使う側が渡す（名前の慣習の表で選ばない）。
    上端: top（番地）・top_symbol（リンカのシンボルの値）・vector_table（ベクタ表の番地。その最初の語 = 初期の MSP を
    ELF から読む）のどれか 1 つ。下端: limit_symbol（下端のシンボルの値）か「上端 − size_symbol の値」のどちらか、どちらも
    無ければ上端の下にある最後の RAM の区画の終わり（ヒープが伸びてくる所も含む。使い得る全部）。"""
    if sum(x is not None for x in (top, top_symbol, vector_table)) != 1:
        raise ValueError("スタックの上端は top（番地）・top_symbol（リンカのシンボル名）・vector_table（ベクタ表の"
                         "番地）のどれか 1 つを渡す")
    if limit_symbol is not None and size_symbol is not None:
        raise ValueError("下端は limit_symbol か size_symbol のどちらか 1 つ")
    notes = []
    if top is not None:
        top_source = "argument"
    elif top_symbol is not None:
        top_source, top = top_symbol, _symbol_value(layout, top_symbol)
    else:
        top = _read_word(layout.path, vector_table)
        if top is None:
            raise ValueError(f"ベクタ表の番地 0x{vector_table:08X} が ELF の中身のある区画に無い")
        top_source = f"vector_table 0x{vector_table:08X}"
    nominal_size = None
    if limit_symbol is not None:
        limit = _symbol_value(layout, limit_symbol)
        if limit >= top:
            raise ValueError(f"{limit_symbol}（0x{limit:08X}）が上端（0x{top:08X}）より下でない")
        nominal_size = top - limit
    elif size_symbol is not None:
        size = _symbol_value(layout, size_symbol)
        if not 0 < size <= top:
            raise ValueError(f"{size_symbol}（0x{size:X}）がスタックの大きさにならない（上端 0x{top:08X}）")
        nominal_size = size
    region = next((r for r in regions if r.contains(top - 1)), None)
    below = [s.end for s in layout.sections
             if s.end <= top and (region.contains(s.address) if region is not None else s.writable)]
    free_bottom = -(-max(below) // STACK_ALIGN) * STACK_ALIGN if below else (region.origin if region else None)
    if nominal_size is not None:
        bottom = top - nominal_size
        source = limit_symbol if limit_symbol is not None else f"{top_source} - {size_symbol}"
        if free_bottom is not None and bottom < free_bottom:
            notes.append(f"名目のスタック（0x{bottom:08X}〜）が RAM の区画（〜0x{free_bottom:08X}）に重なる")
    elif free_bottom is not None:
        bottom, source = free_bottom, "end_of_ram_sections"
        notes.append("下端は上端の下の最後の区画の終わり（ヒープが伸びてくると、その分も使用量に数える）")
    else:
        raise ValueError("スタックの下端が分からない: bottom・limit_symbol・size_symbol のどれかを渡す")
    if region is None and regions:
        notes.append(f"上端 0x{top:08X} がどの領域にも入らない")
    return StackBounds(top, bottom, nominal_size, top_source, source, notes)
