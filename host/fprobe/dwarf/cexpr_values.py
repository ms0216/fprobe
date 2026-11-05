"""C の式の値: Value（型 + 場所）と、値を作る・読む・演算する部品（_ValueOps。評価器の mixin）。

整数拡張・通常の算術変換・ポインタ演算・キャスト・メンバ/添字の場所の計算など、構文に依らない「値の扱い」だけを持つ。
構文の解析（_Evaluator 本体）は cexpr.py。使う側の状態（info・frame・skip・context）は _Evaluator が持つ。
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass

from .cexpr_lex import ExpressionError, UnknownNameError
from .location import MemoryPlace, NotAvailable, Place, PiecesPlace, RegisterPlace, Unavailable, ValuePlace, read_place
from .typesys import (ArrayType, BaseType, CType, EnumType, FunctionType, PointerType, StructType, VoidType, strip,
                      type_name)
from .values import decode_bits

__all__ = ["Value"]


@dataclass(frozen=True)
class Value:
    type: CType
    place: Place
    lvalue: bool = True
    bit_offset: int | None = None             # ビットフィールド: place の先頭からのビット位置
    bit_size: int | None = None

    @property
    def address(self) -> int | None:
        return self.place.address if isinstance(self.place, MemoryPlace) else None

    @property
    def is_bitfield(self) -> bool:
        return self.bit_size is not None


class _ValueOps:
    """評価器が持つ状態（info・frame・skip・context）の上で、値を作る・読む・演算する。"""

    def apply(self, op: str, left: Value, right: Value) -> Value:
        """+ - と && || 以外の二項演算。整数どうしは C の整数拡張と通常の算術変換（int と unsigned は unsigned へ）で
        型を決め、その幅で折り返す。"""
        a, b = self.scalar(left), self.scalar(right)
        if self.skip:
            return self.constant(0)
        kind = self.integer_kind(left, right, shift=op in ("<<", ">>"))
        if kind is not None and isinstance(a, int) and isinstance(b, int):
            return self.integer_op(op, a, b, kind, self.integer_kind(right) if op in ("<<", ">>") else kind)
        if op in ("==", "!=", "<", ">", "<=", ">="):
            return self.constant(int({"==": a == b, "!=": a != b, "<": a < b, ">": a > b, "<=": a <= b,
                                      ">=": a >= b}[op]))
        if op == "*":
            return self.number(a * b)
        if op in ("/", "%"):
            if b == 0:
                raise ExpressionError("0 で割った")
            if isinstance(a, float) or isinstance(b, float):
                if op == "%":
                    raise ExpressionError("% は整数だけ")
                return self.number(a / b)
            q = abs(a) // abs(b) * (1 if (a < 0) == (b < 0) else -1)       # C: 0 の方へ切り捨て
            return self.constant(q if op == "/" else a - b * q)
        if isinstance(a, float) or isinstance(b, float):
            raise ExpressionError(f"{op} は整数だけ")
        if op in ("<<", ">>") and not 0 <= b < 64:
            raise ExpressionError(f"シフト量 {b} は 0〜63 の外（C では未定義）")
        return self.constant({"&": a & b, "|": a | b, "^": a ^ b, "<<": a << b, ">>": a >> b}[op])

    def _int_type(self, size: int, signed: bool) -> CType:
        return self._base_type((([] if signed else ["unsigned"]) + (["int"] if size == 4 else ["long", "long"])))

    @staticmethod
    def _promoted(value: Value) -> tuple[int, bool] | None:
        """整数拡張した (大きさ, 符号あり)。整数・列挙・_Bool 以外（浮動小数・ポインタ・配列）は None。"""
        s = strip(value.type)
        if isinstance(s, BaseType) and s.encoding not in ("float", "unknown") and s.size:
            size, signed = s.size, s.signed
        elif isinstance(s, EnumType) and s.size:
            size, signed = s.size, s.signed
        else:
            return None
        if value.is_bitfield and value.bit_size < 32:
            return 4, True
        return (4, True) if size < 4 else (size, signed)

    def integer_kind(self, left: Value, right: Value | None = None, shift: bool = False) -> tuple[int, bool] | None:
        """二項演算の型 (大きさ, 符号あり)。シフトは左の型。整数どうしでなければ None。"""
        a = self._promoted(left)
        if right is None or shift:
            return a
        b = self._promoted(right)
        if a is None or b is None:
            return None
        if a[0] == b[0]:
            return a[0], a[1] and b[1]
        return a if a[0] > b[0] else b

    def integer_op(self, op: str, a: int, b: int, kind: tuple[int, bool], right_kind: tuple[int, bool]) -> Value:
        size, signed = kind
        def wrap(n: int, size: int = size, signed: bool = signed) -> int:
            n &= (1 << (size * 8)) - 1
            return n - (1 << (size * 8)) if signed and n >> (size * 8 - 1) else n
        a = wrap(a)
        if op in ("<<", ">>"):
            if not 0 <= b < size * 8:
                raise ExpressionError(f"シフト量 {b} は 0〜{size * 8 - 1} の外（C では未定義）")
        else:
            b = wrap(b)
        if op in ("==", "!=", "<", ">", "<=", ">="):
            return self.constant(int({"==": a == b, "!=": a != b, "<": a < b, ">": a > b, "<=": a <= b,
                                      ">=": a >= b}[op]))
        if op in ("/", "%"):
            if b == 0:
                raise ExpressionError("0 で割った")
            q = abs(a) // abs(b) * (1 if (a < 0) == (b < 0) else -1)       # C: 0 の方へ切り捨て
            result = q if op == "/" else a - b * q
        else:
            result = {"*": a * b, "&": a & b, "|": a | b, "^": a ^ b, "<<": a << b if b < 1 << 16 else 0,
                      ">>": a >> b}[op]
        return self.rvalue(self._int_type(size, signed), wrap(result))

    def number(self, n: int | float) -> Value:
        return self.rvalue(self._base_type(["double"]), n) if isinstance(n, float) else self.constant(n)

    def _named_type(self, name: str) -> CType:
        try:
            return self.info.type_named(name)
        except KeyError:
            raise ExpressionError(f"型 {name!r} が無い") from None

    def _base_type(self, words: list[str]) -> CType:
        """基本型。DWARF にあればそれ。無ければ大きさを ELF の ABI の事実から: char 1・short 2・int 4・long long 8・
        float 4・double 8・_Bool 1 は ELF の ILP32 / LP64 の ABI（AAPCS・AAPCS64・RISC-V・x86-64 の System V）で同じ、
        long はアドレスの大きさ（ILP32 は 4、LP64 は 8）。long double は ABI で違う（8・12・16）ので無ければ誤り。"""
        if words == ["void"]:
            return VoidType()
        unsigned = "unsigned" in words
        core = [w for w in words if w not in ("signed", "unsigned")]
        longs = core.count("long")
        if "char" in core:
            name, size = ("unsigned char" if unsigned else "signed char" if "signed" in words else "char"), 1
        elif "short" in core:
            name, size = ("unsigned short" if unsigned else "short"), 2
        elif longs == 2:
            name, size = ("unsigned long long" if unsigned else "long long"), 8
        elif longs == 1 and "double" not in core:
            name, size = ("unsigned long" if unsigned else "long"), self.info.address_size
        elif "float" in core:
            name, size = "float", 4
        elif "double" in core:
            name, size = ("long double" if longs else "double"), None if longs else 8
        elif "_Bool" in core:
            name, size = "_Bool", 1
        else:
            name, size = ("unsigned int" if unsigned else "int"), 4
        try:
            return self.info.type_named(name)
        except KeyError:
            if size is None:
                raise ExpressionError(f"型 {name} が DWARF に無い（大きさは ABI で違う）") from None
            encoding = ("float" if name in ("float", "double", "long double") else "bool" if name == "_Bool"
                        else ("unsigned_char" if unsigned else "signed_char") if size == 1 and "char" in name
                        else "unsigned" if unsigned else "signed")
            return BaseType(name, size, encoding)

    # 値
    def variable(self, name: str) -> Value:
        try:
            var = self.info.lookup(name, self.frame.pc if self.frame else None)
        except KeyError as e:
            try:
                t, number = self.info.enumerator(name)              # 変数に無ければ列挙子の名前（定数）
            except KeyError:
                raise UnknownNameError(str(e.args[0])) from None
            return self.rvalue(t, number)
        return Value(var.type, var.place(self.frame))

    def constant(self, number: int | float) -> Value:
        if isinstance(number, float):
            return self.rvalue(self._base_type(["double"]), number)
        for name, size, signed in (("int", 4, True), ("unsigned int", 4, False), ("long long", 8, True),
                                   ("unsigned long long", 8, False)):
            if (-(1 << (size * 8 - 1)) if signed else 0) <= number < (1 << (size * 8 - (1 if signed else 0))):
                return self.rvalue(self._base_type(name.split()), number)
        raise ExpressionError(f"{number} は大きすぎる")

    def rvalue(self, t: CType, number: int | float) -> Value:
        s = strip(t)
        size = s.size
        if not size:
            raise ExpressionError(f"大きさの分からない型 {type_name(t)} の値は作れない")
        if isinstance(s, BaseType) and s.encoding == "float":
            if size not in (4, 8):
                raise ExpressionError(f"大きさ {size} の浮動小数は扱えない")
            try:
                data = struct.pack("<f" if size == 4 else "<d", float(number))
            except OverflowError:                       # C のキャストは範囲外を無限大にする
                data = struct.pack("<f" if size == 4 else "<d", math.copysign(math.inf, number))
        else:
            data = (int(number) & ((1 << (size * 8)) - 1)).to_bytes(size, "little")
        return Value(t, ValuePlace(data), lvalue=False)

    def read(self, value: Value) -> bytes:
        size = value.type.size if not value.is_bitfield else (value.bit_offset + value.bit_size + 7) // 8
        if size is None:
            raise ExpressionError(f"大きさの分からない {type_name(value.type)} は読めない")
        if self.skip:
            return bytes(size)                        # 短絡評価で使わない値: 読まない
        try:
            return read_place(value.place, size, self.context)
        except NotAvailable as e:
            raise ExpressionError(f"値が読めない: {e.reason}") from None

    def scalar(self, value: Value) -> int | float:
        """数として使う（整数・列挙・ポインタ・浮動小数。配列は先頭の番地に崩れる）。"""
        s = strip(value.type)
        if isinstance(s, ArrayType):
            if value.address is None:
                raise ExpressionError("メモリに無い配列は番地にならない")
            return value.address
        if value.is_bitfield:
            return int(decode_bits(value.type, self.read(value), value.bit_offset, value.bit_size)["value"])
        data = self.read(value)
        if isinstance(s, BaseType):
            if s.encoding == "float":
                return struct.unpack("<f" if s.size == 4 else "<d", data)[0]
            if s.encoding == "unknown":
                raise ExpressionError(f"{type_name(value.type)} の符号の種類が分からない（DWARF に無い）")
            return int.from_bytes(data, "little", signed=s.signed)
        if isinstance(s, EnumType):
            if s.size is None:
                raise ExpressionError(f"{type_name(value.type)} の大きさが分からない（DWARF に無い）")
            return int.from_bytes(data, "little", signed=s.signed)
        if isinstance(s, PointerType):
            return int.from_bytes(data, "little")
        raise ExpressionError(f"{type_name(value.type)} は数として使えない")

    def deref(self, value: Value) -> Value:
        s = strip(value.type)
        if isinstance(s, ArrayType):
            return self.index(value, 0)
        if not isinstance(s, PointerType):
            raise ExpressionError(f"{type_name(value.type)} はポインタではない（* / -> は使えない）")
        target = s.target
        if isinstance(strip(target), (VoidType, FunctionType)):
            raise ExpressionError(f"{type_name(value.type)} の先は読めない（キャストする）")
        return Value(target, MemoryPlace(int(self.scalar(value))))

    def address_of(self, value: Value) -> Value:
        if value.is_bitfield or value.address is None:
            raise ExpressionError("& はメモリ上の値だけ（レジスタ・定数・ビットフィールドには番地が無い）")
        return self.rvalue(PointerType(value.type, self.info.address_size), value.address)

    def member(self, value: Value, name: str) -> Value:
        s = strip(value.type)
        if isinstance(s, PointerType):
            raise ExpressionError(f"{type_name(value.type)} はポインタ（-> を使う）")
        if not isinstance(s, StructType):
            raise ExpressionError(f"{type_name(value.type)} は構造体・共用体ではない")
        try:
            m, extra = s.member(name)
        except KeyError as e:
            raise ExpressionError(str(e.args[0])) from None
        if m.bit_size is not None:
            if m.bit_offset is None:
                raise ExpressionError(f"ビットフィールド {name} の位置が分からない（記憶単位の大きさが DWARF に無い）")
            return Value(m.type, self.offset_place(value, extra), value.lvalue, m.bit_offset, m.bit_size)
        return Value(m.type, self.offset_place(value, extra + m.offset), value.lvalue)

    def index(self, value: Value, index: int) -> Value:
        s = strip(value.type)
        if isinstance(s, PointerType):
            element = s.target
            size = element.size
            if size is None:
                raise ExpressionError(f"{type_name(element)} の大きさが分からない")
            return Value(element, MemoryPlace((int(self.scalar(value)) + index * size) & 0xFFFFFFFF))
        if not isinstance(s, ArrayType):
            raise ExpressionError(f"{type_name(value.type)} は配列・ポインタではない")
        if s.length is not None and not 0 <= index < s.length:
            raise ExpressionError(f"添字 {index} は範囲の外（{type_name(value.type)}）")
        size = s.element.size
        if size is None:
            raise ExpressionError(f"{type_name(s.element)} の大きさが分からない")
        return Value(s.element, self.offset_place(value, index * size), value.lvalue)

    def offset_place(self, value: Value, offset: int) -> Place:
        place = value.place
        if isinstance(place, MemoryPlace):
            return MemoryPlace((place.address + offset) & 0xFFFFFFFF)
        if isinstance(place, Unavailable):
            return place
        if isinstance(place, (RegisterPlace, PiecesPlace, ValuePlace)):
            return ValuePlace(self.read(value)[offset:])
        raise ExpressionError("場所が分からない")

    def add(self, left: Value, right: Value, sign: int) -> Value:
        ls, rs = strip(left.type), strip(right.type)
        if isinstance(ls, (PointerType, ArrayType)) and isinstance(rs, (PointerType, ArrayType)) and sign < 0:
            difference = int(self.scalar(left)) - int(self.scalar(right))
            return self.constant(difference // (self._element(ls).size or 1))
        if isinstance(ls, (PointerType, ArrayType)):
            offset = self.scalar(right)
            if not isinstance(offset, int):
                raise ExpressionError("ポインタに足せるのは整数")
            element = self._element(ls)
            pointer = PointerType(element, self.info.address_size)
            return self.rvalue(pointer, int(self.scalar(left)) + sign * offset * (element.size or 1))
        if isinstance(rs, (PointerType, ArrayType)) and sign > 0:
            return self.add(right, left, +1)
        a, b = self.scalar(left), self.scalar(right)
        kind = self.integer_kind(left, right)
        if kind is not None and isinstance(a, int) and isinstance(b, int):
            size, signed = kind
            return self.rvalue(self._int_type(size, signed), a + sign * b)    # rvalue が型の幅で折り返す
        result = a + sign * b
        if isinstance(result, float):
            return self.rvalue(self._base_type(["double"]), result)
        return self.constant(result)

    @staticmethod
    def _element(t: CType) -> CType:
        return t.target if isinstance(t, PointerType) else t.element

    def arith(self, value: Value, op, symbol: str, integer_only: bool = False) -> Value:
        number = self.scalar(value)
        if integer_only and not isinstance(number, int):
            raise ExpressionError(f"{symbol} は整数だけ")
        result = op(number)
        kind = self._promoted(value)
        if kind is not None and isinstance(result, int):
            return self.rvalue(self._int_type(*kind), result)
        if isinstance(result, float):
            return self.rvalue(value.type, result)
        return self.constant(result)

    def cast(self, target: CType, value: Value) -> Value:
        s = strip(target)
        if isinstance(s, (StructType, ArrayType, FunctionType)):
            raise ExpressionError(f"{type_name(target)} へはキャストできない（数・ポインタだけ。*(T *)&x を使う）")
        if isinstance(s, VoidType):
            return Value(target, Unavailable("void"), lvalue=False)
        number = self.scalar(value)
        if isinstance(s, BaseType) and s.encoding == "float":
            return self.rvalue(target, float(number))
        if isinstance(s, BaseType) and s.encoding == "bool":
            return self.rvalue(target, int(bool(number)))
        return self.rvalue(target, int(number))
