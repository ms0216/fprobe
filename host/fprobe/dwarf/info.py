"""ELF の DWARF の索引: 関数（番地の範囲）・スコープ（字句ブロック）・変数（大域・静的・引数・ローカル）・型の名前。

    info = DebugInfo("app.elf")
    info.function_at(pc)                  # Function（名前・範囲・フレームの基底）
    info.scope_at(pc)                     # Scope（引数・ローカル変数。内側のブロックほど後）
    info.lookup("total", pc)              # 名前 → Variable（スコープ → CU の static → 大域）
    info.type_named("struct Sensor")      # 型の名前 → CType
    var.place(frame)                      # 位置式（ロケーションリストは PC で選ぶ）を評価した場所

索引は初めて使うときに作る（大きな ELF でも開くだけなら速い）。ELF はメモリに読む（ファイルを開いたままにしない）。
"""

from __future__ import annotations

import bisect
import io
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from elftools.dwarf.dwarf_expr import DWARFExprParser
from elftools.dwarf.locationlists import BaseAddressEntry, LocationEntry, LocationExpr, LocationParser
from elftools.dwarf.ranges import BaseAddressEntry as RangeBaseEntry
from elftools.elf.elffile import ELFFile

from .access import AccessError
from .location import (EvalContext, NotAvailable, Place, Unavailable, ValuePlace, evaluate_location,
                       evaluate_value)
from .typesys import BaseType, CType, EnumType, TypeLoader, strip

__all__ = ["AmbiguousNameError", "DebugInfo", "FrameView", "Function", "Scope", "Variable"]



_FORM_SIZES = {"DW_FORM_data1": 1, "DW_FORM_data2": 2, "DW_FORM_data4": 4, "DW_FORM_data8": 8, "DW_FORM_data16": 16}


def _const_bytes(value: int, t, form: str | None) -> bytes:
    """DW_AT_const_value の整数 → 型の大きさのバイト列。型に大きさが無ければ属性の形式の大きさ（data1〜data16）、
    それも無ければ（sdata・udata）値の入る最小のバイト数（4 バイト等で補わない。値は欠けない）。"""
    size = t.size or _FORM_SIZES.get(form)
    if size is None:
        size = max(1, (value.bit_length() + (8 if value < 0 else 7)) // 8)
    signed = (isinstance(t, (BaseType, EnumType)) and t.signed) or value < 0
    if signed or value >= 0:
        return value.to_bytes(size, "little", signed=signed)
    return (value & ((1 << size * 8) - 1)).to_bytes(size, "little")


class AmbiguousNameError(LookupError):
    """同じ名前の変数が複数あって 1 つに決まらない（黙って 1 つを選ばない）。candidates = 候補（name・kind・
    function・file・line）。"<ファイル>::<名前>"・"<ファイル>::<関数>::<名前>" で選べる。"""

    def __init__(self, name: str, candidates: list[Variable]):
        self.name = name
        self.candidates = [{"name": v.name, "kind": v.kind, "function": v.function, "file": v.decl_file,
                            "line": v.decl_line} for v in candidates]
        where = ", ".join(f"{c['file'] or '?'}:{c['line'] or '?'}" for c in self.candidates)
        examples = " / ".join(dict.fromkeys(
            f"{c['file'] or '?'}::{c['function'] + '::' if c['function'] else ''}{c['name']}" for c in self.candidates))
        super().__init__(f"{name!r} は {len(candidates)} 個ある（{where}）。ファイルで選ぶ: {examples}")

    def __str__(self) -> str:                    # KeyError と違い、引用符を付けずに文で
        return self.args[0]


def _in_file(variable: Variable, file: str) -> bool:
    """変数の宣言のファイルが file（パスの末尾の一致。区切りは / と \\ を同じに）か。"""
    path = (variable.decl_file or "").replace("\\", "/")
    want = file.replace("\\", "/")
    return path == want or path.endswith("/" + want)


def _location_key(variable: Variable) -> object:
    """同じ実体かを見分ける鍵: 位置式のバイト列（番地を含む）。無ければその変数だけ。"""
    attributes = getattr(variable.die, "attributes", None) or {}
    attr = attributes.get("DW_AT_location")
    return tuple(attr.value) if attr is not None and isinstance(attr.value, (list, bytes)) else id(variable)


def _is_file(qualifier: str) -> bool:
    """"<ファイル>::<名前>" の前置きか（C の名前に無い . / \\ を含む）。"""
    return any(ch in qualifier for ch in "./\\")


@dataclass
class FrameView:
    """変数の場所を評価するための、1 つのフレームの様子。

    pc: 変数・スコープを引く番地（呼び出し元のフレームでは戻り先 − 1 = 呼び出し命令の中）。
    read_register: DWARF のレジスタ番号 → 値（復元できなければ AccessError / NotAvailable）。
    """
    pc: int | None
    read_memory: Callable[[int, int], bytes]
    read_register: Callable[[int], int]
    cfa: int | None = None


def _no_register(number: int) -> int:
    raise NotAvailable("フレームの外（レジスタが無い）")


@dataclass(eq=False)
class Variable:
    name: str
    kind: str                     # global / static / parameter / local
    die: object = field(repr=False)
    info: "DebugInfo" = field(repr=False)
    function: str | None = None
    decl_file: str | None = None
    decl_line: int | None = None

    @property
    def type(self) -> CType:
        return self.info.types.type_of(self.die)

    def place(self, frame: FrameView | None = None) -> Place:
        return self.info.place_of(self.die, frame)

    def __str__(self) -> str:
        return f"{self.kind} {self.name}"


@dataclass(eq=False)
class Function:
    name: str
    ranges: tuple[tuple[int, int], ...]
    die: object = field(repr=False)
    decl_file: str | None = None
    decl_line: int | None = None

    @property
    def low(self) -> int:
        return min(lo for lo, _ in self.ranges)

    def contains(self, pc: int) -> bool:
        return any(lo <= pc < hi for lo, hi in self.ranges)


@dataclass
class Scope:
    """PC から見える関数の変数。locals は外側のブロックから内側の順（同じ名前は内側が勝つ）。"""
    function: Function
    parameters: list[Variable]
    locals: list[Variable]
    statics: list[Variable]       # 関数の中の static

    def lookup(self, name: str) -> Variable | None:
        for v in reversed(self.locals):
            if v.name == name:
                return v
        for v in self.parameters + self.statics:
            if v.name == name:
                return v
        return None


def _text(value) -> str:
    return value.decode(errors="replace") if isinstance(value, bytes) else str(value)


class DebugInfo:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._elf = ELFFile(io.BytesIO(self.path.read_bytes()))
        if not self._elf.has_dwarf_info():
            raise ValueError(f"{self.path.name} にデバッグ情報（DWARF）が無い")
        self.dwarf = self._elf.get_dwarf_info()
        self.address_size = self._elf.elfclass // 8
        self.types = TypeLoader(self.address_size)
        self._locations = LocationParser(self.dwarf.location_lists())
        self._parsers: dict[int, DWARFExprParser] = {}
        self._indexed = False
        self._file_name_cache: dict[int, dict[int, str]] = {}
        self._functions: list[Function] = []
        self._starts: list[int] = []
        self._globals: dict[str, list[Variable]] = {}
        self._function_statics: dict[str, list[Variable]] = {}
        self._type_dies: dict[str, object] = {}
        self._enumerator_dies: dict[str, object] = {}              # 列挙子の名前 → その列挙型の DIE（無名の型も）

    @property
    def elf(self) -> ELFFile:
        return self._elf

    # --- 索引 -------------------------------------------------------------------------

    def _index(self) -> None:
        if self._indexed:
            return
        self._indexed = True
        entries: list[tuple[int, Function]] = []
        for cu in self.dwarf.iter_CUs():
            top = cu.get_top_DIE()
            files = self._file_names(cu)
            for die in top.iter_children():
                self._index_die(die, files, entries)
        entries.sort(key=lambda e: e[0])
        self._functions = [f for _, f in entries]
        self._starts = [lo for lo, _ in entries]

    def _index_die(self, die, files, entries) -> None:
        tag = die.tag
        if tag == "DW_TAG_subprogram":
            ranges = self._ranges(die)
            name = self._die_name(die)
            if ranges and name:
                function = Function(name, tuple(ranges), die, *self._decl(die, files))
                for lo, _ in ranges:
                    entries.append((lo, function))
                statics = [self._variable(c, "static", files, name) for c in self._walk_variables(die)
                           if c.tag == "DW_TAG_variable" and self._is_static_location(c)]
                for v in statics:
                    self._function_statics.setdefault(name, []).append(v)
        elif tag == "DW_TAG_variable":
            if "DW_AT_location" in die.attributes or "DW_AT_const_value" in die.attributes:
                name = self._die_name(die)
                if name:
                    spec = self._specification(die)
                    external = bool(die.attributes.get("DW_AT_external")) or bool(
                        spec is not None and spec.attributes.get("DW_AT_external"))
                    kind = "global" if external else "static"
                    self._globals.setdefault(name, []).append(self._variable(die, kind, files, None))
        elif tag in ("DW_TAG_structure_type", "DW_TAG_union_type", "DW_TAG_enumeration_type", "DW_TAG_class_type",
                     "DW_TAG_typedef", "DW_TAG_base_type"):
            name = self._die_name(die)
            if name and not die.attributes.get("DW_AT_declaration"):
                prefix = {"DW_TAG_structure_type": "struct ", "DW_TAG_union_type": "union ",
                          "DW_TAG_enumeration_type": "enum ", "DW_TAG_class_type": "class "}.get(tag, "")
                self._type_dies.setdefault(prefix + name, die)
            if tag == "DW_TAG_enumeration_type" and not die.attributes.get("DW_AT_declaration"):
                for child in die.iter_children():
                    if child.tag == "DW_TAG_enumerator" and (n := self._die_name(child)):
                        self._enumerator_dies.setdefault(n, die)
        elif tag == "DW_TAG_namespace":
            for child in die.iter_children():
                self._index_die(child, files, entries)

    @staticmethod
    def _walk_variables(die):
        """関数の中の変数・引数（字句ブロックの中も。入れ子の関数・インライン展開の中は除く）。"""
        for child in die.iter_children():
            if child.tag in ("DW_TAG_variable", "DW_TAG_formal_parameter"):
                yield child
            elif child.tag == "DW_TAG_lexical_block":
                yield from DebugInfo._walk_variables(child)

    def _is_static_location(self, die) -> bool:
        attr = die.attributes.get("DW_AT_location")
        is_expression = attr is not None and (attr.form == "DW_FORM_exprloc" or attr.form.startswith("DW_FORM_block"))
        if not is_expression or not attr.value:                 # DWARF 2/3 の位置式は block の形
            return False
        return attr.value[0] in (0x03, 0xA1, 0xFB)       # DW_OP_addr / addrx / GNU_addr_index

    def _variable(self, die, kind, files, function) -> Variable:
        return Variable(self._die_name(die) or "?", kind, die, self, function, *self._decl(die, files))

    @staticmethod
    def _specification(die):
        for key in ("DW_AT_specification", "DW_AT_abstract_origin"):
            if key in die.attributes:
                return die.get_DIE_from_attribute(key)
        return None

    def _die_name(self, die) -> str | None:
        attr = die.attributes.get("DW_AT_name")
        if attr is not None:
            return _text(attr.value)
        spec = self._specification(die)
        return self._die_name(spec) if spec is not None else None

    def _decl(self, die, files) -> tuple[str | None, int | None]:
        f = die.attributes.get("DW_AT_decl_file")
        line = die.attributes.get("DW_AT_decl_line")
        name = files.get(f.value) if f is not None else None
        return name, line.value if line is not None else None

    def _file_names(self, cu) -> dict[int, str]:
        """CU のファイル番号 → ファイル名。CU ごとに 1 回だけ作る（scope_at が関数ごとに引くため）。
        返す辞書は共有なので呼び出し側は書き換えない。"""
        names = self._file_name_cache.get(cu.cu_offset)
        if names is None:
            program = self.dwarf.line_program_for_CU(cu)
            if program is None:
                names = {}
            else:
                base = 1 if program.header["version"] < 5 else 0
                names = {i + base: Path(_text(e.name)).name for i, e in enumerate(program["file_entry"])}
            self._file_name_cache[cu.cu_offset] = names
        return names

    # --- 番地の範囲 -------------------------------------------------------------------------

    def _ranges(self, die) -> list[tuple[int, int]]:
        attrs = die.attributes
        if "DW_AT_low_pc" in attrs and "DW_AT_high_pc" in attrs:
            lo = attrs["DW_AT_low_pc"].value
            high = attrs["DW_AT_high_pc"]
            hi = high.value if high.form in ("DW_FORM_addr", "DW_FORM_addrx", "DW_FORM_addrx1", "DW_FORM_addrx2",
                                             "DW_FORM_addrx3", "DW_FORM_addrx4") else lo + high.value
            return [(lo, hi)]
        if "DW_AT_ranges" in attrs:
            lists = self.dwarf.range_lists()
            if lists is None:
                return []
            base = self._cu_base(die.cu)
            out = []
            for entry in lists.get_range_list_at_offset(attrs["DW_AT_ranges"].value, cu=die.cu):
                if isinstance(entry, RangeBaseEntry):
                    base = entry.base_address
                elif entry.is_absolute:
                    out.append((entry.begin_offset, entry.end_offset))
                else:
                    out.append((base + entry.begin_offset, base + entry.end_offset))
            return [(lo, hi) for lo, hi in out if hi > lo]
        return []

    @staticmethod
    def _cu_base(cu) -> int:
        low = cu.get_top_DIE().attributes.get("DW_AT_low_pc")
        return low.value if low is not None else 0

    # --- 引く ---------------------------------------------------------------------------

    def functions(self) -> list[Function]:
        self._index()
        return list(dict.fromkeys(self._functions))

    def function_at(self, pc: int) -> Function | None:
        self._index()
        i = bisect.bisect_right(self._starts, pc) - 1     # 範囲ごとの項目（分かれた関数は範囲の数だけ）
        if i >= 0 and self._functions[i].contains(pc):
            return self._functions[i]
        return None

    def scope_at(self, pc: int) -> Scope | None:
        function = self.function_at(pc)
        if function is None:
            return None
        files = self._file_names(function.die.cu)
        parameters: list[Variable] = []
        locals_: list[Variable] = []
        statics: list[Variable] = []

        def visit(die, depth: int) -> None:
            for child in die.iter_children():
                if child.tag == "DW_TAG_formal_parameter" and depth == 0:
                    parameters.append(self._variable(child, "parameter", files, function.name))
                elif child.tag == "DW_TAG_variable":
                    if self._is_static_location(child):
                        statics.append(self._variable(child, "static", files, function.name))
                    else:
                        locals_.append(self._variable(child, "local", files, function.name))
                elif child.tag == "DW_TAG_lexical_block":
                    ranges = self._ranges(child)
                    if not ranges or any(lo <= pc < hi for lo, hi in ranges):
                        visit(child, depth + 1)
        visit(function.die, 0)
        return Scope(function, parameters, locals_, statics)

    def global_variable(self, name: str) -> Variable:
        """大域・ファイルの static。外部結合（global）が 1 つあればそれ（C のプログラム全体でその名前が指すもの）。
        外部結合が無く static が複数あれば選ばずに AmbiguousNameError（候補の一覧）。
        "関数名::名前" で関数の中の static、"ファイル::名前"・"ファイル::関数名::名前" でファイルを選ぶ（ファイルは
        宣言のファイルのパスの末尾）。同じ名前の関数が複数あるときも選ばない。"""
        self._index()
        *qualifiers, inner = name.split("::")
        if len(qualifiers) > 2 or (len(qualifiers) == 2 and not _is_file(qualifiers[0])):
            raise KeyError(f"{name!r} は 名前・関数::名前・ファイル::名前・ファイル::関数::名前 のどれか")
        file = qualifiers[0] if qualifiers and _is_file(qualifiers[0]) else None
        function = qualifiers[-1] if qualifiers and not _is_file(qualifiers[-1]) else None
        if function is not None:
            found = [v for v in self._function_statics.get(function, [])
                     if v.name == inner and (file is None or _in_file(v, file))]
            if not found:
                raise KeyError(f"関数 {function} の中に static {inner!r} は無い" + (f"（{file}）" if file else ""))
            return self._one(name, found)
        found = [v for v in self._globals.get(inner, []) if file is None or _in_file(v, file)]
        if not found:
            raise KeyError(f"変数 {inner!r} が {self.path.name} の DWARF に無い" + (f"（{file}）" if file else ""))
        # 外部結合の同じ実体が複数の CU にあることがある（-fcommon の仮定義等）: 同じ位置式は 1 つに数える
        same: dict[object, Variable] = {}
        for v in found:
            if v.kind == "global":
                same.setdefault(_location_key(v), v)
        external = list(same.values())
        return self._one(name, external if len(external) == 1 else found)

    @staticmethod
    def _one(name: str, found: list[Variable]) -> Variable:
        """候補が 1 つならそれ。複数なら選ばない（AmbiguousNameError）。"""
        if len(found) > 1:
            raise AmbiguousNameError(name, found)
        return found[0]

    def global_variables(self) -> list[Variable]:
        self._index()
        return [v for vs in self._globals.values() for v in vs]

    def lookup(self, name: str, pc: int | None = None) -> Variable:
        """名前を、PC のスコープ（内側のブロック → 引数 → 関数の static）→ 同じ CU の static → 大域 の順に引く。"""
        if pc is not None and "::" not in name:
            scope = self.scope_at(pc)
            if scope is not None:
                found = scope.lookup(name)
                if found is not None:
                    return found
                same_cu = [v for v in self._globals.get(name, []) if v.die.cu is scope.function.die.cu]
                if same_cu:
                    return sorted(same_cu, key=lambda v: v.kind != "static")[0]
        return self.global_variable(name)

    def type_named(self, name: str) -> CType:
        """"struct Sensor" / "union Word" / "enum Mode" / typedef 名 / 基本型の名前（"unsigned int" 等）。"""
        self._index()
        key = " ".join(name.split())
        die = self._type_dies.get(key)
        if die is None:
            raise KeyError(f"型 {name!r} が {self.path.name} の DWARF に無い")
        return self.types.load(die)

    def enumerator(self, name: str) -> tuple[EnumType, int]:
        """列挙子の名前（MODE_RUN 等）→（その列挙型, 値）。式で定数として使う。"""
        self._index()
        die = self._enumerator_dies.get(name)
        if die is None:
            raise KeyError(f"列挙子 {name!r} が {self.path.name} の DWARF に無い")
        t = self.types.load(die)
        return t, strip(t).value_of(name)

    def type_names(self) -> list[str]:
        self._index()
        return sorted(self._type_dies)

    # --- 場所 ---------------------------------------------------------------------------------

    def parser(self, cu) -> DWARFExprParser:
        parser = self._parsers.get(cu.cu_offset)
        if parser is None:
            parser = self._parsers[cu.cu_offset] = DWARFExprParser(cu.structs)
        return parser

    def place_of(self, die, frame: FrameView | None) -> Place:
        """DIE の DW_AT_location（または DW_AT_const_value）を、フレームの様子で評価した場所。"""
        attrs = die.attributes
        if "DW_AT_const_value" in attrs:
            return self._const_place(die)
        attr = attrs.get("DW_AT_location")
        if attr is None:
            return Unavailable("DWARF に位置（DW_AT_location）が無い")
        ops = self.location_ops(attr, die, frame.pc if frame else None)
        if ops is None:
            return Unavailable("この番地は位置リストの範囲の外（DWARF にこの番地での位置が無い）")
        return evaluate_location(ops, self.eval_context(die, frame))

    def location_ops(self, attr, die, pc: int | None):
        """位置式の命令の並び（ロケーションリストは pc で選ぶ。合うものが無ければ None）。"""
        cu = die.cu
        location = self._locations.parse_from_attribute(attr, cu.header.version, die)
        if isinstance(location, LocationExpr):
            return self.parser(cu).parse_expr(location.loc_expr)
        if pc is None:
            return None
        base = self._cu_base(cu)
        default = None
        for entry in location:
            if isinstance(entry, BaseAddressEntry):
                base = entry.base_address
            elif isinstance(entry, LocationEntry):
                if entry.begin_offset == -1 and entry.end_offset == -1:
                    default = entry
                    continue
                lo, hi = ((entry.begin_offset, entry.end_offset) if entry.is_absolute
                          else (base + entry.begin_offset, base + entry.end_offset))
                if lo <= pc < hi:
                    return self.parser(cu).parse_expr(entry.loc_expr)
        return self.parser(cu).parse_expr(default.loc_expr) if default is not None else None

    def _const_place(self, die) -> Place:
        attr = die.attributes["DW_AT_const_value"]
        if isinstance(attr.value, int):
            return ValuePlace(_const_bytes(attr.value, strip(self.types.type_of(die)), attr.form))
        return ValuePlace(bytes(attr.value))

    def eval_context(self, die, frame: FrameView | None) -> EvalContext:
        cu = die.cu
        frame = frame or FrameView(None, _unreadable, _no_register)
        function = self._enclosing_function(die)
        frame_base = None
        if function is not None and "DW_AT_frame_base" in function.attributes:
            base_attr = function.attributes["DW_AT_frame_base"]

            def frame_base() -> int:
                ops = self.location_ops(base_attr, function, frame.pc)
                if ops is None:
                    raise NotAvailable("フレームの基底が分からない")
                return evaluate_value(ops, self.eval_context(function, frame))
        return EvalContext(frame.read_memory, frame.read_register, frame_base, frame.cfa,
                           lambda index: self.dwarf.get_addr(cu, index), lambda offset: self._base_type(cu, offset))

    def _base_type(self, cu, offset: int) -> tuple[int, bool]:
        die = self.dwarf.get_DIE_from_refaddr(cu.cu_offset + offset, cu)
        t = self.types.load(die)
        return (t.size, t.signed) if isinstance(t, BaseType) else (self.address_size, False)

    @staticmethod
    def _enclosing_function(die):
        node = die
        while node is not None:
            if node.tag == "DW_TAG_subprogram":
                return node
            node = node.get_parent()
        return None


def _unreadable(address: int, length: int) -> bytes:
    raise AccessError("メモリの読み手が無い")
