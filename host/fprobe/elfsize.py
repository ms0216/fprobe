"""ELF のメモリ使用量（Build Analyzer 相当、U26）: 区画・メモリ領域ごとの使用量と使用率・シンボルの大きさ、
ベアメタルの MSP のスタックの範囲（リンカのシンボル・ベクタ表から）。

ELF には領域（リンカスクリプトの MEMORY）が残らないので、領域は別に渡す: リンカスクリプト（parse_linker_regions）、
GNU ld のマップファイル（parse_map_regions）、接続中のターゲットのメモリマップ、または直接。

    layout = ElfLayout.from_elf("app.elf")
    regions = parse_linker_regions(Path("STM32F401RETX_FLASH.ld").read_text())
    size_report(layout, regions, top=20)          # 領域ごとの使用量・使用率、区画、大きいシンボル
    stack_bounds(layout, regions)                 # MSP のスタックの上端・下端・名目の大きさ

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

# スタックの上端・下端・大きさのリンカのシンボル（CMSIS・STM32CubeIDE・MCUXpresso・nRF・Zephyr 以外のよくある名前）
TOP_SYMBOLS = ("_estack", "__StackTop", "__stack_top", "__stack", "_stack_top", "__initial_sp", "_vStackTop",
               "__stack_end__", "_stack_end", "__stack_end")
LIMIT_SYMBOLS = ("__StackLimit", "__stack_limit", "_sstack", "__stack_start__", "_stack_start", "_vStackBase",
                 "__StackBottom")
SIZE_SYMBOLS = ("_Min_Stack_Size", "__stack_size__", "__STACK_SIZE", "_StackSize", "_stack_size", "__stack_size",
                "STACK_SIZE")
VECTOR_SYMBOLS = ("g_pfnVectors", "__isr_vector", "__Vectors", "__vectors", "_vectors", "vectors", "vector_table",
                  "__vector_table", "isr_vector", "_vector_table")
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
    vector_words: tuple[int, ...] = ()   # ベクタ表の先頭の語（初期の MSP, リセット, ...）。分かるとき
    vector_address: int | None = None

    @classmethod
    def from_elf(cls, path: str | Path) -> "ElfLayout":
        if is_network_path(path):
            raise ValueError(f"外の共有の ELF は開かない: {path}")
        with open(path, "rb") as f:
            elf = ELFFile(f)
            segments = [s for s in elf.iter_segments() if s["p_type"] == "PT_LOAD"]
            sections, names, data = [], {}, {}
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
                if content:
                    data[s.name] = (address, s)
            symbols, values, absolute = [], {}, {}
            vector_at = None
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
                    if s.name in VECTOR_SYMBOLS and vector_at is None and isinstance(shndx, int):
                        vector_at = value
                    if kind is not None and s["st_size"] > 0:
                        where = shndx if isinstance(shndx, str) else names.get(shndx, "?")
                        symbols.append(ElfSymbol(s.name, value, s["st_size"], kind,
                                                 {"SHN_ABS": "ABS", "SHN_COMMON": "COMMON"}.get(where, where),
                                                 s["st_info"]["bind"] == "STB_LOCAL"))
            words, vector_at = _vector_words(data, vector_at)
            return cls(Path(path), elf["e_entry"] & ~1, sorted(sections, key=lambda s: s.address), symbols, values,
                       absolute, words, vector_at)

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


def _vector_words(data: dict, vector_at: int | None) -> tuple[tuple[int, ...], int | None]:
    """ベクタ表の先頭 2 語（初期の SP・リセット）。表の名前のシンボルが無ければ、番地の最も低い中身のある区画の先頭。"""
    candidates = sorted(data.values(), key=lambda item: item[0])
    if vector_at is None and candidates:
        vector_at = candidates[0][0]
    for address, section in candidates:
        if address <= vector_at and vector_at + 8 <= address + section["sh_size"]:
            raw = section.data()[vector_at - address:vector_at - address + 8]
            return (int.from_bytes(raw[:4], "little"), int.from_bytes(raw[4:], "little")), vector_at
    return (), None


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


def _first(values: dict[str, int], names: tuple[str, ...]) -> tuple[str, int] | None:
    for n in names:
        if n in values:
            return n, values[n]
    return None


def stack_bounds(layout: ElfLayout, regions: list[MemoryRegion], *, nominal: bool = True) -> StackBounds:
    """MSP のスタックの範囲。上端: リンカのシンボル（_estack 等）か、ベクタ表の最初の語。下端: nominal なら
    リンカの下端のシンボル（__StackLimit 等）か「上端 − 名目の大きさ（_Min_Stack_Size 等）」、無ければ（または
    nominal=False なら）上端の下にある最後の RAM の区画の終わり（ヒープが伸びてくる所も含む。使い得る全部）。"""
    notes = []
    found = _first(layout.values, TOP_SYMBOLS)
    if found is not None:
        top_source, top = found
    elif layout.vector_words and layout.vector_words[0] and layout.vector_words[0] % 4 == 0:
        top_source, top = "vector_table", layout.vector_words[0]
    else:
        raise ValueError("スタックの上端が分からない（_estack 等のシンボルもベクタ表も無い）: top を指定する")
    size_sym = _first(layout.values, SIZE_SYMBOLS)
    limit_sym = _first(layout.values, LIMIT_SYMBOLS)
    nominal_size = None
    if limit_sym is not None and limit_sym[1] < top:
        nominal_size = top - limit_sym[1]
    elif size_sym is not None and 0 < size_sym[1] < top:
        nominal_size = size_sym[1]
    region = next((r for r in regions if r.contains(top - 1)), None)
    below = [s.end for s in layout.sections
             if s.end <= top and (region.contains(s.address) if region is not None else s.writable)]
    free_bottom = -(-max(below) // STACK_ALIGN) * STACK_ALIGN if below else (region.origin if region else None)
    if nominal and nominal_size is not None:
        bottom = top - nominal_size
        source = limit_sym[0] if limit_sym is not None and limit_sym[1] == bottom else f"{top_source} - {size_sym[0]}"
        if free_bottom is not None and bottom < free_bottom:
            notes.append(f"名目のスタック（0x{bottom:08X}〜）が RAM の区画（〜0x{free_bottom:08X}）に重なる")
    elif free_bottom is not None:
        bottom, source = free_bottom, "end_of_ram_sections"
        notes.append("下端は上端の下の最後の区画の終わり（ヒープが伸びてくると、その分も使用量に数える）")
    else:
        raise ValueError("スタックの下端が分からない: bottom を指定する")
    if region is None and regions:
        notes.append(f"上端 0x{top:08X} がどの領域にも入らない")
    return StackBounds(top, bottom, nominal_size, top_source, source, notes)
