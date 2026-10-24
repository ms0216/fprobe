"""バイト列 + 型 → JSON にできる値（decode）と、値 → バイト列（encode）。リトルエンディアン。

値の形（AI がそのまま読め、UI が木で表示できる）。どの値も "kind"（値の種類。DWARF の型で決まる）を持つ object:

    整数・文字 → {"kind": "int", "value": n}
    浮動小数（4・8 バイト） → {"kind": "float", "value": x}（NaN・無限大は value が "nan" "inf" "-inf"）
    真偽 → {"kind": "bool", "value": b}
    生のバイト（符号の種類の分からない基本型・4/8 以外の大きさの浮動小数・void） → {"kind": "bytes", "hex": "..."}
    列挙 → {"kind": "enum", "name": "MODE_RUN"（列挙子に無い値は None）, "value": 1}
    ポインタ → {"kind": "pointer", "address": 番地, "to": "struct Sensor"}（+ "symbol"、char * は "string"。NUL の前で
               読むのをやめたら "string_end": "limit"（+ "string_limit": 使った上限のバイト数）か "unreadable"（その先が読めない））
    構造体・共用体 → {"kind": "struct", "members": {メンバー名: 値}}（無名のメンバーは外側に展開）
    配列 → {"kind": "array", "length": 要素数, "items": [...]}（items は max_elements まで。len(items) < length なら切った）
           文字の配列は + "string": "..."（NUL の前まで）
    読めない → {"kind": "unavailable", "reason": "理由"}

ビットフィールド（decode_bits）は int・enum・bool のどれか。
"""

from __future__ import annotations

import math
import struct
from collections.abc import Callable
from dataclasses import dataclass

from .typesys import (ArrayType, BaseType, CType, EnumType, FunctionType, Member, PointerType, StructType, VoidType,
                      is_char, strip, type_name)

__all__ = ["DecodeContext", "SCALAR_KINDS", "decode", "decode_bits", "encode", "scalar", "unavailable"]

SCALAR_KINDS = ("int", "float", "bool", "enum")   # value の欄に 1 つの数（真偽）を持つ種類
STRING_LIMIT = 256                # char * の先を読む最大のバイト数（DecodeContext.string_limit の既定。切ったら結果に印と上限）


@dataclass(frozen=True)
class DecodeContext:
    """解読を豊かにする任意の手段: ポインタの先の文字列を読む・番地をシンボル名にする。"""
    read_memory: Callable[[int, int], bytes] | None = None
    describe_address: Callable[[int], str | None] | None = None
    max_elements: int = 64
    string_limit: int = STRING_LIMIT


def unavailable(reason: str) -> dict:
    return {"kind": "unavailable", "reason": reason}


def scalar(value: dict) -> int | float | bool | str | None:
    """値の数（SCALAR_KINDS の value。浮動小数の NaN・無限大は "nan" 等の文字列のまま）。ほかの種類は None。"""
    return value["value"] if value["kind"] in SCALAR_KINDS else None


def decode(t: CType, data: bytes, context: DecodeContext | None = None) -> dict:
    context = context or DecodeContext()
    s = strip(t)
    if isinstance(s, BaseType):
        return _base(s, data[:s.size])
    if isinstance(s, EnumType):
        if s.size is None:
            return unavailable(f"大きさの分からない {type_name(s)}")
        return _enum(s, int.from_bytes(data[:s.size], "little", signed=s.signed))
    if isinstance(s, PointerType):
        return _pointer(s, int.from_bytes(data[:s.size], "little"), context)
    if isinstance(s, StructType):
        if s.size is None:
            return unavailable(f"不完全型 {type_name(s)}")
        return _struct(s, data, context)
    if isinstance(s, ArrayType):
        return _array(s, data, context)
    if isinstance(s, VoidType):
        return _bytes(data)
    return unavailable(f"{type_name(t)} は値として読まない")


def _bytes(data: bytes) -> dict:
    return {"kind": "bytes", "hex": data.hex()}


def _enum(t: EnumType, value: int) -> dict:
    return {"kind": "enum", "name": t.name_of(value), "value": value}


def _base(t: BaseType, data: bytes) -> dict:
    if t.encoding == "float":
        if t.size not in (4, 8):
            return _bytes(data)
        value = struct.unpack("<f" if t.size == 4 else "<d", data)[0]
        if math.isnan(value):
            value = "nan"
        elif math.isinf(value):
            value = "inf" if value > 0 else "-inf"
        return {"kind": "float", "value": value}
    if t.encoding == "bool":
        return {"kind": "bool", "value": int.from_bytes(data, "little") != 0}
    if t.encoding == "unknown":                        # DWARF に符号の種類が無い・知らない: 生のバイト
        return _bytes(data)
    return {"kind": "int", "value": int.from_bytes(data, "little", signed=t.signed)}


def _pointer(t: PointerType, address: int, context: DecodeContext) -> dict:
    out: dict = {"kind": "pointer", "address": address, "to": type_name(t.target)}
    target = strip(t.target)
    if context.describe_address and address and (isinstance(target, FunctionType) or not is_char(target)):
        symbol = context.describe_address(address)
        if symbol:
            out["symbol"] = symbol
    if is_char(target) and address and context.read_memory:
        text, end = _read_c_string(context.read_memory, address, context.string_limit)
        out["string"] = text
        if end == "limit":
            out.update(string_end="limit", string_limit=context.string_limit)
        elif end == "unreadable" and text is not None:
            out["string_end"] = "unreadable"
    return out


def _read_c_string(read: Callable[[int, int], bytes], address: int, limit: int) -> tuple[str | None, str]:
    """NUL までを読む（最大 limit バイト）。(文字列, 終わり方)。終わり方は "nul"・"limit"（NUL の前に上限）・
    "unreadable"（NUL の前で読めなくなった。1 バイトも読めなければ文字列は None）。往復を減らすため 32 バイトずつ。"""
    out = bytearray()
    while len(out) < limit:
        want = min(32, limit - len(out))
        try:
            chunk = read(address + len(out), want)
        except Exception:                                  # 区画の端: 1 バイトずつに落とす
            try:
                chunk = read(address + len(out), 1)
            except Exception:
                return (_text(bytes(out)) if out else None), "unreadable"
        end = chunk.find(0)
        if end >= 0:
            out += chunk[:end]
            return _text(bytes(out)), "nul"
        out += chunk
    return _text(bytes(out)), "limit"


def _text(data: bytes) -> str:
    return data.decode("ascii", errors="backslashreplace")


def _struct(t: StructType, data: bytes, context: DecodeContext) -> dict:
    members: dict = {}
    for m in t.members:
        value = decode_member(m, data, context)
        if m.name is None and value["kind"] == "struct":
            members.update(value["members"])               # 無名の構造体・共用体（C11）
        else:
            members[m.name or f"<anonymous@{m.offset}>"] = value
    return {"kind": "struct", "members": members}


def decode_member(m: Member, data: bytes, context: DecodeContext) -> dict:
    if m.bit_size is not None and m.bit_offset is not None:
        return decode_bits(m.type, data, m.bit_offset, m.bit_size)
    if m.bit_size is not None:
        return unavailable(f"ビットフィールド {m.name} の位置が分からない（記憶単位の大きさが DWARF に無い）")
    size = m.type.size
    if size is None:
        return unavailable(f"大きさの分からない {type_name(m.type)}")
    return decode(m.type, data[m.offset:m.offset + size], context)


def decode_bits(t: CType, data: bytes, bit_offset: int, bit_size: int) -> dict:
    """ビットフィールド: data の先頭からのビット位置 bit_offset から bit_size ビット（リトルエンディアン）。"""
    first, last = bit_offset // 8, (bit_offset + bit_size + 7) // 8
    raw = int.from_bytes(data[first:last], "little") >> (bit_offset % 8)
    value = raw & ((1 << bit_size) - 1)
    s = strip(t)
    signed = (isinstance(s, BaseType) and s.signed) or (isinstance(s, EnumType) and s.signed)
    if signed and value >> (bit_size - 1):
        value -= 1 << bit_size
    if isinstance(s, EnumType):
        return _enum(s, value)
    if isinstance(s, BaseType) and s.encoding == "bool":
        return {"kind": "bool", "value": value != 0}
    return {"kind": "int", "value": value}


def _array(t: ArrayType, data: bytes, context: DecodeContext) -> dict:
    element = t.element
    size = element.size
    if size is None or size == 0:
        return unavailable(f"要素の大きさの分からない {type_name(t)}")
    length = t.length if t.length is not None else len(data) // size
    shown = min(length, context.max_elements)
    out: dict = {"kind": "array", "length": length,
                 "items": [decode(element, data[i * size:(i + 1) * size], context) for i in range(shown)]}
    if is_char(element):
        raw = data[:length * size]
        end = raw.find(0)
        out["string"] = _text(raw if end < 0 else raw[:end])
    return out


# --- 書き込み ---------------------------------------------------------------------------------

def encode(t: CType, value: object) -> bytes:
    """基本型・列挙（名前か数）・ポインタ（数）を、その型の大きさのバイト列にする。"""
    s = strip(t)
    if isinstance(s, EnumType):
        if s.size is None:
            raise TypeError(f"{type_name(t)} の大きさが分からない（DWARF に無い）")
        number = s.value_of(value) if isinstance(value, str) else _integer(value)
        return _int_bytes(number, s.size, t)
    if isinstance(s, PointerType):
        return _int_bytes(_integer(value), s.size, t)
    if isinstance(s, BaseType):
        if s.encoding == "unknown":
            raise TypeError(f"{type_name(t)} の符号の種類が分からない（DWARF に無い）")
        if s.encoding == "float":
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise TypeError(f"{type_name(t)} には数を書く")
            return struct.pack("<f" if s.size == 4 else "<d", float(value))
        if s.encoding == "bool":
            return (1 if value else 0).to_bytes(s.size, "little")
        return _int_bytes(_integer(value), s.size, t)
    raise TypeError(f"{type_name(t)} には値を書けない（基本型・列挙・ポインタだけ）")


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"整数を書く（{value!r}）")
    return value


def _int_bytes(value: int, size: int, t: CType) -> bytes:
    bits = size * 8
    s = strip(t)
    signed = isinstance(s, (BaseType, EnumType)) and s.signed
    lo, hi = (-(1 << (bits - 1)), 1 << (bits - 1)) if signed else (0, 1 << bits)
    if not lo <= value < hi:
        raise ValueError(f"{value} は {type_name(t)}（{size} バイト）に入らない")
    return (value & ((1 << bits) - 1)).to_bytes(size, "little")
