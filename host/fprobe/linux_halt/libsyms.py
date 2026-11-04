"""共有ライブラリ（libc など）のシンボル: 実行時の番地 → 関数名 + オフセット。

C のソースが無くても（Raspberry Pi OS の libc は行番号の情報を持たない）、関数の名前と ARM / Thumb の別は ELF の
シンボル表（.symtab、無ければ .dynsym）から分かる。gdb が `usleep () from libc.so.6` と出すのと同じ事実。
引くのはシンボルのサイズの内だけ（サイズの外の番地を、いちばん近い名前に当てはめない。推測しない）。

attach のとき（コアを止める前。止まった対象の ssh は答えない）に、対象の /proc/<pid>/maps が指す共有ライブラリを PC に
取り寄せておき（view.prefetch_libraries）、ここで読む。読み込み位置は maps の「ファイルの先頭（offset 0）の対応の始まり」から
求める（link_map の名前の照合に頼らない。名前はシンボリックリンクで maps と違うことがある）。"""
from __future__ import annotations

import bisect
import re
from dataclasses import dataclass
from pathlib import Path

from elftools.elf.elffile import ELFFile

MASK = 0xFFFFFFFF
_MAPS_LINE = re.compile(r"^([0-9a-f]+)-([0-9a-f]+)\s+(\S{4})\s+([0-9a-f]+)\s+\S+\s+\d+\s*(.*)$")
_FUNC_TYPES = ("STT_FUNC", "STT_GNU_IFUNC")


@dataclass(frozen=True)
class LibLocation:
    module: str            # ライブラリのファイル名（libc.so.6 など）
    function: str
    offset: int            # 関数の先頭からのバイト数
    thumb: bool            # 関数が Thumb か（ARM の ELF の約束: st_value の bit 0）


def parse_maps(text: str) -> list[dict]:
    """/proc/<pid>/maps の、ファイルに対応する行: {path, start, end, perms, offset}。削除済み（(deleted)）は除く。"""
    out = []
    for line in text.splitlines():
        m = _MAPS_LINE.match(line.strip())
        if m is None:
            continue
        start, end, perms, offset, path = m.groups()
        if not path.startswith("/") or path.endswith("(deleted)"):
            continue
        out.append({"path": path, "start": int(start, 16), "end": int(end, 16), "perms": perms, "offset": int(offset, 16)})
    return out


def library_bases(maps: list[dict], exclude_path: str | None = None) -> dict[str, int]:
    """共有ライブラリ（.so を名前に含むファイル）ごとの、ファイルの先頭（offset 0）の対応の始まり。exclude_path（実行ファイル）は除く。"""
    out: dict[str, int] = {}
    for e in maps:
        name = e["path"].rsplit("/", 1)[-1]
        if ".so" not in name or e["path"] == exclude_path:
            continue
        if e["offset"] == 0 and e["path"] not in out:
            out[e["path"]] = e["start"]
    return out


class ModuleSymbols:
    """1 つのライブラリの、関数のシンボル表と読み込み位置。"""

    def __init__(self, path: str, local_path: str | Path, base: int):
        self.path, self.base = path, base
        self.name = path.rsplit("/", 1)[-1]
        with open(local_path, "rb") as f:
            elf = ELFFile(f)
            if elf.elfclass != 32 or not elf.little_endian or elf["e_machine"] != "EM_ARM":
                raise ValueError("%s: only 32-bit little-endian ARM ELF files are handled" % self.name)
            loads = [(s["p_vaddr"], s["p_memsz"]) for s in elf.iter_segments() if s["p_type"] == "PT_LOAD"]
            if not loads:
                raise ValueError("%s: no PT_LOAD segment" % self.name)
            first = min(v for v, _ in loads) & ~0xFFF
            self.load_bias = (base - first) & MASK                      # 実行時 = リンク時 + load_bias
            self._lo = (min(v for v, _ in loads) + self.load_bias) & MASK
            self._hi = (max(v + m for v, m in loads) + self.load_bias) & MASK
            section = elf.get_section_by_name(".symtab")
            self.source = ".symtab"
            if section is None:
                section, self.source = elf.get_section_by_name(".dynsym"), ".dynsym"
            symbols = []
            if section is not None:
                for s in section.iter_symbols():
                    if s["st_info"]["type"] not in _FUNC_TYPES or s["st_shndx"] == "SHN_UNDEF" or not s["st_value"] or not s.name:
                        continue
                    symbols.append((s["st_value"] & ~1, s["st_size"], s.name, bool(s["st_value"] & 1)))
            else:
                self.source = None
        symbols.sort(key=lambda x: (x[0], x[2]))
        self._symbols = symbols
        self._starts = [s[0] for s in symbols]

    @property
    def count(self) -> int:
        return len(self._symbols)

    def contains(self, address: int) -> bool:
        return self._lo <= address < self._hi

    def lookup(self, address: int) -> LibLocation | None:
        """実行時の番地を含むシンボル（サイズの内。サイズ 0 は先頭の番地だけ）。無ければ None。"""
        link = (address - self.load_bias) & MASK
        i = bisect.bisect_right(self._starts, link) - 1
        if i < 0:
            return None
        first = bisect.bisect_left(self._starts, self._starts[i])      # 同じ先頭の別名は、名前順の最初を使う
        start, size, name, thumb = self._symbols[first]
        if link < start + max(size, 1) and (size > 0 or link == start):
            return LibLocation(self.name, name, link - start, thumb)
        return None


class LibraryModules:
    """attach のとき取り寄せたライブラリのシンボルの集まり（実行時の番地から引く）。"""

    def __init__(self, entries: list[dict] | None):
        """entries: [{path, local_path, base}]。読めないものは facts に error として残し、他は続ける。"""
        self.modules: list[ModuleSymbols] = []
        self._facts: list[dict] = []
        for e in entries or []:
            item = {"path": e["path"], "local_path": e.get("local_path"), "base": e.get("base"), "symbols": None,
                    "source": None, "error": e.get("error")}
            if item["error"] is None:
                try:
                    m = ModuleSymbols(e["path"], e["local_path"], e["base"])
                    self.modules.append(m)
                    item.update(symbols=m.count, source=m.source)
                except (OSError, ValueError, KeyError) as ex:
                    item["error"] = "%s: %s" % (type(ex).__name__, ex)
            self._facts.append(item)

    def facts(self) -> list[dict]:
        return [dict(f) for f in self._facts]

    def lookup(self, address: int) -> LibLocation | None:
        for m in self.modules:
            if m.contains(address):
                return m.lookup(address)
        return None

    def module_of(self, address: int) -> tuple[str, int] | None:
        """address を含むライブラリの (ファイル名, ライブラリの中の番地 = リンク時の番地)。どのライブラリの中でもなければ None。
        シンボルが無い（公開されていない内部の関数）番地でも、どのライブラリの中かは事実として分かる。"""
        for m in self.modules:
            if m.contains(address):
                return m.name, (address - m.load_bias) & MASK
        return None

    def thumb_at(self, address: int) -> bool | None:
        """address を含む関数が Thumb か（シンボルが無ければ None）。"""
        loc = self.lookup(address)
        return None if loc is None else loc.thumb
