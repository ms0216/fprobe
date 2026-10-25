"""ELF のシンボルと行番号（DWARF の行表）。番地 ⇄ 関数名・変数名・ファイル:行 の対応を引く。

ターゲット（pyOCD）には依存しない。デバッグの操作は fprobe.debug がこれを使って場所を解決する。

    syms = Symbols("app.elf")
    syms.resolve("main")            # 関数の先頭の番地（Thumb のビットは落とす）
    syms.resolve("main.c:42")       # その行の最初の命令の番地
    syms.describe(0x08000123)       # {"function": "main", "offset": 6, "file": "main.c", "line": 42}
    syms.variable("counter")        # Symbol(name, address, size, kind="object")
"""

import bisect
import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePath, PurePosixPath

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


def _normal_path(path: Path) -> Path:
    """同じファイルの違う書き方（a/../a/x.c 等。CU ごとに書き方が違う）を 1 つに。"""
    return Path(os.path.normpath(path))


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
            self._symbols, self._same_named = self._read_symbols(elf)
            self._overloads: dict[str, list[str]] = {}
            dwarf = elf.get_dwarf_info() if elf.has_dwarf_info() else None     # 1 つを共有（DIE の読みを 2 度しない）
            linked = self._name_from_source(dwarf, elf.elfclass // 8)
            rows, sources, self._prologue_ends = self._read_lines(dwarf)
            self._set_lines(rows, sources)
            self._relocated: dict[str, dict[str, Path]] = {}
            self._relocate_sources()
            self.segments = tuple(Segment(seg["p_paddr"], seg.data()) for seg in elf.iter_segments()
                                  if seg["p_type"] == "PT_LOAD" and seg["p_filesz"] > 0)
        named = [s for name, s in self._symbols.items() if name not in linked]   # 場所の名前はソースの名前で出す
        named += [s for name, extra in self._same_named.items() if name not in linked for s in extra]
        # 同じ番地は大きさの順（bisect は最後を取る。大きさ 0 の別名より本体を後ろに置く）
        functions = sorted((s for s in named if s.kind == "function"), key=lambda s: (s.address, s.size))
        self._function_starts = [s.address for s in functions]
        self._functions = functions
        self._index_all(named)

    def _set_lines(self, rows: list[tuple[int, str, int, Path]], sources: dict[str, list[Path]]) -> None:
        """行表（番地順の (番地, ファイル名, 行, 場所)）と、ファイル名 → 場所（同じ名前の別のファイルはすべて、出た順）。"""
        self._lines = rows
        self._source_paths = {name: list(paths) for name, paths in sources.items()}
        self._sources = {name: paths[0] for name, paths in self._source_paths.items()}
        self._line_addresses = [row[0] for row in rows]
        self._line_index: dict[tuple[Path, int], int] | None = None     # (場所, 行) → 最小の番地（初めて引くときに作る）
        self._lines_by_file: dict[tuple[str, Path], set[int]] | None = None

    def _index_all(self, symbols) -> None:
        """symbol_at の索引（番地の順。同じ番地は大きいほうが先）。"""
        everything = sorted(symbols, key=lambda s: (s.address, -s.size))
        self._all_starts = [s.address for s in everything]
        self._all = everything
        self._all_extent = max((max(s.size, 1) for s in everything), default=1)   # 後ろを探す距離（最大の大きさ）

    # --- 読み込み ------------------------------------------------------------------

    @staticmethod
    def _read_symbols(elf: ELFFile) -> tuple[dict[str, Symbol], dict[str, list[Symbol]]]:
        """名前 → 最初のシンボル と、同じ名前で別の番地・大きさのもの（別ファイルの static 等。名前 → 2 つ目以降）。"""
        out: dict[str, Symbol] = {}
        extra: dict[str, list[Symbol]] = {}
        for section in elf.iter_sections():
            if not isinstance(section, SymbolTableSection):
                continue
            for s in section.iter_symbols():
                kind = {"STT_FUNC": "function", "STT_OBJECT": "object"}.get(s["st_info"]["type"])
                if kind is None or not s.name:
                    continue
                address = s["st_value"] & ~1 if kind == "function" else s["st_value"]
                symbol = Symbol(s.name, address, s["st_size"], kind)
                first = out.setdefault(s.name, symbol)
                if first != symbol and symbol not in extra.setdefault(s.name, []):
                    extra[s.name].append(symbol)
        return out, {name: v for name, v in extra.items() if v}

    def _name_from_source(self, dwarf, address_size: int) -> set[str]:
        """C++・Rust: ELF のシンボルはマングルされた名前（リンク名）。DWARF の関数（DW_AT_linkage_name と、名前空間・
        クラスの入れ子）からソースの名前（"motor::Pid::step"）を付け、その名前でも引けるようにする。同じ名前が
        複数（多重定義）なら引数の型を添える（"motor::scale(int, int)"）。ソースの名前を付けたリンク名の集合を返す。"""
        if dwarf is None:
            return set()
        found: dict[str, tuple[str, str]] = {}                  # リンク名 → (修飾した名前, 引数の型)
        types = TypeLoader(address_size)
        for cu in dwarf.iter_CUs():
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
    def _read_lines(dwarf) -> tuple[list[tuple[int, str, int, Path]], dict[str, list[Path]], list[int]]:
        """(番地, ファイル名, 行, 場所) を番地順に（is_stmt の行だけ = 文の始まり）と、ファイル名 → 場所（同じ名前の
        別のファイルはすべて）と、前置きの終わり（prologue_end）の番地の一覧（番地順）。"""
        if dwarf is None:
            return [], {}, []
        rows: list[tuple[int, str, int, Path]] = []
        sources: dict[str, list[Path]] = {}
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
                path = _normal_path(path)
                paths.append(path)
                same = sources.setdefault(path.name, [])
                if path not in same:                                # 同じ名前の別のファイルも残す（どちらとも決めない）
                    same.append(path)
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
        rows.sort(key=lambda r: r[0])     # 番地だけで並べる（安定。同じ番地は行番号プログラムでの出現順 = 最後が実効の行）
        return rows, sources, sorted(prologue_ends)

    # --- 引く ----------------------------------------------------------------------

    def _relocate_sources(self) -> None:
        """ビルドした場所に無いソースが ELF の隣にあれば、そちらを使う（ELF とソースを一緒に別の場所へ移したとき。
        例: リポジトリを別の PC・場所に置いた同梱のデモ）。"""
        moved = {}
        for name, paths in self._source_paths.items():
            here = self.path.parent / name
            if len(paths) == 1 and not paths[0].exists() and here.is_file():   # 同じ名前が複数ならどれとも決めない
                moved[paths[0]] = here
        if moved:
            self._set_lines([(a, f, line, moved.get(p, p)) for a, f, line, p in self._lines],
                            {name: [moved.get(p, p) for p in paths] for name, paths in self._source_paths.items()})
            self._relocated = {here.name: {"built": built, "used": here} for built, here in moved.items()}

    def relocated(self) -> dict[str, dict[str, Path]]:
        """ビルドした場所に無く、ELF の隣の同じ名前のファイルに置き換えたソース: 名前 → {built（行表の場所）, used}。
        同じ名前なら同じソースとみなしている（中身は確かめていない）。"""
        return {name: dict(v) for name, v in getattr(self, "_relocated", {}).items()}

    def sources(self) -> dict[str, Path]:
        """行番号の情報が参照するソース: ファイル名 → 場所（ビルドした PC の上の場所。無いこともある）。同じ名前の
        別のファイルがあれば、ここには最初に出たものだけ（ほかは source_conflicts）。"""
        return dict(self._sources)

    def source_conflicts(self) -> dict[str, list[Path]]:
        """同じファイル名の別のファイル: 名前 → 場所の一覧（出た順）。"ファイル:行" はディレクトリを添えて選ぶ。"""
        return {name: list(paths) for name, paths in self._source_paths.items() if len(paths) > 1}

    def _source_for(self, file: str) -> Path:
        """"ファイル:行" のファイルの部分 → 行表の場所。名前の同じファイルが複数なら、添えたディレクトリ（後ろから
        合わせる）で 1 つに決まるときだけ。決まらなければ候補を挙げて誤り（先に出たものを選ばない）。"""
        parts = PurePosixPath(file.replace("\\", "/")).parts
        candidates = self._source_paths.get(parts[-1] if parts else file, [])
        if len(parts) > 1:
            candidates = [p for p in candidates if PurePosixPath(p.as_posix()).parts[-len(parts):] == parts]
        if not candidates:
            raise KeyError(f"{file} はこの ELF の行表に無い")
        if len(candidates) > 1:
            raise KeyError(f"{file} は複数のファイルに当たる（ディレクトリを添えて選ぶ）: "
                           + ", ".join(str(p) for p in candidates))
        return candidates[0]

    def lines_of(self, file: str) -> list[int]:
        """そのファイルで命令のある行（ブレークポイントを置ける行）。"""
        path = self._sources.get(file)                                # 同じ名前の 2 つ目のファイルの行は混ぜない
        if self._lines_by_file is None:
            self._lines_by_file = {}
            for _, f, line, p in self._lines:
                self._lines_by_file.setdefault((f, p), set()).add(line)
        return sorted(self._lines_by_file.get((file, path), ()))

    def names(self) -> list[Symbol]:
        """すべての関数・変数のシンボル（ELF の名前。C++・Rust はソースの名前も）。"""
        return [*self._symbols.values(), *(x for v in self._same_named.values() for x in v)]

    def symbol(self, name: str) -> Symbol:
        if name in self._same_named:
            found = [self._symbols[name], *self._same_named[name]]
            raise KeyError(f"{name!r} は同じ名前が複数: " + ", ".join(f"0x{x.address:08x}" for x in found))
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
            return self._line_address(file, int(line))
        return self.symbol(text).address

    def _line_address(self, file: str, line: int) -> int:
        path = self._source_for(file)
        if self._line_index is None:
            index: dict[tuple[Path, int], int] = {}
            for address, _, n, p in self._lines:
                key = (p, n)
                if key not in index or address < index[key]:
                    index[key] = address
            self._line_index = index
        address = self._line_index.get((path, line))
        if address is None:
            raise KeyError(f"{file}:{line} に対応する命令が無い（最適化で消えたか、行が違う）")
        return address

    def after_prologue(self, address: int) -> int:
        """関数の先頭なら、前置き（スタックの用意）の後の番地。引数・ローカル変数が読める最初の場所（関数名の
        ブレークポイントに使う）。行表の prologue_end（コンパイラが書いた事実）があればそれ、無ければ関数の先頭のまま
        （「次の行の始まり」を前置きの終わりと推定しない。GDB 流の経験則はプラグイン fprobe.plugins.heuristics の
        after_first_line）。関数の先頭でなければそのまま。"""
        end = self._function_end_at(address)
        if end is None:
            return address
        j = bisect.bisect_left(self._prologue_ends, address)
        if j < len(self._prologue_ends) and self._prologue_ends[j] < end:
            return self._prologue_ends[j]
        return address

    def has_prologue_end(self, address: int) -> bool | None:
        """関数の先頭 address の関数に、行表の prologue_end があるか（関数の先頭でなければ None）。"""
        end = self._function_end_at(address)
        if end is None:
            return None
        j = bisect.bisect_left(self._prologue_ends, address)
        return j < len(self._prologue_ends) and self._prologue_ends[j] < end

    def _function_end_at(self, address: int) -> int | None:
        """address がちょうど関数の先頭なら、その関数の終わり（無ければ None）。"""
        i = bisect.bisect_right(self._function_starts, address) - 1
        if i < 0 or self._function_starts[i] != address:
            return None
        return address + max(self._functions[i].size, 1)

    def symbol_at(self, address: int) -> tuple[Symbol, int] | None:
        """番地を含むシンボル（関数・変数）と、その先頭からのバイト数。大きさ 0 のシンボルは先頭の番地だけ。"""
        i = bisect.bisect_right(self._all_starts, address) - 1
        while i >= 0 and self._all_starts[i] <= address:
            s = self._all[i]
            if address < s.address + max(s.size, 1):
                return s, address - s.address
            if address - s.address >= self._all_extent:          # これより前のシンボルは届かない（大きさの最大）
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
