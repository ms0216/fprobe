"""ELF のシンボルと行番号（DWARF の行表）。番地 ⇄ 関数名・変数名・ファイル:行 の対応を引く。

ターゲット（pyOCD）には依存しない。デバッグの操作は fprobe.debug がこれを使って場所を解決する。

    syms = Symbols("app.elf")
    syms.resolve("main")            # 関数の先頭の番地（Thumb のビットは落とす）
    syms.resolve("main.c:42")       # その行の最初の命令の番地
    syms.describe(0x08000123)       # {"function": "main", "offset": 6, "file": "main.c", "line": 42}
    syms.variable("counter")        # Symbol(name, address, size, kind="object")
"""

import bisect
import re
from dataclasses import dataclass
from pathlib import Path, PurePath

from elftools.elf.elffile import ELFFile
from elftools.elf.sections import SymbolTableSection

from .dwarf.typesys import TypeLoader, type_name

__all__ = ["LineEntry", "Location", "Segment", "Symbol", "Symbols", "is_line_location", "is_network_path"]


@dataclass(frozen=True)
class Symbol:
    name: str
    address: int                 # 関数は Thumb のビット（bit 0）を落とした番地
    size: int
    kind: str                    # "function" / "object"


@dataclass(frozen=True)
class Location:
    """番地が指すソースの場所（分からない欄は None）。"""
    address: int
    function: str | None = None
    offset: int | None = None    # 関数の先頭からのバイト数
    file: str | None = None
    line: int | None = None

    def __str__(self) -> str:
        where = f"{self.function}+{self.offset}" if self.function is not None else f"0x{self.address:08X}"
        return f"{where} ({self.file}:{self.line})" if self.file else where


@dataclass(frozen=True)
class LineEntry:
    """行表の文（is_stmt の行）: その始まりの番地とファイル・行。行単位のステップの区切り。"""
    address: int
    file: str
    line: int


@dataclass(frozen=True)
class Segment:
    """読み込む中身（PT_LOAD の、ファイルに中身のある部分）。"""
    address: int
    data: bytes


def _text(value: object) -> str:
    """DWARF の文字列（bytes、DWARF 5 では path を持つ構造）を str に。"""
    value = getattr(value, "DW_LNCT_path", value)
    return value.decode() if isinstance(value, bytes) else str(value)


_SCOPES = {"DW_TAG_namespace", "DW_TAG_class_type", "DW_TAG_structure_type", "DW_TAG_union_type",
           "DW_TAG_enumeration_type"}


def _qualified(die) -> str | None:
    """DIE の名前を、囲む名前空間・クラスで修飾する（"motor::Pid::step"）。名前の無い DIE は None。"""
    name = die.attributes.get("DW_AT_name")
    if name is None:
        return None
    parts = [_text(name.value)]
    parent = die.get_parent()
    while parent is not None and parent.tag in _SCOPES:
        scope = parent.attributes.get("DW_AT_name")
        parts.append(_text(scope.value) if scope is not None else "(anonymous namespace)")
        parent = parent.get_parent()
    return "::".join(reversed(parts))


def is_line_location(location: str) -> bool:
    """"ファイル:行" の形か（"motor::scale" のような C++ の名前と区別する）。"""
    file, sep, line = location.strip().rpartition(":")
    return bool(sep and file and line.isdigit())


def is_network_path(path: str | Path) -> bool:
    """外の共有（UNC。円記号 2 つか // で始まる）。開くと認証が外へ出るので、ELF・ソースでは開かない。
    ただし Windows のローカルの長いパス（円記号 2 つ + "?" + 円記号 + ドライブ名）は除く。"""
    text = str(path).replace("/", "\\")
    if re.match(r"^\\\\\?\\[A-Za-z]:", text):
        return False
    return text.startswith("\\\\")


class Symbols:
    def __init__(self, path: str | Path):
        if is_network_path(path):
            raise ValueError(f"外の共有の ELF は開かない: {path}")
        self.path = Path(path)
        with open(self.path, "rb") as f:
            elf = ELFFile(f)
            self.entry = elf["e_entry"] & ~1
            self._symbols = self._read_symbols(elf)
            self._overloads: dict[str, list[str]] = {}
            linked = self._name_from_source(elf)
            self._lines, self._sources, self._prologue_ends = self._read_lines(elf)
            self._relocate_sources()
            self.segments = tuple(Segment(seg["p_paddr"], seg.data()) for seg in elf.iter_segments()
                                  if seg["p_type"] == "PT_LOAD" and seg["p_filesz"] > 0)
        named = [s for name, s in self._symbols.items() if name not in linked]   # 場所の名前はソースの名前で出す
        functions = sorted((s for s in named if s.kind == "function"), key=lambda s: s.address)
        self._function_starts = [s.address for s in functions]
        self._functions = functions
        everything = sorted(named, key=lambda s: (s.address, -s.size))
        self._all_starts = [s.address for s in everything]
        self._all = everything
        self._line_addresses = [row[0] for row in self._lines]

    # --- 読み込み ------------------------------------------------------------------

    @staticmethod
    def _read_symbols(elf: ELFFile) -> dict[str, Symbol]:
        out: dict[str, Symbol] = {}
        for section in elf.iter_sections():
            if not isinstance(section, SymbolTableSection):
                continue
            for s in section.iter_symbols():
                kind = {"STT_FUNC": "function", "STT_OBJECT": "object"}.get(s["st_info"]["type"])
                if kind is None or not s.name:
                    continue
                address = s["st_value"] & ~1 if kind == "function" else s["st_value"]
                out.setdefault(s.name, Symbol(s.name, address, s["st_size"], kind))
        return out

    def _name_from_source(self, elf: ELFFile) -> set[str]:
        """C++・Rust: ELF のシンボルはマングルされた名前（リンク名）。DWARF の関数（DW_AT_linkage_name と、名前空間・
        クラスの入れ子）からソースの名前（"motor::Pid::step"）を付け、その名前でも引けるようにする。同じ名前が
        複数（多重定義）なら引数の型を添える（"motor::scale(int, int)"）。ソースの名前を付けたリンク名の集合を返す。"""
        if not elf.has_dwarf_info():
            return set()
        found: dict[str, tuple[str, str]] = {}                  # リンク名 → (修飾した名前, 引数の型)
        types = TypeLoader(elf.elfclass // 8)
        for cu in elf.get_dwarf_info().iter_CUs():
            for die in cu.iter_DIEs():
                if die.tag != "DW_TAG_subprogram":
                    continue
                declaration = die
                while "DW_AT_linkage_name" not in declaration.attributes and \
                        "DW_AT_specification" in declaration.attributes:
                    declaration = declaration.get_DIE_from_attribute("DW_AT_specification")
                linkage = declaration.attributes.get("DW_AT_linkage_name") or \
                    declaration.attributes.get("DW_AT_MIPS_linkage_name")
                if linkage is None or _text(linkage.value) not in self._symbols or _text(linkage.value) in found:
                    continue
                name = _qualified(declaration)
                if name is None:
                    continue
                parameters = ", ".join(type_name(types.type_of(p)) for p in die.iter_children()
                                       if p.tag == "DW_TAG_formal_parameter" and not p.attributes.get("DW_AT_artificial"))
                found[_text(linkage.value)] = (name, parameters)
        counts: dict[str, int] = {}
        for name, _ in found.values():
            counts[name] = counts.get(name, 0) + 1
        for linkage, (name, parameters) in found.items():
            symbol = self._symbols[linkage]
            source = name if counts[name] == 1 else f"{name}({parameters})"
            if counts[name] > 1:
                self._overloads.setdefault(name, []).append(source)
            self._symbols.setdefault(source, Symbol(source, symbol.address, symbol.size, symbol.kind))
        return set(found)

    @staticmethod
    def _read_lines(elf: ELFFile) -> tuple[list[tuple[int, str, int, Path]], dict[str, Path], list[int]]:
        """(番地, ファイル名, 行, 場所) を番地順に（is_stmt の行だけ = 文の始まり）と、ファイル名 → 場所と、
        前置きの終わり（prologue_end）の番地の一覧（番地順）。"""
        if not elf.has_dwarf_info():
            return [], {}, []
        dwarf = elf.get_dwarf_info()
        rows: list[tuple[int, str, int, Path]] = []
        sources: dict[str, Path] = {}
        prologue_ends: set[int] = set()
        for cu in dwarf.iter_CUs():
            program = dwarf.line_program_for_CU(cu)
            if program is None:
                continue
            files = program["file_entry"]
            base = 1 if program.header["version"] < 5 else 0        # DWARF 5 はファイル番号が 0 始まり
            comp_dir = cu.get_top_DIE().attributes.get("DW_AT_comp_dir")
            dirs = [_text(d) for d in program["include_directory"]]
            if base == 1:                                           # DWARF 4: ディレクトリ 0 はコンパイルした場所
                dirs = [_text(comp_dir.value) if comp_dir else ""] + dirs
            paths = []
            for f in files:
                index = f.dir_index or 0                            # DWARF 5 で番号が無いこともある
                directory = dirs[index] if index < len(dirs) else ""
                path = Path(directory) / f.name.decode()
                if not path.is_absolute() and comp_dir:
                    path = Path(_text(comp_dir.value)) / path
                paths.append(path)
                sources.setdefault(path.name, path)                 # 同じ名前が 2 つあれば先のもの（場所の書き方と同じ）
            for entry in program.get_entries():
                state = entry.state
                if state is None or state.end_sequence or not state.is_stmt:
                    continue
                if state.prologue_end:
                    prologue_ends.add(state.address)
                index = state.file - base
                known = 0 <= index < len(files)
                name = files[index].name.decode() if known else "?"
                rows.append((state.address, PurePath(name).name, state.line, paths[index] if known else Path("?")))
        rows.sort()
        return rows, sources, sorted(prologue_ends)

    # --- 引く ----------------------------------------------------------------------

    def _relocate_sources(self) -> None:
        """ビルドした場所に無いソースが ELF の隣にあれば、そちらを使う（ELF とソースを一緒に別の場所へ移したとき。
        例: リポジトリを別の PC・場所に置いた同梱のデモ）。"""
        moved = {}
        for name, path in self._sources.items():
            here = self.path.parent / name
            if not path.exists() and here.is_file():
                moved[path] = here
        if moved:
            self._sources = {name: moved.get(p, p) for name, p in self._sources.items()}
            self._lines = [(a, f, line, moved.get(p, p)) for a, f, line, p in self._lines]

    def sources(self) -> dict[str, Path]:
        """行番号の情報が参照するソース: ファイル名 → 場所（ビルドした PC の上の場所。無いこともある）。"""
        return dict(self._sources)

    def lines_of(self, file: str) -> list[int]:
        """そのファイルで命令のある行（ブレークポイントを置ける行）。"""
        path = self._sources.get(file)                                # 同じ名前の 2 つ目のファイルの行は混ぜない
        return sorted({line for _, f, line, p in self._lines if f == file and p == path})

    def names(self) -> list[Symbol]:
        """すべての関数・変数のシンボル（ELF の名前。C++・Rust はソースの名前も）。"""
        return list(self._symbols.values())

    def symbol(self, name: str) -> Symbol:
        try:
            return self._symbols[name]
        except KeyError:
            if name in self._overloads:
                raise KeyError(f"{name!r} は多重定義: {', '.join(self._overloads[name])} のどれか") from None
            raise KeyError(f"シンボル {name!r} が {self.path.name} に無い") from None

    def variable(self, name: str) -> Symbol:
        s = self.symbol(name)
        if s.kind != "object":
            raise KeyError(f"{name!r} は変数ではない（{s.kind}）")
        return s

    def resolve(self, location: str | int) -> int:
        """場所（番地の整数・"0x..."・関数名・変数名・"ファイル:行"）を番地にする。"""
        if isinstance(location, int):
            return location
        text = location.strip()
        if text.lower().startswith("0x"):
            return int(text, 16)
        if is_line_location(text):
            file, _, line = text.rpartition(":")
            return self._line_address(PurePath(file).name, int(line))
        return self.symbol(text).address

    def _line_address(self, file: str, line: int) -> int:
        path = self._sources.get(file)
        hits = [address for address, f, n, p in self._lines if f == file and n == line and p == path]
        if not hits:
            raise KeyError(f"{file}:{line} に対応する命令が無い（最適化で消えたか、行が違う）")
        return min(hits)

    def after_prologue(self, address: int) -> int:
        """関数の先頭なら、前置き（スタックの用意）の後の番地。引数・ローカル変数が読める最初の場所（関数名の
        ブレークポイントに使う。GDB と同じ）。行表の prologue_end があればそれ、無ければ次の行の始まり。
        関数の先頭でなければそのまま。"""
        i = bisect.bisect_right(self._function_starts, address) - 1
        if i < 0 or self._function_starts[i] != address:
            return address
        end = address + max(self._functions[i].size, 1)
        j = bisect.bisect_left(self._prologue_ends, address)
        if j < len(self._prologue_ends) and self._prologue_ends[j] < end:
            return self._prologue_ends[j]
        k = bisect.bisect_left(self._line_addresses, address)
        first_line = self._lines[k][2] if k < len(self._lines) and self._lines[k][0] == address else None
        for row in self._lines[k:]:
            if row[0] >= end:
                break
            if row[0] > address and row[2] != first_line:
                return row[0]
        return address

    def symbol_at(self, address: int) -> tuple[Symbol, int] | None:
        """番地を含むシンボル（関数・変数）と、その先頭からのバイト数。大きさ 0 のシンボルは先頭の番地だけ。"""
        i = bisect.bisect_right(self._all_starts, address) - 1
        while i >= 0 and self._all_starts[i] <= address:
            s = self._all[i]
            if address < s.address + max(s.size, 1):
                return s, address - s.address
            if address - s.address > 0x10000:
                break
            i -= 1
        return None

    def function_at(self, address: int) -> Symbol | None:
        """番地を含む関数（無ければ None）。"""
        i = bisect.bisect_right(self._function_starts, address) - 1
        if i >= 0 and address < self._functions[i].address + max(self._functions[i].size, 1):
            return self._functions[i]
        return None

    def line_entry(self, address: int) -> LineEntry | None:
        """番地を含む文: 行表のその番地以下で最後の行（同じ関数の中のもの）。行の情報が無ければ None。
        番地がちょうど文の始まりなら entry.address == address。"""
        j = bisect.bisect_right(self._line_addresses, address) - 1
        if j < 0:
            return None
        row_address, file, line, _ = self._lines[j]
        function = self.function_at(address)
        if function is None:
            if self._functions:
                return None                       # 関数の外（行表の最後の行が遠くから届いているだけ）
        elif row_address < function.address:
            return None                           # 関数の中に行が無い（-g の無いコード）
        return LineEntry(row_address, file, line)

    def describe(self, address: int) -> Location:
        """番地を関数名 + オフセットとファイル:行にする。"""
        function = offset = file = line = None
        i = bisect.bisect_right(self._function_starts, address) - 1
        if i >= 0:
            f = self._functions[i]
            if address < f.address + max(f.size, 1):
                function, offset = f.name, address - f.address
        j = bisect.bisect_right(self._line_addresses, address) - 1
        if j >= 0 and (function is not None or not self._functions):
            _, file, line, _ = self._lines[j]
        return Location(address, function, offset, file, line)
