"""SCVD の式・型・メモリの読み（scvd の下層。Scvd の読み込み・表示の木は scvd）。

式の字句・構文（`_Parser`）、評価（`_Env`。<var>・<read> の値・メンバ・関数）、メモリ・シンボル・レジスタの口
（`ScvdContext`）、型・出来事・成分の表（`Typedef`・`ScvdEvent`・`Component`）。式の文法・関数・書式の一覧は scvd の説明。
"""

from __future__ import annotations

import math
import re
import struct
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .scvd import Scvd

LIST_LIMIT = 1024                         # readlist・list の回数の上限（壊れたリストで止まらないように）
STRING_LIMIT = 128                        # %N・%t で番地から読む C 文字列の上限（バイト。切ったら印と notes）
_SCALARS = {"uint8_t": (1, "B"), "int8_t": (1, "b"), "uint16_t": (2, "H"), "int16_t": (2, "h"),
            "uint32_t": (4, "I"), "int32_t": (4, "i"), "uint64_t": (8, "Q"), "int64_t": (8, "q"),
            "float": (4, "f"), "double": (8, "d")}


class ScvdError(Exception):
    pass


# --- 型 -----------------------------------------------------------------------------------------

@dataclass
class Member:
    name: str
    type: str
    offset: int
    size: int | None = None              # 配列の要素数
    enums: dict[int, str] = field(default_factory=dict)

    @property
    def scalar(self) -> tuple[int, str]:
        t = "uint32_t" if self.type.startswith("*") else self.type
        return _SCALARS.get(t, (4, "I"))


@dataclass
class Typedef:
    name: str
    size: int
    members: dict[str, Member] = field(default_factory=dict)


@dataclass
class ScvdEvent:
    id: int
    level: str | None
    property: str
    value: str
    info: str = ""
    handle: str | None = None
    hname: str | None = None
    state: str | None = None
    tracking: str | None = None
    prints: list[dict] = field(default_factory=list)


@dataclass
class Component:
    no: int
    name: str
    brief: str = ""
    prefix: str = ""
    states: dict[str, dict] = field(default_factory=dict)


# --- 式 -----------------------------------------------------------------------------------------

_TOKEN = re.compile(r"""\s*(?:
    (?P<num>0[xX][0-9a-fA-F]+[uUlL]*|\d+\.\d*(?:[eE][-+]?\d+)?|\d+[eE][-+]?\d+|\d+[uUlL]*)|
    (?P<str>"(?:[^"\\]|\\.)*")|
    (?P<name>[A-Za-z_]\w*)|
    (?P<op><<=|>>=|&&|\|\||==|!=|<=|>=|<<|>>|\+=|-=|\*=|/=|%=|&=|\|=|\^=|\+\+|--|[-+*/%&|^!~<>=?:.,()\[\];])
)""", re.X)
_BINARY = {"||": 1, "&&": 2, "|": 3, "^": 4, "&": 5, "==": 6, "!=": 6, "<": 7, ">": 7, "<=": 7, ">=": 7,
           "<<": 8, ">>": 8, "+": 9, "-": 9, "*": 10, "/": 10, "%": 10}
_ASSIGN = {"=", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "<<=", ">>="}


def _tokens(text: str) -> list[tuple[str, str]]:
    out, pos = [], 0
    text = text.strip()
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if m is None or m.end() == pos:
            raise ScvdError(f"式を読めない: {text[pos:pos + 20]!r}（{text!r}）")
        pos = m.end()
        for kind in ("num", "str", "name", "op"):
            if m[kind] is not None:
                out.append((kind, m[kind]))
                break
    return out


class _Parser:
    """式 → 木（タプル）。"""

    def __init__(self, text: str, types: set[str] | None = None):
        self.toks = _tokens(text)
        self.i = 0
        self.types = types              # 型の名前（name:member を型の参照とみなすのはこれだけ。?: の : と区別する）

    def peek(self, offset: int = 0) -> tuple[str, str] | None:
        j = self.i + offset
        return self.toks[j] if j < len(self.toks) else None

    def take(self, value: str | None = None) -> tuple[str, str]:
        t = self.peek()
        if t is None or (value is not None and t[1] != value):
            raise ScvdError(f"式の誤り: {value or '続き'} が要る")
        self.i += 1
        return t

    def statements(self) -> list:
        out = []
        while self.peek() is not None:
            if self.peek()[1] == ";":
                self.take()
                continue
            out.append(self.expression())
            if self.peek() is not None:
                self.take(";")
        return out

    def expression(self):
        left = self.conditional()
        t = self.peek()
        if t is not None and t[0] == "op" and t[1] in _ASSIGN:
            self.take()
            return ("assign", t[1], left, self.expression())
        return left

    def conditional(self):
        cond = self.binary(1)
        if self.peek() is not None and self.peek()[1] == "?":
            self.take()
            a = self.expression()
            self.take(":")
            return ("?", cond, a, self.conditional())
        return cond

    def binary(self, level: int):
        left = self.unary()
        while True:
            t = self.peek()
            if t is None or t[0] != "op" or _BINARY.get(t[1], 0) < level:
                return left
            self.take()
            right = self.binary(_BINARY[t[1]] + 1)
            left = ("bin", t[1], left, right)

    def unary(self):
        t = self.peek()
        if t is not None and t[0] == "op" and t[1] in ("-", "+", "!", "~"):
            self.take()
            return ("un", t[1], self.unary())
        if t is not None and t[1] == "(" and self.peek(1) and self.peek(1)[0] == "name" and \
                self.peek(1)[1] in _SCALARS and self.peek(2) and self.peek(2)[1] == ")":
            self.i += 3
            return ("cast", self.toks[self.i - 2][1], self.unary())
        return self.postfix(self.primary())

    def primary(self):
        kind, value = self.take()
        if kind == "num":
            text = value.rstrip("uUlL")
            if text.lower().startswith("0x"):
                return ("num", int(text, 16))
            return ("num", float(text) if any(c in text for c in ".eE") else int(text))
        if kind == "str":
            return ("str", bytes(value[1:-1], "utf-8").decode("unicode_escape"))
        if kind == "name":
            if self.peek() is not None and self.peek()[1] == "(":
                self.take()
                args = []
                while self.peek()[1] != ")":
                    args.append(self.typed_reference() or self.expression())
                    if self.peek()[1] == ",":
                        self.take()
                self.take(")")
                return ("call", value, args)
            if self.peek() is not None and self.peek()[1] == ":" and self.peek(1) and self.peek(1)[0] == "name" \
                    and (self.types is None or value in self.types):
                self.take()
                member = self.take()[1]
                if self.peek() is not None and self.peek()[1] == ":" and self.peek(1) and self.peek(1)[0] == "name":
                    self.take()
                    return ("enum", value, member, self.take()[1])
                return ("member_ref", value, member)
            return ("name", value)
        if value == "(":
            inner = self.expression()
            self.take(")")
            return inner
        raise ScvdError(f"式の誤り: {value!r}")

    def typed_reference(self):
        """関数の引数の typedef:member（__Offset_of）。"""
        a, b, c = self.peek(), self.peek(1), self.peek(2)
        if a and b and c and a[0] == "name" and b[1] == ":" and c[0] == "name" and \
                not (self.peek(3) and self.peek(3)[1] == ":"):
            self.i += 3
            return ("member_ref", a[1], c[1])
        return None

    def postfix(self, node):
        while True:
            t = self.peek()
            if t is None:
                return node
            if t[1] == "[":
                self.take()
                index = self.expression()
                self.take("]")
                node = ("index", node, index)
            elif t[1] == ".":
                self.take()
                node = ("dot", node, self.take()[1])
            elif t[1] in ("++", "--"):
                self.take()
                node = ("assign", "+=" if t[1] == "++" else "-=", node, ("num", 1))
            else:
                return node


@dataclass
class _Element:
    """読んだ 1 つの要素（型・番地・中身）。calc でメンバに書いた値は extra に。"""
    type: str
    address: int
    data: bytes
    extra: dict = field(default_factory=dict)


@dataclass
class _Readout:
    """<read>・<readlist> の結果（要素の並び）。"""
    type: str
    elements: list[_Element] = field(default_factory=list)


@dataclass
class _Ref:
    """メンバの値と、その列挙（%E 用）。"""
    value: object
    member: Member | None = None


class ScvdContext:
    """式の評価に要るもの: メモリの読み（read(番地, 長さ) → bytes）・シンボル（名前 → 番地・大きさ）・番地 → 名前・
    ELF の文字列・レジスタ。どれも無ければ None（その関数・書式は 0 や 16 進になる）。list_limit = readlist・list を
    回す数の上限（壊れたリストで止まらないように。止めたら view の notes にその事実）。string_limit = %N・%t で
    番地から読む C 文字列の上限（バイト。その中に NUL が無ければ切って、値の中に印・notes に番地と上限）。"""

    def __init__(self, read: Callable[[int, int], bytes] | None = None,
                 symbol: Callable[[str], tuple[int, int] | None] | None = None,
                 symbol_name: Callable[[int], str | None] | None = None,
                 string: Callable[[int], str | None] | None = None,
                 register: Callable[[str], int | None] | None = None, running: bool = False,
                 list_limit: int = LIST_LIMIT, string_limit: int = STRING_LIMIT):
        self.read, self.symbol, self.symbol_name = read, symbol, symbol_name
        self.string, self.register, self.running = string, register, running
        self.list_limit, self.string_limit = list_limit, string_limit


class _Env:
    def __init__(self, scvd: "Scvd", ctx: ScvdContext, values: dict | None = None):
        self.scvd, self.ctx = scvd, ctx
        self.vars: dict[str, object] = dict(values or {})
        self.notes: list[str] = []                             # 書式の評価で分かった事実（文字列を上限で切った等）

    def parse(self, text: str):
        return _Parser(text, set(self.scvd.typedefs)).expression()

    def statements(self, text: str) -> list:
        return _Parser(text, set(self.scvd.typedefs)).statements()

    # 読み
    def read(self, address: int, size: int) -> bytes:
        if self.ctx.read is None:
            raise ScvdError("メモリを読めない（ターゲット・スナップショットが無い）")
        return bytes(self.ctx.read(address, size))

    def scalar(self, data: bytes, kind: str):
        size, code = _SCALARS.get(kind, (4, "I"))
        data = data[:size].ljust(size, b"\0")
        return struct.unpack("<" + code, data)[0]

    def member(self, element: _Element, name: str) -> _Ref:
        if name in element.extra:
            return _Ref(element.extra[name])
        if name == "_addr":
            return _Ref(element.address)
        td = self.scvd.typedefs.get(element.type)
        if td is None or name not in td.members:
            raise ScvdError(f"{element.type} にメンバ {name} が無い")
        m = td.members[name]
        size, _ = m.scalar
        if m.size and m.size > 1:
            raw = element.data[m.offset:m.offset + size * m.size]
            return _Ref(raw, m)
        if m.offset < 0 or m.offset + size > len(element.data):
            return _Ref(0, m)
        return _Ref(self.scalar(element.data[m.offset:m.offset + size], m.type), m)

    # 評価
    def eval(self, node) -> object:
        return self.ref(node).value

    def ref(self, node) -> _Ref:
        kind = node[0]
        if kind == "num" or kind == "str":
            return _Ref(node[1])
        if kind == "name":
            name = node[1]
            if name == "__Running":
                return _Ref(1 if self.ctx.running else 0)
            if name not in self.vars:
                raise ScvdError(f"名前 {name} が無い")
            v = self.vars[name]
            if isinstance(v, _Readout):
                return _Ref(v)
            return _Ref(v)
        if kind == "enum":
            td = self.scvd.typedefs.get(node[1])
            m = td.members.get(node[2]) if td else None
            if m is None:
                raise ScvdError(f"{node[1]}:{node[2]} が無い")
            for value, name in m.enums.items():
                if name == node[3]:
                    return _Ref(value)
            raise ScvdError(f"{node[1]}:{node[2]}:{node[3]} が無い")
        if kind == "member_ref":
            return _Ref(node)
        if kind == "dot":
            base = self.ref(node[1]).value
            if isinstance(base, _Readout):
                if node[2] == "_count":
                    return _Ref(len(base.elements))
                if not base.elements:
                    return _Ref(0)
                base = base.elements[0]
            if isinstance(base, _Element):
                return self.member(base, node[2])
            raise ScvdError(f".{node[2]} を付けられない値")
        if kind == "index":
            base = self.ref(node[1]).value
            i = int(self.eval(node[2]))
            if isinstance(base, _Readout):
                if not 0 <= i < len(base.elements):
                    raise ScvdError(f"添字 {i} が範囲の外（{len(base.elements)} 個）")
                el = base.elements[i]
                size, _ = _SCALARS.get(base.type, (0, ""))
                return _Ref(self.scalar(el.data, base.type) if size else el)
            if isinstance(base, (bytes, bytearray)):
                return _Ref(base[i] if 0 <= i < len(base) else 0)
            raise ScvdError("添字を付けられない値")
        if kind == "un":
            v = self.eval(node[2])
            ops = {"-": lambda x: -x, "+": lambda x: x, "!": lambda x: int(not x), "~": lambda x: ~int(x)}
            return _Ref(ops[node[1]](v))
        if kind == "cast":
            v = self.eval(node[2])
            size, code = _SCALARS[node[1]]
            if code in "fd":
                return _Ref(float(v))
            v = int(v) & ((1 << (8 * size)) - 1)
            if code.islower() and v >> (8 * size - 1):
                v -= 1 << (8 * size)
            return _Ref(v)
        if kind == "?":
            return self.ref(node[2]) if self.eval(node[1]) else self.ref(node[3])
        if kind == "bin":
            op = node[1]
            if op == "&&":
                return _Ref(int(bool(self.eval(node[2])) and bool(self.eval(node[3]))))
            if op == "||":
                return _Ref(int(bool(self.eval(node[2])) or bool(self.eval(node[3]))))
            return _Ref(_binary(op, self.eval(node[2]), self.eval(node[3])))
        if kind == "assign":
            value = self.eval(node[3])
            if node[1] != "=":
                value = _binary(node[1][:-1], self.eval(node[2]), value)
            self.assign(node[2], value)
            return _Ref(value)
        if kind == "call":
            return _Ref(self.call(node[1], node[2]))
        raise ScvdError(f"評価できない式 {kind}")

    def assign(self, target, value) -> None:
        if target[0] == "name":
            self.vars[target[1]] = value
            return
        if target[0] == "dot":
            base = self.ref(target[1]).value
            if isinstance(base, _Readout) and base.elements:
                base = base.elements[0]
            if isinstance(base, _Element):
                base.extra[target[2]] = value
                return
        raise ScvdError("代入できない式")

    def call(self, name: str, args: list) -> object:
        if name == "__Symbol_exists":
            return int(self.ctx.symbol is not None and self.ctx.symbol(str(self.eval(args[0]))) is not None)
        if name == "__FindSymbol":
            found = self.ctx.symbol(str(self.eval(args[0]))) if self.ctx.symbol else None
            return found[0] if found else 0
        if name == "__size_of":
            found = self.ctx.symbol(str(self.eval(args[0]))) if self.ctx.symbol else None
            return found[1] if found else 0
        if name == "__Offset_of":
            ref = args[0]
            if ref[0] != "member_ref":
                raise ScvdError("__Offset_of の引数は typedef:member")
            td = self.scvd.typedefs.get(ref[1])
            return td.members[ref[2]].offset if td and ref[2] in td.members else -1
        if name == "__GetRegVal":
            value = self.ctx.register(str(self.eval(args[0]))) if self.ctx.register else None
            return value or 0
        if name == "__CalcMemUsed":
            address, size, fill, magic = (int(self.eval(a)) for a in args)
            return self.calc_mem_used(address, size, fill, magic)
        raise ScvdError(f"知らない関数 {name}")

    def calc_mem_used(self, address: int, size: int, fill: int, magic: int) -> int:
        if size <= 0:
            return 0
        data = self.read(address, size)
        overflow = magic != 0 and struct.unpack_from("<I", data)[0] != magic
        start = 4 if magic else 0
        unused = 0
        for i in range(start, size - 3, 4):
            if struct.unpack_from("<I", data, i)[0] != fill:
                break
            unused += 4
        used = size - start - unused if magic else size - unused
        percent = used * 100 // size
        return (used & 0xFFFFF) | (percent & 0x1FF) << 20 | (1 << 31 if overflow else 0)


def _binary(op: str, a, b):
    if isinstance(a, str) or isinstance(b, str):
        if op == "==":
            return int(a == b)
        if op == "!=":
            return int(a != b)
        raise ScvdError("文字列の演算")
    if op == "/":
        if b == 0:
            raise ScvdError("0 で割った")
        if isinstance(a, float) or isinstance(b, float):
            return a / b
        q = abs(a) // abs(b)
        return q if (a >= 0) == (b >= 0) else -q
    if op == "%":
        if b == 0:
            raise ScvdError("0 で割った")
        return int(math.fmod(a, b)) if not (isinstance(a, float) or isinstance(b, float)) else math.fmod(a, b)
    ops = {"+": lambda x, y: x + y, "-": lambda x, y: x - y, "*": lambda x, y: x * y,
           "<<": lambda x, y: int(x) << int(y), ">>": lambda x, y: int(x) >> int(y), "&": lambda x, y: int(x) & int(y),
           "|": lambda x, y: int(x) | int(y), "^": lambda x, y: int(x) ^ int(y), "==": lambda x, y: int(x == y),
           "!=": lambda x, y: int(x != y), "<": lambda x, y: int(x < y), ">": lambda x, y: int(x > y),
           "<=": lambda x, y: int(x <= y), ">=": lambda x, y: int(x >= y)}
    return ops[op](a, b)


def _c_string(env: _Env, address: int) -> str:
    limit = env.ctx.string_limit
    try:
        data = env.read(address, limit)
    except Exception:                                          # 読めない番地: 16 進で
        return f"0x{address:08X}"
    text, nul, _ = data.partition(b"\0")
    if nul:
        return text.decode("latin-1")
    note = f"上限 {limit} バイトで切った"                       # 上限の中に NUL が無い: 続きは読んでいない
    env.notes.append(f"0x{address:08X} の文字列: {note}（続きは読んでいない。string_limit）")
    return text.decode("latin-1") + f"<{note}>"
