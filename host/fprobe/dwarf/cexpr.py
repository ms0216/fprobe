"""C の簡単な式を、DWARF の型付きで評価する。

    expr    := unary (('+' | '-') unary)*
    unary   := ('*' | '&' | '-' | '~' | '!') unary | '(' 型名 ')' unary | postfix
    postfix := primary ('[' expr ']' | '.' 名前 | '->' 名前)*
    primary := 名前 ('::' 名前)? | 整数（10 進・0x・0b・'c'） | '(' expr ')'
    型名    := (const|volatile)* (struct|union|enum 名前 | 基本型の語 | typedef 名) ('*' (const|volatile)*)*

名前は選んだフレームのスコープ → その CU の static → 大域 の順に引く（DebugInfo.lookup）。
`関数名::名前` で関数の中の static。結果は Value（型 + 場所）で、読み出し・解読・書き込みは呼ぶ側（Inspector）が行う。
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass

from .access import AccessError
from .info import DebugInfo, FrameView
from .location import (EvalContext, MemoryPlace, NotAvailable, Place, PiecesPlace, RegisterPlace, Unavailable,
                       ValuePlace, read_place)
from .typesys import (ArrayType, BaseType, CType, EnumType, FunctionType, PointerType, QualifiedType, StructType,
                      VoidType, strip, type_name)
from .values import decode_bits

__all__ = ["ExpressionError", "UnknownNameError", "Value", "evaluate", "evaluate_number"]


class ExpressionError(ValueError):
    """式が書けていない・型が合わない・名前が無い。"""


class UnknownNameError(ExpressionError):
    """名前が変数にも列挙子にも無い（引いたスコープに無い。PC の無い評価ではローカル変数もこれ）。"""


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


# --- 字句 ------------------------------------------------------------------------------------------

_TOKEN = re.compile(r"""
    \s*(?:
      (?P<float>\d+\.\d*(?:[eE][+-]?\d+)?|\d+[eE][+-]?\d+)[fF]?
    | (?P<number>0[xX][0-9a-fA-F]+|0[bB][01]+|\d+)[uUlL]*
    | (?P<char>'(?:\\.|[^\\'])')
    | (?P<name>[A-Za-z_]\w*)
    | (?P<op>->|::|\|\||&&|==|!=|<=|>=|<<|>>|[.\[\]()*&+\-~!<>|^/%])
    )""", re.VERBOSE)
_ESCAPES = {"n": 10, "t": 9, "r": 13, "0": 0, "\\": 92, "'": 39, '"': 34, "a": 7, "b": 8, "f": 12, "v": 11}
_BASE_WORDS = {"void", "char", "short", "int", "long", "float", "double", "signed", "unsigned", "_Bool"}
_QUALIFIER_WORDS = {"const", "volatile"}
_TAG_WORDS = {"struct", "union", "enum"}


def _tokens(text: str) -> list[tuple[str, object]]:
    out: list[tuple[str, object]] = []
    pos = 0
    text = text.rstrip()
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if m is None or m.end() == pos:
            raise ExpressionError(f"式の {pos + 1} 文字目が読めない: {text[pos:pos + 10]!r}")
        pos = m.end()
        if m["float"]:
            out.append(("number", float(m["float"])))
        elif m["number"]:
            n = m["number"].lower()
            out.append(("number", int(n, 16) if n.startswith("0x") else int(n[2:], 2) if n.startswith("0b")
                        else int(n, 10)))
        elif m["char"]:
            body = m["char"][1:-1]
            out.append(("number", _ESCAPES.get(body[1], ord(body[1])) if body.startswith("\\") else ord(body)))
        elif m["name"]:
            out.append(("name", m["name"]))
        else:
            out.append(("op", m["op"]))
    out.append(("end", None))
    return out


# --- 評価 ------------------------------------------------------------------------------------------

def evaluate(text: str, info: DebugInfo, frame: FrameView | None = None) -> Value:
    """式を評価する。frame はローカル変数を引くフレーム（None なら大域だけ）。"""
    return _Evaluator(info, frame, _tokens(text)).run()


def evaluate_number(text: str, info: DebugInfo, frame: FrameView | None = None) -> tuple[CType, int | float]:
    """式を評価して数にする（整数・列挙・ポインタ・浮動小数。配列は先頭の番地）: (式の型, 値)。関数の引数等に使う。"""
    evaluator = _Evaluator(info, frame, _tokens(text))
    value = evaluator.run()
    return value.type, evaluator.scalar(value)


class _Evaluator:
    def __init__(self, info: DebugInfo, frame: FrameView | None, tokens):
        self.info = info
        self.frame = frame
        self.tokens = tokens
        self.i = 0
        self.skip = 0                                 # > 0: 短絡評価で値を使わない右辺（メモリを読まない）
        memory = frame.read_memory if frame else _no_memory
        registers = frame.read_register if frame else _no_register
        self.context = EvalContext(memory, registers)

    # 字句の操作
    def peek(self, k: int = 0):
        return self.tokens[min(self.i + k, len(self.tokens) - 1)]

    def take(self):
        token = self.tokens[self.i]
        self.i += 1
        return token

    def accept(self, op: str) -> bool:
        if self.peek() == ("op", op):
            self.i += 1
            return True
        return False

    def expect(self, op: str) -> None:
        if not self.accept(op):
            raise ExpressionError(f"{op!r} が要る（{self._where()}）")

    def _where(self) -> str:
        kind, value = self.peek()
        return "式の終わり" if kind == "end" else f"{value!r} の前"

    def run(self) -> Value:
        value = self.expression()
        if self.peek()[0] != "end":
            raise ExpressionError(f"式の終わりに余りがある（{self._where()}）")
        return value

    # 構文（二項演算子は C の優先順位。弱い順）
    _LEVELS = (("||",), ("&&",), ("|",), ("^",), ("&",), ("==", "!="), ("<", ">", "<=", ">="), ("<<", ">>"),
               ("+", "-"), ("*", "/", "%"))

    def expression(self) -> Value:
        return self.binary(0)

    def binary(self, level: int) -> Value:
        if level == len(self._LEVELS):
            return self.unary()
        ops = self._LEVELS[level]
        left = self.binary(level + 1)
        while True:
            op = next((o for o in ops if self.accept(o)), None)
            if op is None:
                return left
            if op in ("&&", "||"):
                lv = bool(self.scalar(left))
                decided = lv if op == "||" else not lv            # 左で決まる: 右は構文だけ読み、値は使わない
                self.skip += decided
                try:
                    right = self.binary(level + 1)
                finally:
                    self.skip -= decided
                left = self.constant(int(lv if decided else bool(self.scalar(right))))
            elif op in ("+", "-"):
                left = self.add(left, self.binary(level + 1), +1 if op == "+" else -1)
            else:
                left = self.apply(op, left, self.binary(level + 1))

    def apply(self, op: str, left: Value, right: Value) -> Value:
        """+ - と && || 以外の二項演算（ponytail: 整数は数学の整数で計算する。unsigned の折り返しは見ない）。"""
        a, b = self.scalar(left), self.scalar(right)
        if self.skip:
            return self.constant(0)
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
        return self.constant({"&": a & b, "|": a | b, "^": a ^ b, "<<": a << b, ">>": a >> b}[op])

    def number(self, n: int | float) -> Value:
        return self.rvalue(self._base_type(["double"]), n) if isinstance(n, float) else self.constant(n)

    def unary(self) -> Value:
        if self.accept("*"):
            return self.deref(self.unary())
        if self.accept("&"):
            return self.address_of(self.unary())
        if self.accept("-"):
            return self.arith(self.unary(), lambda v: -v, "-")
        if self.accept("~"):
            return self.arith(self.unary(), lambda v: ~v, "~", integer_only=True)
        if self.accept("!"):
            operand = self.scalar(self.unary())
            return self.constant(int(not operand))
        if self.peek() == ("op", "(") and self.is_type_start(1):
            self.take()
            target = self.type_name()
            self.expect(")")
            return self.cast(target, self.unary())
        return self.postfix()

    def postfix(self) -> Value:
        value = self.primary()
        while True:
            if self.accept("["):
                index = self.scalar(self.expression())
                self.expect("]")
                if not isinstance(index, int):
                    raise ExpressionError("添字は整数")
                value = self.index(value, index)
            elif self.accept("."):
                value = self.member(value, self.name())
            elif self.accept("->"):
                value = self.member(self.deref(value), self.name())
            else:
                return value

    def primary(self) -> Value:
        kind, token = self.take()
        if kind == "number":
            return self.constant(token)
        if kind == "name":
            name = token
            if self.accept("::"):
                name = f"{name}::{self.name()}"
            return self.variable(name)
        if (kind, token) == ("op", "("):
            value = self.expression()
            self.expect(")")
            return value
        raise ExpressionError(f"値が要る（{token!r}）" if kind != "end" else "式が途中で終わっている")

    def name(self) -> str:
        kind, token = self.take()
        if kind != "name":
            raise ExpressionError(f"名前が要る（{token!r}）")
        return token

    # 型名
    def is_type_start(self, k: int) -> bool:
        kind, token = self.peek(k)
        if kind != "name":
            return False
        if token in _BASE_WORDS or token in _QUALIFIER_WORDS or token in _TAG_WORDS:
            return True
        if self.is_variable(token):
            return False
        try:
            self.info.type_named(token)
            return True
        except KeyError:
            return False

    def is_variable(self, name: str) -> bool:
        try:
            self.info.lookup(name, self.frame.pc if self.frame else None)
            return True
        except KeyError:
            return False

    def type_name(self) -> CType:
        qualifiers: list[str] = []
        words: list[str] = []
        base: CType | None = None
        while self.peek()[0] == "name":
            token = self.peek()[1]
            if token in _QUALIFIER_WORDS:
                qualifiers.append(self.take()[1])
            elif token in _TAG_WORDS and base is None and not words:
                self.take()
                tag = f"{token} {self.name()}"
                base = self._named_type(tag)
            elif token in _BASE_WORDS and base is None:
                words.append(self.take()[1])
            elif base is None and not words:
                base = self._named_type(self.take()[1])
            else:
                break
        if base is None:
            if not words:
                raise ExpressionError(f"型名が要る（{self._where()}）")
            base = self._base_type(words)
        t: CType = base
        for q in qualifiers:
            t = QualifiedType(q, t)
        while self.accept("*"):
            t = PointerType(t, self.info.address_size)
            while self.peek()[0] == "name" and self.peek()[1] in _QUALIFIER_WORDS:
                t = QualifiedType(self.take()[1], t)
        return t

    def _named_type(self, name: str) -> CType:
        try:
            return self.info.type_named(name)
        except KeyError:
            raise ExpressionError(f"型 {name!r} が無い") from None

    def _base_type(self, words: list[str]) -> CType:
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
            name, size = ("unsigned long" if unsigned else "long"), 4
        elif "float" in core:
            name, size = "float", 4
        elif "double" in core:
            name, size = ("long double" if longs else "double"), 8
        elif "_Bool" in core:
            name, size = "_Bool", 1
        else:
            name, size = ("unsigned int" if unsigned else "int"), 4
        try:
            return self.info.type_named(name)
        except KeyError:
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
        size = s.size or 4
        if isinstance(s, BaseType) and s.encoding == "float":
            data = struct.pack("<f" if size == 4 else "<d", float(number))
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
            result = decode_bits(value.type, self.read(value), value.bit_offset, value.bit_size)
            return result["value"] if isinstance(result, dict) else int(result)
        data = self.read(value)
        if isinstance(s, BaseType):
            if s.encoding == "float":
                return struct.unpack("<f" if s.size == 4 else "<d", data)[0]
            return int.from_bytes(data, "little", signed=s.signed)
        if isinstance(s, EnumType):
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


def _no_memory(address: int, length: int) -> bytes:
    raise AccessError("ターゲットのメモリの読み手が無い")


def _no_register(number: int) -> int:
    raise NotAvailable("フレームが無い（レジスタが無い）")
