"""SCVD（Component Viewer Description。CMSIS-View の SCVD 形式）の解釈（KEIL-03・KEIL-01 の表示）。

- typedefs: 型（大きさ・メンバの位置と型・列挙の名前）。
- objects（Component Viewer）: <var>・<calc>・<read>・<readlist>・<list> でターゲットのメモリを読み、<out> の
  <item>・<print>・<list> で表示の木を作る（`Scvd.view`）。メモリは呼ぶ側が渡す（走行中でも、スナップショットでも）。
- events（Event Recorder）: 成分の番号（no）・出来事の ID → 名前（property）・値の書式（value の %d[val1] 等）・重要度・
  handle / hname / state / tracking（時間軸の状態）。`Scvd.format_event`。

式は C と同じ優先順位（?: と代入を含む）。名前: val1〜val4（出来事）、<var>・<read> の名前と .member・[i]・
._count・._addr、typedef:member:enum（列挙の値）、関数 __FindSymbol・__Symbol_exists・__size_of・__Offset_of・
__CalcMemUsed・__GetRegVal・__Running。書式: %d %u %x %T %t %E %S %C %N %F %I %J %M %U %%。
出典: ARM-software/CMSIS-View Documentation/Doxygen/src/ref_scvd.txt、tools/eventlist（Arm の Go の実装）。
確かでない点: %F（EventRecorder.scvd が使う。文書に無い）はアドレスの指す ELF の文字列（ファイル名）とした。
<readlist> の limit・<list> の limit は「その値に達したら終わり」とした。%U は UTF-16 の文字列として読む。
"""

from __future__ import annotations

import math
import re
import struct
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["Scvd", "ScvdContext", "ScvdError", "ScvdEvent", "evaluate"]

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


def evaluate(text: str, values: dict | None = None, scvd: "Scvd | None" = None, ctx: ScvdContext | None = None):
    """式を 1 つ評価する（試験・道具の口）。"""
    env = _Env(scvd or Scvd(), ctx or ScvdContext(), values)
    result = None
    for statement in env.statements(text):
        result = env.eval(statement)
    return result


# --- 書式 ---------------------------------------------------------------------------------------

def _split_format(text: str) -> list:
    """value の文字 → 文字と (変換, 式, typedef:member) の並び。式の中の [] は数えて閉じる。"""
    out, pos = [], 0
    while (i := text.find("%", pos)) >= 0:
        out.append(text[pos:i])
        if text[i + 1:i + 2] == "%":
            out.append("%")
            pos = i + 2
            continue
        conv = text[i + 1:i + 2]
        if text[i + 2:i + 3] != "[":
            out.append(text[i:i + 2])
            pos = i + 2
            continue
        depth, j = 1, i + 3
        while j < len(text) and depth:
            depth += {"[": 1, "]": -1}.get(text[j], 0)
            j += 1
        inner = text[i + 3:j - 1]
        enum_ref = None
        m = re.match(r"(.*),\s*([A-Za-z_]\w*):([A-Za-z_]\w*)\s*$", inner, flags=re.S)
        if m and m[1].count("(") == m[1].count(")"):
            inner, enum_ref = m[1], (m[2], m[3])
        out.append((conv, inner, enum_ref))
        pos = j
    out.append(text[pos:])
    return out


class Scvd:
    def __init__(self):
        self.components: list[Component] = []
        self.typedefs: dict[str, Typedef] = {}
        self.events: dict[int, ScvdEvent] = {}
        self.objects: list[ET.Element] = []
        self.files: list[str] = []
        self.names: list[str] = []

    @classmethod
    def load(cls, *sources: str | Path) -> "Scvd":
        """SCVD のファイル（複数可。重なる ID は後のものが勝つ）。"""
        scvd = cls()
        for s in sources:
            scvd.add(Path(s).read_text(encoding="utf-8"), str(s))
        return scvd

    def add(self, text: str, name: str = "<text>") -> None:
        try:
            root = ET.fromstring(text)
        except ET.ParseError as e:
            raise ScvdError(f"{name}: XML の誤り: {e}") from None
        self.files.append(name)
        for comp in root.iter("component"):
            if comp.get("no") is None and comp.get("name"):
                self.names.append(comp.get("name"))
        env = _Env(self, ScvdContext())
        for td in root.iter("typedef"):
            size = int(env.eval(env.parse(td.get("size", "0")))) if td.get("size") else 0
            t = Typedef(td.get("name"), size)
            for m in td.iter("member"):
                enums = {}
                for e in m.iter("enum"):
                    try:
                        enums[int(env.eval(env.parse(e.get("value", "0"))))] = e.get("name")
                    except ScvdError:
                        pass
                offset = int(env.eval(env.parse(m.get("offset", "0")))) if m.get("offset") else 0
                count = int(m.get("size")) if m.get("size", "").isdigit() else None
                t.members[m.get("name")] = Member(m.get("name"), m.get("type", "uint32_t"), offset, count, enums)
            for v in td.findall("var"):
                t.members[v.get("name")] = Member(v.get("name"), v.get("type", "uint32_t"), -1)
            self.typedefs[t.name] = t
        events = root.find("events")
        if events is not None:
            for comp in events.iter("component"):
                c = Component(int(comp.get("no", "0"), 0), comp.get("name", ""), comp.get("brief", ""),
                              comp.get("prefix", ""))
                for st in comp.findall("state"):
                    c.states[st.get("name")] = dict(st.attrib)
                self.components.append(c)
            for ev in events.iter("event"):
                try:
                    event_id = int(env.eval(env.parse(ev.get("id", "0"))))
                except ScvdError:
                    continue
                prints = [dict(p.attrib) for p in ev.findall("print")]
                self.events[event_id] = ScvdEvent(event_id, ev.get("level"), ev.get("property", ""),
                                                  ev.get("value", ""), ev.get("info", ""), ev.get("handle"),
                                                  ev.get("hname"), ev.get("state"), ev.get("tracking"), prints)
        objects = root.find("objects")
        if objects is not None:
            self.objects.extend(objects.findall("object"))

    def component(self, number: int) -> Component | None:
        for c in self.components:
            if c.no == number:
                return c
        return None

    # --- 書式 -----------------------------------------------------------------------------

    def format(self, text: str, env: _Env) -> str:
        out = []
        for part in _split_format(text):
            if isinstance(part, str):
                out.append(part)
                continue
            conv, expr, enum_ref = part
            try:
                out.append(self._one(conv, expr, enum_ref, env))
            except ScvdError as e:
                out.append(f"<{e}>")
        return "".join(out)

    def _one(self, conv: str, expr: str, enum_ref, env: _Env) -> str:
        ref = env.ref(env.parse(expr))
        value = ref.value
        if conv == "E":
            member = ref.member
            if enum_ref is not None:
                td = self.typedefs.get(enum_ref[0])
                member = td.members.get(enum_ref[1]) if td else None
            if member is not None and isinstance(value, int):
                size, code = member.scalar                     # 記録の値は 32 ビットの生の値: メンバの型の符号で読む
                value &= (1 << (8 * size)) - 1
                if code.islower() and value >> (8 * size - 1):
                    value -= 1 << (8 * size)
                if value in member.enums:
                    return member.enums[value]
            return str(value)
        if conv == "t":
            if isinstance(value, (bytes, bytearray)):
                return value.split(b"\0", 1)[0].decode("latin-1")
            if isinstance(value, int) and env.ctx.read is not None and value:
                return _c_string(env, value)
            return str(value)
        if isinstance(value, (bytes, bytearray)):
            value = int.from_bytes(value[:8], "little")
        if conv == "d":
            if isinstance(value, float):
                return f"{value:g}"
            v = int(value)
            return str(v - (1 << 32) if 0x8000_0000 <= v < 1 << 32 else v)
        if conv == "u":
            return str(int(value) & 0xFFFFFFFF if int(value) < 0 else int(value))
        if conv == "x":
            return f"0x{int(value) & 0xFFFFFFFF if int(value) < 0 else int(value):X}"
        if conv == "T":
            return f"{value:g}" if isinstance(value, float) else str(value)
        if conv in ("S", "C"):
            name = env.ctx.symbol_name(int(value)) if env.ctx.symbol_name else None
            return name or f"0x{int(value):08X}"
        if conv == "N":
            return _c_string(env, int(value)) if value else ""
        if conv == "F":
            text = env.ctx.string(int(value)) if env.ctx.string else None
            return text if text is not None else f"0x{int(value):08X}"
        if conv == "I":
            v = int(value) & 0xFFFFFFFF
            return ".".join(str(v >> s & 0xFF) for s in (24, 16, 8, 0))
        if conv == "M":
            raw = env.read(int(value), 6) if env.ctx.read else int(value).to_bytes(6, "big")
            return "-".join(f"{b:02X}" for b in raw)
        if conv == "J":
            raw = env.read(int(value), 16)
            return ":".join(f"{int.from_bytes(raw[i:i + 2], 'big'):x}" for i in range(0, 16, 2))
        if conv == "U":
            raw = env.read(int(value), 64)
            text = raw.decode("utf-16-le", errors="replace")
            return text.split("\0", 1)[0]
        return str(value)

    # --- Event Recorder ----------------------------------------------------------------------

    def format_event(self, event_id: int, values: dict, ctx: ScvdContext | None = None) -> dict | None:
        """出来事の ID（16 ビット）と val1〜val4 → {property, value, level, component, handle, name, state,
        tracking}。level は SCVD の level の名前のまま（Error / API / Op / Detail。fprobe の重要度に写さない）。SCVD に無ければ None。"""
        ev = self.events.get(event_id)
        if ev is None:
            return None
        env = _Env(self, ctx or ScvdContext(), values)
        property_text, value_text = ev.property, ev.value
        for p in ev.prints:
            try:
                ok = bool(env.eval(env.parse(p["cond"]))) if p.get("cond") else True
            except ScvdError:
                ok = False
            if ok:
                property_text, value_text = p.get("property", property_text), p.get("value", value_text)
                break
        comp = self.component(event_id >> 8)
        out = {"property": self.format(property_text, env), "value": self.format(value_text, env),
               "level": ev.level, "component": comp.name if comp else None}
        if ev.handle:
            try:
                out["handle"] = int(env.eval(env.parse(ev.handle)))
            except ScvdError:
                pass
        if ev.hname:
            out["name"] = self.format(ev.hname, env)
        if ev.state:
            out["state"] = ev.state
            if comp and comp.states.get(ev.state, {}).get("unique") == "1":
                out["unique"] = True
        if ev.tracking:
            out["tracking"] = ev.tracking
        if env.notes:
            out["notes"] = env.notes
        return out

    # --- Component Viewer --------------------------------------------------------------------

    def view(self, ctx: ScvdContext) -> list[dict]:
        """objects を評価して表示の木（[{property, value, children}]）を作る。読めない・式の誤りは notes に。"""
        out = []
        for obj in self.objects:
            env = _Env(self, ctx)
            for el in obj.iter():                               # 読まなかった（cond が偽の）リストも空として名前がある
                if el.tag in ("read", "readlist") and el.get("name"):
                    env.vars.setdefault(el.get("name"), _Readout(el.get("type", "uint32_t")))
            notes: list[str] = []
            items: list[dict] = []
            self._statements(obj, env, notes, items)
            notes.extend(env.notes)
            node = {"property": obj.get("name", ""), "value": "", "children": items}
            if notes:
                node["notes"] = notes
            out.append(node)
        return out

    def _cond(self, el: ET.Element, env: _Env, notes: list[str]) -> bool:
        cond = el.get("cond")
        if not cond:
            return True
        try:
            return bool(env.eval(env.parse(cond)))
        except ScvdError as e:
            notes.append(f"<{el.tag} {el.get('name', '')}> の cond: {e}")
            return False

    def _statements(self, parent: ET.Element, env: _Env, notes: list[str], items: list[dict]) -> None:
        for el in parent:
            tag = el.tag
            try:
                if tag == "var":
                    value = el.get("value", "0")
                    env.vars[el.get("name")] = env.eval(env.parse(value)) if value else 0
                elif not self._cond(el, env, notes):
                    continue
                elif tag == "calc":
                    for st in env.statements(el.text or ""):
                        env.eval(st)
                elif tag == "read":
                    self._read(el, env)
                elif tag == "readlist":
                    self._readlist(el, env, notes)
                elif tag == "list":
                    self._loop(el, env, lambda: self._statements(el, env, notes, items), notes)
                elif tag == "out":
                    for child in el:
                        self._out(child, env, notes, items)
            except ScvdError as e:
                notes.append(f"<{tag} {el.get('name', '')}>: {e}")

    def _address(self, el: ET.Element, env: _Env) -> int:
        address = 0
        if el.get("symbol"):
            found = env.ctx.symbol(el.get("symbol")) if env.ctx.symbol else None
            if found is None:
                raise ScvdError(f"シンボル {el.get('symbol')} が無い")
            address = found[0]
        if el.get("offset"):
            address += int(env.eval(env.parse(el.get("offset"))))
        return address

    def _type_size(self, kind: str) -> int:
        if kind in _SCALARS:
            return _SCALARS[kind][0]
        td = self.typedefs.get(kind)
        if td is None:
            raise ScvdError(f"型 {kind} が無い")
        return td.size

    def _read(self, el: ET.Element, env: _Env) -> None:
        kind = el.get("type", "uint32_t")
        size = self._type_size(kind)
        count = int(env.eval(env.parse(el.get("size")))) if el.get("size") else 1
        address = self._address(el, env)
        data = env.read(address, size * count) if count > 0 else b""
        readout = _Readout(kind, [_Element(kind, address + i * size, data[i * size:(i + 1) * size])
                                  for i in range(count)])
        env.vars[el.get("name")] = readout

    def _readlist(self, el: ET.Element, env: _Env, notes: list[str]) -> None:
        kind = el.get("type", "uint32_t")
        size = self._type_size(kind)
        name = el.get("name")
        existing = env.vars.get(name)
        readout = existing if isinstance(existing, _Readout) and el.get("init") != "1" else _Readout(kind)
        env.vars[name] = readout
        base = self._address(el, env)
        count = int(env.eval(env.parse(el.get("count")))) if el.get("count") else None
        if el.get("based") == "1":
            n = count or 1
            pointers = struct.unpack(f"<{n}I", env.read(base, 4 * n))
            addresses = [p for p in pointers if p]
        elif el.get("next"):
            addresses = []
            address, seen = base, set()
            most = count or env.ctx.list_limit                       # count は SCVD が決める数、無ければ上限
            while address and address not in seen and len(addresses) < most:
                seen.add(address)
                addresses.append(address)
                element = _Element(kind, address, env.read(address, size))
                address = int(env.member(element, el.get("next")).value)
            if address and address in seen:
                notes.append(f"<readlist {name}>: {len(addresses)} 個の後、前に読んだ項目 0x{address:08X} に戻った（輪）。"
                             "そこで止めた")
            elif address and not count:
                notes.append(f"<readlist {name}>: 辿る上限 {most} 個に達したので止めた（続きは読んでいない）")
            for a in addresses:
                readout.elements.append(_Element(kind, a, env.read(a, size)))
            return
        else:
            wanted = count if count is not None else 1
            if wanted > env.ctx.list_limit:
                notes.append(f"<readlist {name}>: count {wanted} のうち上限 {env.ctx.list_limit} 個だけ読んだ")
            addresses = [base + i * size for i in range(min(wanted, env.ctx.list_limit))]
        if addresses and el.get("next") is None and el.get("based") != "1":
            data = env.read(addresses[0], size * len(addresses))
            readout.elements.extend(_Element(kind, a, data[i * size:(i + 1) * size]) for i, a in enumerate(addresses))
            return
        for a in addresses:
            readout.elements.append(_Element(kind, a, env.read(a, size)))

    def _loop(self, el: ET.Element, env: _Env, body: Callable[[], None], notes: list[str]) -> None:
        name = el.get("name")
        start = int(env.eval(env.parse(el.get("start", "0"))))
        limit = el.get("limit")
        cond_while = el.get("while")
        env.vars[name] = start
        for _ in range(env.ctx.list_limit):
            if limit is not None and env.vars[name] >= int(env.eval(env.parse(limit))):
                break
            if cond_while is not None and not env.eval(env.parse(cond_while)):
                break
            body()
            if cond_while is None:
                env.vars[name] = int(env.vars[name]) + 1
        else:                                                         # 終わりの条件の前に上限で止めた
            notes.append(f"<list {name}>: 回す上限 {env.ctx.list_limit} 回に達したので止めた（続きは回していない）")

    def _out(self, el: ET.Element, env: _Env, notes: list[str], items: list[dict]) -> None:
        if not self._cond(el, env, notes):
            return
        if el.tag == "list":
            self._loop(el, env, lambda: [self._out(c, env, notes, items) for c in el], notes)
            return
        if el.tag != "item":
            return
        prop, value = el.get("property", ""), el.get("value", "")
        for p in el.findall("print"):
            if self._cond(p, env, notes):
                prop, value = p.get("property", prop), p.get("value", value)
                break
        node = {"property": self.format(prop, env), "value": self.format(value, env)}
        children: list[dict] = []
        for child in el:
            if child.tag in ("item", "list"):
                self._out(child, env, notes, children)
        if children:
            node["children"] = children
        if el.get("alert") == "1":
            node["alert"] = True
        items.append(node)


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
