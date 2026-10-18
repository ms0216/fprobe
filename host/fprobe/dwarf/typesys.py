"""C の型の模型と、DWARF の DIE からの読み込み。

型は DIE の位置ごとに 1 つだけ作る（自分を指す構造体のため、構造体は先に箱を作ってからメンバーを埋める）。
多次元の配列（1 つの array_type に複数の subrange）は、要素が配列の配列に直す（`a[i][j]` を素直に評価できる）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["ArrayType", "BaseType", "CType", "EnumType", "FunctionType", "Member", "PointerType", "QualifiedType",
           "StructType", "TypeLoader", "TypedefType", "VoidType", "is_char", "strip", "type_name"]

ADDRESS_SIZE = 4


class CType:
    """型の共通の基底。size はバイト数（不完全型・void・関数は None）、name は名前（無名・派生型は None）。"""
    size: int | None
    name: str | None


@dataclass(eq=False)
class VoidType(CType):
    size: int | None = None
    name: str = "void"


@dataclass(eq=False)
class BaseType(CType):
    name: str
    size: int
    encoding: str                 # signed / unsigned / signed_char / unsigned_char / float / bool

    @property
    def signed(self) -> bool:
        return self.encoding in ("signed", "signed_char")


@dataclass(eq=False)
class PointerType(CType):
    target: CType
    size: int = ADDRESS_SIZE
    name: str | None = None


@dataclass(eq=False)
class Member:
    name: str | None              # None = 無名の構造体・共用体（メンバーは外側の名前で引く）
    type: CType
    offset: int                   # 構造体の先頭からのバイト位置（ビットフィールドは bit_offset // 8）
    bit_size: int | None = None   # ビットフィールドのときだけ
    bit_offset: int | None = None # ビットフィールドの、構造体の先頭からのビット位置（リトルエンディアン）


@dataclass(eq=False)
class StructType(CType):
    kind: str                     # struct / union / class
    name: str | None
    size: int | None
    members: list[Member] = field(default_factory=list)

    def member(self, name: str) -> tuple[Member, int]:
        """名前でメンバーを引く（無名のメンバーの中も）。(メンバー, 構造体の先頭からの追加のバイト位置)。"""
        for m in self.members:
            if m.name == name:
                return m, 0
        for m in self.members:
            inner = strip(m.type)
            if m.name is None and isinstance(inner, StructType):
                try:
                    found, extra = inner.member(name)
                except KeyError:
                    continue
                return found, m.offset + extra
        raise KeyError(f"{type_name(self)} にメンバー {name!r} は無い")


@dataclass(eq=False)
class ArrayType(CType):
    element: CType
    length: int | None            # None = 長さが分からない（extern int a[]; や柔軟な配列メンバー）
    name: str | None = None

    @property
    def size(self) -> int | None:  # type: ignore[override]
        if self.length is None or self.element.size is None:
            return None
        return self.length * self.element.size


@dataclass(eq=False)
class EnumType(CType):
    name: str | None
    size: int
    signed: bool
    enumerators: tuple[tuple[str, int], ...]

    def name_of(self, value: int) -> str | None:
        return next((n for n, v in self.enumerators if v == value), None)

    def value_of(self, name: str) -> int:
        for n, v in self.enumerators:
            if n == name:
                return v
        raise KeyError(f"{type_name(self)} に {name!r} は無い")


@dataclass(eq=False)
class TypedefType(CType):
    name: str
    target: CType

    @property
    def size(self) -> int | None:  # type: ignore[override]
        return self.target.size


@dataclass(eq=False)
class QualifiedType(CType):
    qualifier: str                # const / volatile / restrict / atomic
    target: CType
    name: str | None = None

    @property
    def size(self) -> int | None:  # type: ignore[override]
        return self.target.size


@dataclass(eq=False)
class FunctionType(CType):
    result: CType
    parameters: tuple[CType, ...]
    variadic: bool = False
    size: int | None = None
    name: str | None = None


def strip(t: CType) -> CType:
    """typedef と修飾子を外した型。"""
    while isinstance(t, (TypedefType, QualifiedType)):
        t = t.target
    return t


def is_char(t: CType) -> bool:
    """文字の型（char / signed char / unsigned char）。uint8_t・int8_t のような typedef は数として扱うので除く。"""
    while isinstance(t, QualifiedType):
        t = t.target
    return isinstance(t, BaseType) and t.size == 1 and t.encoding in ("signed_char", "unsigned_char")


# --- 型の名前（C の書き方） -------------------------------------------------------------

def type_name(t: CType) -> str:
    return _declare(t, "").strip()


def _declare(t: CType, inner: str) -> str:
    """C の宣言子の書き方: 型 t の「inner」（変数名や * の並び）を宣言する文字列。"""
    if isinstance(t, PointerType):
        target = t.target
        needs_paren = isinstance(target, (ArrayType, FunctionType))
        return _declare(target, f"(*{inner})" if needs_paren else f"*{inner}")
    if isinstance(t, ArrayType):
        return _declare(t.element, f"{inner}[{'' if t.length is None else t.length}]")
    if isinstance(t, FunctionType):
        params = ", ".join(type_name(p) for p in t.parameters) + (", ..." if t.variadic else "")
        return _declare(t.result, f"{inner}({params or 'void'})")
    if isinstance(t, QualifiedType):
        if isinstance(t.target, PointerType):                       # int *const p
            return _declare(t.target, f"{t.qualifier} {inner}".rstrip())
        return f"{t.qualifier} {_declare(t.target, inner)}"
    return f"{_base_name(t)}{' ' + inner if inner and not inner.startswith('[') else inner}"


def _base_name(t: CType) -> str:
    if isinstance(t, StructType):
        return f"{t.kind} {t.name or '<anonymous>'}"
    if isinstance(t, EnumType):
        return f"enum {t.name or '<anonymous>'}"
    return t.name or "?"


# --- DIE からの読み込み ---------------------------------------------------------------------

_ENCODINGS = {0x1: "unsigned", 0x2: "bool", 0x4: "float", 0x5: "signed", 0x6: "signed_char", 0x7: "unsigned",
              0x8: "unsigned_char", 0x10: "unsigned"}       # 0x10 = DW_ATE_UTF（char8_t 等）
_QUALIFIERS = {"DW_TAG_const_type": "const", "DW_TAG_volatile_type": "volatile",
               "DW_TAG_restrict_type": "restrict", "DW_TAG_atomic_type": "atomic"}


def _name(die) -> str | None:
    attr = die.attributes.get("DW_AT_name")
    if attr is None:
        return None
    return attr.value.decode(errors="replace") if isinstance(attr.value, bytes) else str(attr.value)


def _int_attr(die, name: str) -> int | None:
    attr = die.attributes.get(name)
    return None if attr is None or not isinstance(attr.value, int) else attr.value


class TypeLoader:
    """DIE → CType。DIE の位置（.debug_info の中のオフセット）で記憶する。"""

    def __init__(self, address_size: int = ADDRESS_SIZE):
        self.address_size = address_size
        self._cache: dict[int, CType] = {}
        self.void = VoidType()

    def type_of(self, die) -> CType:
        """DIE の DW_AT_type が指す型（無ければ void）。定義の DIE が型を持たず、DW_AT_specification /
        DW_AT_abstract_origin で宣言を指すときは宣言の型（GCC の「extern の宣言 → 定義」。Zephyr の _kernel 等）。"""
        while "DW_AT_type" not in die.attributes:
            for key in ("DW_AT_specification", "DW_AT_abstract_origin"):
                if key in die.attributes:
                    die = die.get_DIE_from_attribute(key)
                    break
            else:
                return self.void
        return self.load(die.get_DIE_from_attribute("DW_AT_type"))

    def load(self, die) -> CType:
        cached = self._cache.get(die.offset)
        if cached is not None:
            return cached
        tag = die.tag
        if tag in ("DW_TAG_structure_type", "DW_TAG_union_type", "DW_TAG_class_type"):
            kind = {"DW_TAG_structure_type": "struct", "DW_TAG_union_type": "union"}.get(tag, "class")
            t = StructType(kind, _name(die), None if die.attributes.get("DW_AT_declaration") else
                           _int_attr(die, "DW_AT_byte_size"))
            self._cache[die.offset] = t                       # メンバーより先に記憶（自分を指すポインタ）
            t.members = [self._member(m) for m in die.iter_children() if m.tag == "DW_TAG_member"]
            return t
        if tag == "DW_TAG_typedef":
            t = TypedefType(_name(die) or "?", self.void)
            self._cache[die.offset] = t
            t.target = self.type_of(die)
            return t
        if tag in ("DW_TAG_pointer_type", "DW_TAG_reference_type", "DW_TAG_rvalue_reference_type"):
            t = PointerType(self.void, _int_attr(die, "DW_AT_byte_size") or self.address_size)
            self._cache[die.offset] = t
            t.target = self.type_of(die)
            return t
        if tag in _QUALIFIERS:
            t = QualifiedType(_QUALIFIERS[tag], self.void)
            self._cache[die.offset] = t
            t.target = self.type_of(die)
            return t
        t = self._simple(die)
        self._cache[die.offset] = t
        return t

    def _simple(self, die) -> CType:
        tag = die.tag
        if tag == "DW_TAG_base_type":
            encoding = _ENCODINGS.get(_int_attr(die, "DW_AT_encoding") or 0, "unsigned")
            return BaseType(_name(die) or "?", _int_attr(die, "DW_AT_byte_size") or 0, encoding)
        if tag == "DW_TAG_enumeration_type":
            values = tuple((_name(c) or "?", c.attributes["DW_AT_const_value"].value)
                           for c in die.iter_children() if c.tag == "DW_TAG_enumerator")
            underlying = strip(self.type_of(die)) if "DW_AT_type" in die.attributes else None
            signed = (underlying.signed if isinstance(underlying, BaseType)
                      else any(v < 0 for _, v in values))
            size = _int_attr(die, "DW_AT_byte_size") or (underlying.size if underlying else 4)
            return EnumType(_name(die), size, signed, values)
        if tag == "DW_TAG_array_type":
            element = self.type_of(die)
            lengths = [self._subrange_length(c) for c in die.iter_children()
                       if c.tag in ("DW_TAG_subrange_type", "DW_TAG_enumeration_type")]
            for length in reversed(lengths or [None]):           # 内側の次元から包む
                element = ArrayType(element, length)
            return element
        if tag == "DW_TAG_subroutine_type":
            params = tuple(self.type_of(c) for c in die.iter_children() if c.tag == "DW_TAG_formal_parameter")
            variadic = any(c.tag == "DW_TAG_unspecified_parameters" for c in die.iter_children())
            return FunctionType(self.type_of(die), params, variadic)
        if tag == "DW_TAG_unspecified_type":
            return VoidType(name=_name(die) or "void")
        return VoidType(name=f"<{tag}>")

    @staticmethod
    def _subrange_length(die) -> int | None:
        count = _int_attr(die, "DW_AT_count")
        if count is not None:
            return count
        upper = _int_attr(die, "DW_AT_upper_bound")
        if upper is None:
            return None
        lower = _int_attr(die, "DW_AT_lower_bound") or 0
        return upper - lower + 1

    def _member(self, die) -> Member:
        t = self.type_of(die)
        location = die.attributes.get("DW_AT_data_member_location")
        offset = location.value if location is not None and isinstance(location.value, int) else 0
        bit_size = _int_attr(die, "DW_AT_bit_size")
        if bit_size is None:
            return Member(_name(die), t, offset)
        data_bit_offset = _int_attr(die, "DW_AT_data_bit_offset")
        if data_bit_offset is None:
            # DWARF 2/3（GCC）: DW_AT_bit_offset は記憶単位（byte_size、無ければ型の大きさ）の最上位からのビット位置
            storage = _int_attr(die, "DW_AT_byte_size") or t.size or 4
            data_bit_offset = offset * 8 + storage * 8 - (_int_attr(die, "DW_AT_bit_offset") or 0) - bit_size
        return Member(_name(die), t, data_bit_offset // 8, bit_size, data_bit_offset)
