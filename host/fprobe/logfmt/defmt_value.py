"""defmt の引数の読み（ワイヤ形式のバイト列 → 値）と、公式の解読器（decoder/src/frame.rs）に合わせた表示。

表（DefmtTable）は入れ子の書式・ビットフラグの名前を引くためだけに受け取る。fprobe.logfmt.defmt が使う。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from .defmt_format import _INTS, LOG_TAGS, DefmtEntry, DefmtError, Hint, Param, _Incomplete, parse_format

if TYPE_CHECKING:
    from .defmt import DefmtTable


@dataclass
class _Formatted:
    """解読した 1 つの書式（入れ子の Format も）: 並びと、番号 → 値。"""
    fragments: list
    args: dict


class _Reader:
    def __init__(self, data: bytes, table: DefmtTable):
        self.data, self.pos, self.table = data, 0, table

    def take(self, n: int) -> bytes:
        if self.pos + n > len(self.data):
            raise _Incomplete("かたまりが途中で終わった")
        out = self.data[self.pos:self.pos + n]
        self.pos += n
        return out

    def u(self, bits: int) -> int:
        return int.from_bytes(self.take(bits // 8), "little")

    def s(self, bits: int) -> int:
        return int.from_bytes(self.take(bits // 8), "little", signed=True)

    def nested_entry(self) -> DefmtEntry:
        index = self.u(16)
        entry = self.table.entries.get(index)
        if entry is None:
            raise DefmtError(f"表に無い番号 {index}（入れ子）")
        if entry.tag in LOG_TAGS:
            raise DefmtError(f"入れ子の番号 {index} がログを指している")
        return entry

    def format_args(self, fragments: list) -> _Formatted:
        params = [p for p in fragments if isinstance(p, Param)]
        merged: dict[int, Param] = {}
        for p in params:
            if p.type == "bits" and p.index in merged and merged[p.index].type == "bits":
                a, b = merged[p.index].bits
                merged[p.index] = Param(p.index, "bits", None, bits=(min(a, p.bits[0]), max(b, p.bits[1])))
            else:
                merged.setdefault(p.index, p)
        args = {i: self.value(merged[i]) for i in sorted(merged)}
        return _Formatted(fragments, args)

    def value(self, p: Param):
        t = p.type
        if t in _INTS:
            bits, signed = _INTS[t]
            return self.s(bits) if signed else self.u(bits)
        if t == "f32":
            return struct.unpack("<f", self.take(4))[0]
        if t == "f64":
            return struct.unpack("<d", self.take(8))[0]
        if t == "bool":
            b = self.u(8)
            if b > 1:
                raise DefmtError(f"bool の値が {b}")
            return bool(b)
        if t == "char":
            code = self.u(32)
            try:
                return chr(code)
            except ValueError:
                raise DefmtError(f"char の値が {code:#x}") from None
        if t == "str":
            return self._utf8(self.take(self.u(32)))
        if t == "[u8]":
            return bytes(self.take(self.u(32)))
        if t.startswith("[u8;"):
            return bytes(self.take(p.length))
        if t == "istr":
            entry = self.nested_entry()
            return entry.data
        if t == "format":
            return self.format_value(self.nested_entry())
        if t == "[?]":
            return self.format_slice(self.u(32))
        if t.startswith("[?;"):
            return self.format_slice(p.length)
        if t in ("__internal_Debug", "__internal_Display"):
            end = self.data.find(b"\xff", self.pos)
            if end < 0:
                raise _Incomplete("__internal_Debug の終わり（0xFF）が無い")
            return self._utf8(self.take(end - self.pos + 1)[:-1])
        if t == "__internal_FormatSequence":
            items = []
            while True:
                index = self.u(16)
                if index == 0:
                    return items
                entry = self.table.entries.get(index)
                if entry is None:
                    raise DefmtError(f"表に無い番号 {index}（並び）")
                items.append(self.format_args(entry.fragments))
        if t == "bits":
            start, end = p.bits
            lo, hi = start // 8, (end - 1) // 8
            size = hi - lo + 1
            width = 1 if size == 1 else 2 if size == 2 else 4 if size <= 4 else 8 if size <= 8 else 16
            return self.u(width * 8) << (lo * 8)
        raise DefmtError(f"知らない型 {t}")

    def format_value(self, entry: DefmtEntry, variants: list | None = None):
        """Format の値: enum（書式に | がある derived）は判別子で枝を選ぶ。"""
        variants = variants if variants is not None else _variants(entry)
        if variants is None:
            return self.format_args(entry.fragments)
        n = len(variants) - 1
        width = 8 if n <= 0xFF else 16 if n <= 0xFFFF else 32
        d = self.u(width)
        if d >= len(variants):
            raise DefmtError(f"enum の判別子 {d} が枝の数 {len(variants)} を超えている")
        return self.format_args(parse_format(variants[d]))

    def format_slice(self, count: int) -> list:
        if count == 0:
            return []
        entry = self.nested_entry()                                   # 書式の番号は 1 回だけ（全要素で共有）
        variants = _variants(entry)
        return [self.format_value(entry, variants) for _ in range(count)]

    @staticmethod
    def _utf8(data: bytes) -> str:
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            raise DefmtError("文字列が UTF-8 でない") from None


def _variants(entry: DefmtEntry) -> list[str] | None:
    if entry.tag != "derived" or "|" not in entry.data:
        return None
    return entry.data.split("|")


def _plain(value):
    """記録の args 用の素朴な値（入れ子は文字に）。"""
    if isinstance(value, _Formatted):
        return _render(value, None, None)
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


# --- 表示 ---------------------------------------------------------------------------------------

def _effective(hint: Hint | None, parent: Hint | None) -> Hint | None:
    if hint is None or hint.kind in (None, "?") and not hint.zero_pad and not hint.alternate:
        return parent if parent is not None else hint
    if hint.kind == "?" and parent is not None:
        return parent
    return hint


def _render(f: _Formatted, parent: Hint | None, table: DefmtTable | None) -> str:
    out = []
    for frag in f.fragments:
        if isinstance(frag, str):
            out.append(frag)
            continue
        value = f.args.get(frag.index)
        out.append(_render_value(frag, value, parent, table))
    return "".join(out)


def _render_value(p: Param, value, parent: Hint | None, table) -> str:
    hint = _effective(p.hint, parent)
    t = p.type
    if t == "bits":
        start, end = p.bits
        return _int(value >> start & ((1 << (end - start)) - 1), end - start, False, hint, table)
    if t in _INTS:
        bits, signed = _INTS[t]
        return _int(value, bits, signed, hint, table)
    if t in ("f32", "f64"):
        return _float(value, t == "f32")
    if t == "bool":
        return "true" if value else "false"
    if t == "char":
        return _rust_debug_char(value) if hint is not None and hint.kind == "?" else value
    if t in ("str", "istr", "__internal_Debug", "__internal_Display"):
        if t == "str" and hint is not None and hint.kind == "?":
            return _rust_debug_str(value)
        return value
    if t == "[u8]" or t.startswith("[u8;"):
        return _bytes(value, hint)
    if t == "format":
        inner = parent if parent is not None and parent.kind == "a" else hint
        return _render(value, inner, table)
    if t == "[?]" or t.startswith("[?;"):
        inner = parent if parent is not None and parent.kind == "a" else hint
        if inner is not None and inner.kind == "a" and value and \
                all(len(v.args) == 1 and isinstance(v.args.get(0), int) and "{=u8" in "".join(
                    x if isinstance(x, str) else f"{{={x.type}" for x in v.fragments) for v in value):
            return _bytes(bytes(v.args[0] for v in value), inner)
        return "[" + ", ".join(_render(v, inner, table) for v in value) + "]"
    if t == "__internal_FormatSequence":
        return "".join(_render(v, hint, table) for v in value)
    return str(value)


def _int(value: int, bits: int, signed: bool, hint: Hint | None, table) -> str:
    kind = hint.kind if hint is not None else None
    if kind in ("x", "X", "b", "o"):
        if value < 0:
            value &= (1 << bits) - 1                                  # 元の幅の 2 の補数
        digits = format(value, kind)
        prefix = {"x": "0x", "X": "0x", "b": "0b", "o": "0o"}[kind] if hint.alternate else ""
        if hint.zero_pad:
            digits = digits.rjust(max(hint.zero_pad - len(prefix), 0), "0")
        return prefix + digits
    if kind == "us":
        return f"{value // 1_000_000}.{value % 1_000_000:06}"
    if kind == "ms":
        return f"{value // 1_000}.{value % 1_000:03}"
    if kind in ("tus", "tms", "ts"):
        return _clock(value, kind)
    if kind in ("iso8601ms", "iso8601s"):
        ms = value if kind == "iso8601ms" else value * 1000
        dt = datetime.fromtimestamp(ms // 1000, tz=timezone.utc)
        frac = f".{ms % 1000:03}" if kind == "iso8601ms" else ""
        return dt.strftime("%Y-%m-%dT%H:%M:%S") + frac + "Z"
    if kind == "bitflags" and table is not None:
        flags = [name for name, v in table.bitflags.get(hint.bitflags, []) if v and value & v == v]
        return " | ".join(flags) if flags else "(empty)"
    text = str(value)
    if hint is not None and hint.zero_pad:
        sign = "-" if value < 0 else ""
        text = sign + str(abs(value)).rjust(hint.zero_pad - len(sign), "0")
    return text


def _clock(value: int, kind: str) -> str:
    per = {"tus": 1_000_000, "tms": 1_000, "ts": 1}[kind]
    seconds, frac = divmod(value, per)
    minutes, s = divmod(seconds, 60)
    hours, m = divmod(minutes, 60)
    days, h = divmod(hours, 24)
    text = f"{h:02}:{m:02}:{s:02}"
    if days:
        text = f"{days}:{text}"
    if kind == "tus":
        text += f".{frac:06}"
    elif kind == "tms":
        text += f".{frac:03}"
    return text


def _float(value: float, single: bool) -> str:
    """ryu の最短表記に寄せる（1.0 → "1.0"、1e20 → "1e20"）。f32 は 32 ビットで往復する最短の桁。"""
    if value != value:
        return "NaN"
    if value in (float("inf"), float("-inf")):
        return "inf" if value > 0 else "-inf"
    text = repr(value)
    if single:
        for digits in range(1, 10):
            candidate = f"{value:.{digits}g}"
            if struct.unpack("<f", struct.pack("<f", float(candidate)))[0] == value:
                text = repr(float(candidate))
                break
    if "e" in text:
        mantissa, exponent = text.split("e")
        mantissa = mantissa[:-2] if mantissa.endswith(".0") else mantissa
        return f"{mantissa}e{int(exponent)}"
    return text


def _bytes(data: bytes, hint: Hint | None) -> str:
    kind = hint.kind if hint is not None else None
    if kind == "a":
        out = []
        for b in data:
            c = chr(b)
            out.append({"\t": "\\t", "\n": "\\n", "\r": "\\r", '"': '\\"', "\\": "\\\\"}.get(c) or
                       (c if 0x20 <= b < 0x7F else f"\\x{b:02x}"))
        return 'b"' + "".join(out) + '"'
    if kind in ("x", "X", "b", "o"):
        h = Hint(kind, True, hint.zero_pad)
        return "[" + ", ".join(_int(b, 8, False, h, None) for b in data) + "]"
    return "[" + ", ".join(str(b) for b in data) + "]"


def _rust_debug_str(s: str) -> str:
    out = []
    for c in s:
        if c in "\"\\":
            out.append("\\" + c)
        elif c == "\n":
            out.append("\\n")
        elif c == "\r":
            out.append("\\r")
        elif c == "\t":
            out.append("\\t")
        elif ord(c) < 0x20 or ord(c) == 0x7F:
            out.append(f"\\u{{{ord(c):x}}}")
        else:
            out.append(c)
    return '"' + "".join(out) + '"'


def _rust_debug_char(c: str) -> str:
    return "'" + ("\\'" if c == "'" else _rust_debug_str(c)[1:-1].replace('\\"', '"')) + "'"
