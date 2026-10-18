"""バイト列 + 型 → JSON にできる値（decode）と、値 → バイト列（encode）。リトルエンディアン。

値の形（AI がそのまま読め、UI が木で表示できる）:

    整数・文字 → int / bool → bool / float・double → float（NaN・無限大は "nan" "inf" "-inf"）
    列挙 → {"enum": "MODE_RUN", "value": 1}
    ポインタ → {"pointer": 番地, "to": "struct Sensor"}（+ "symbol"、char * は "string"）
    構造体・共用体 → {メンバー名: 値}（無名のメンバーは外側に展開）
    配列 → [...]（max_elements を超えたら {"length": n, "head": [...]}）
    文字の配列 → {"string": "...", "values": [...]}
    読めない → {"unavailable": "理由"}
"""

from __future__ import annotations

import math
import struct
from collections.abc import Callable
from dataclasses import dataclass

from .typesys import (ArrayType, BaseType, CType, EnumType, FunctionType, Member, PointerType, StructType, VoidType,
                      is_char, strip, type_name)

__all__ = ["DecodeContext", "decode", "decode_bits", "encode", "unavailable"]

STRING_LIMIT = 256                # char * の先を読む最大のバイト数


@dataclass(frozen=True)
class DecodeContext:
    """解読を豊かにする任意の手段: ポインタの先の文字列を読む・番地をシンボル名にする。"""
    read_memory: Callable[[int, int], bytes] | None = None
    describe_address: Callable[[int], str | None] | None = None
    max_elements: int = 64


def unavailable(reason: str) -> dict:
    return {"unavailable": reason}


def decode(t: CType, data: bytes, context: DecodeContext | None = None) -> object:
    context = context or DecodeContext()
    s = strip(t)
    if isinstance(s, BaseType):
        return _base(s, data[:s.size])
    if isinstance(s, EnumType):
        value = int.from_bytes(data[:s.size], "little", signed=s.signed)
        return {"enum": s.name_of(value), "value": value}
    if isinstance(s, PointerType):
        return _pointer(s, int.from_bytes(data[:s.size], "little"), context)
    if isinstance(s, StructType):
        if s.size is None:
            return unavailable(f"不完全型 {type_name(s)}")
        return _struct(s, data, context)
    if isinstance(s, ArrayType):
        return _array(s, data, context)
    if isinstance(s, VoidType):
        return data.hex()
    return unavailable(f"{type_name(t)} は値として読まない")


def _base(t: BaseType, data: bytes) -> object:
    if t.encoding == "float":
        if t.size not in (4, 8):
            return data.hex()
        value = struct.unpack("<f" if t.size == 4 else "<d", data)[0]
        if math.isnan(value):
            return "nan"
        if math.isinf(value):
            return "inf" if value > 0 else "-inf"
        return value
    if t.encoding == "bool":
        return int.from_bytes(data, "little") != 0
    return int.from_bytes(data, "little", signed=t.signed)


def _pointer(t: PointerType, address: int, context: DecodeContext) -> dict:
    out: dict = {"pointer": address, "to": type_name(t.target)}
    target = strip(t.target)
    if context.describe_address and address and (isinstance(target, FunctionType) or not is_char(target)):
        symbol = context.describe_address(address)
        if symbol:
            out["symbol"] = symbol
    if is_char(target) and address and context.read_memory:
        out["string"] = _read_c_string(context.read_memory, address)
    return out


def _read_c_string(read: Callable[[int, int], bytes], address: int) -> str | None:
    """NUL までを読む（最大 STRING_LIMIT バイト。読めなければ None）。読み手の往復を減らすため 32 バイトずつ。"""
    out = bytearray()
    while len(out) < STRING_LIMIT:
        try:
            chunk = read(address + len(out), 32)
        except Exception:                                  # 区画の端: 1 バイトずつに落とす
            try:
                chunk = read(address + len(out), 1)
            except Exception:
                return _text(bytes(out)) if out else None
        end = chunk.find(0)
        if end >= 0:
            out += chunk[:end]
            break
        out += chunk
    return _text(bytes(out))


def _text(data: bytes) -> str:
    return data.decode("ascii", errors="backslashreplace")


def _struct(t: StructType, data: bytes, context: DecodeContext) -> dict:
    out: dict = {}
    for m in t.members:
        value = decode_member(m, data, context)
        if m.name is None and isinstance(value, dict) and isinstance(strip(m.type), StructType):
            out.update(value)                              # 無名の構造体・共用体（C11）
        else:
            out[m.name or f"<anonymous@{m.offset}>"] = value
    return out


def decode_member(m: Member, data: bytes, context: DecodeContext) -> object:
    if m.bit_size is not None and m.bit_offset is not None:
        return decode_bits(m.type, data, m.bit_offset, m.bit_size)
    size = m.type.size
    if size is None:
        return unavailable(f"大きさの分からない {type_name(m.type)}")
    return decode(m.type, data[m.offset:m.offset + size], context)


def decode_bits(t: CType, data: bytes, bit_offset: int, bit_size: int) -> object:
    """ビットフィールド: data の先頭からのビット位置 bit_offset から bit_size ビット（リトルエンディアン）。"""
    first, last = bit_offset // 8, (bit_offset + bit_size + 7) // 8
    raw = int.from_bytes(data[first:last], "little") >> (bit_offset % 8)
    value = raw & ((1 << bit_size) - 1)
    s = strip(t)
    signed = (isinstance(s, BaseType) and s.signed) or (isinstance(s, EnumType) and s.signed)
    if signed and value >> (bit_size - 1):
        value -= 1 << bit_size
    if isinstance(s, EnumType):
        return {"enum": s.name_of(value), "value": value}
    if isinstance(s, BaseType) and s.encoding == "bool":
        return value != 0
    return value


def _array(t: ArrayType, data: bytes, context: DecodeContext) -> object:
    element = t.element
    size = element.size
    if size is None or size == 0:
        return unavailable(f"要素の大きさの分からない {type_name(t)}")
    length = t.length if t.length is not None else len(data) // size
    shown = min(length, context.max_elements)
    values = [decode(element, data[i * size:(i + 1) * size], context) for i in range(shown)]
    if is_char(element):
        raw = data[:length * size]
        end = raw.find(0)
        return {"string": _text(raw if end < 0 else raw[:end]),
                "values": values if shown == length else {"length": length, "head": values}}
    return values if shown == length else {"length": length, "head": values}


# --- 書き込み ---------------------------------------------------------------------------------

def encode(t: CType, value: object) -> bytes:
    """基本型・列挙（名前か数）・ポインタ（数）を、その型の大きさのバイト列にする。"""
    s = strip(t)
    if isinstance(s, EnumType):
        number = s.value_of(value) if isinstance(value, str) else _integer(value)
        return _int_bytes(number, s.size, t)
    if isinstance(s, PointerType):
        return _int_bytes(_integer(value), s.size, t)
    if isinstance(s, BaseType):
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
