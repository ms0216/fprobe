"""バイナリの応答（WebSocket のバイナリのフレーム）。JSON-RPC 2.0 の応答に、型付きの配列（区画）を添えて 1 通で送る。

仕様（docs/superpowers/specs/2026-10-18-timeline-tiles-design.md §3）。数はすべてリトルエンディアン。

    0       4   magic "FPB1"
    4       4   u32 = JSON の長さ N
    8       N   JSON-RPC の応答（UTF-8）。result.sections = {名前: {"dtype", "offset", "length"}}
    8+N         0 で 8 バイト境界まで埋める
    ...         区画（それぞれ 8 バイト境界から。offset はフレームの頭からのバイト数、length は要素の数）

- 応答の id・誤りの形は JSON-RPC 2.0 のまま（誤りはテキストのフレームで返す。バイナリになるのは成功の結果だけ）。
- 区画の型: f64 / u64 / u32 / u16 / u8。8 バイト境界なので、ブラウザは複写せずに Float64Array 等で見られる。
- 仕様で "binary": true のメソッドだけがこの形で答える（実装は BinaryResult を返す）。
"""

from __future__ import annotations

import json
import struct
import sys
from array import array
from dataclasses import dataclass, field
from typing import Any

__all__ = ["DTYPES", "MAGIC", "BinaryResult", "decode_frame", "encode_frame"]

MAGIC = b"FPB1"
_ALIGN = 8
# 区画の型 → array の型コード（要素の大きさが合うものを選ぶ。'L' は OS で 4 か 8 バイトなので使わない）
DTYPES = {"f64": "d", "u64": "Q", "u32": "I", "u16": "H", "u8": "B"}
_SIZES = {"f64": 8, "u64": 8, "u32": 4, "u16": 2, "u8": 1}


def _dtype_of(data: array | bytes | bytearray | memoryview) -> str:
    if isinstance(data, (bytes, bytearray, memoryview)):
        return "u8"
    for name, code in DTYPES.items():
        if data.typecode == code and data.itemsize == _SIZES[name]:
            return name
    raise TypeError(f"区画にできない配列の型 {data.typecode!r}（{data.itemsize} バイト）")


@dataclass
class BinaryResult:
    """バイナリで返す結果: JSON にする部分 result と、名前付きの区画 sections（array か bytes）。"""
    result: dict[str, Any]
    sections: dict[str, array | bytes] = field(default_factory=dict)


def _pad(n: int) -> int:
    return -n % _ALIGN


def encode_frame(response: dict, sections: dict[str, array | bytes]) -> bytes:
    """JSON-RPC の応答（result は辞書）と区画を 1 つのフレームにする。result.sections に区画の位置を書き足す。"""
    layout, blobs = {}, []
    for name, data in sections.items():
        dtype = _dtype_of(data)
        if isinstance(data, array):
            if sys.byteorder == "big" and data.itemsize > 1:
                data = array(data.typecode, data)
                data.byteswap()
            raw = data.tobytes()
        else:
            raw = bytes(data)
        layout[name] = {"dtype": dtype, "length": len(raw) // _SIZES[dtype]}
        blobs.append((name, raw))
    # JSON の長さで区画の位置が決まり、位置が JSON に入る: 位置を仮に書いた長さで決め、桁が増えたらやり直す
    offsets = {name: 0 for name in layout}
    while True:
        result = {**response["result"], "sections": {n: {**layout[n], "offset": offsets[n]} for n in layout}}
        head = json.dumps({**response, "result": result}, ensure_ascii=False, separators=(",", ":")).encode()
        position = 8 + len(head) + _pad(8 + len(head))
        placed = {}
        for name, raw in blobs:
            placed[name] = position
            position += len(raw) + _pad(len(raw))
        if placed == offsets:
            break
        offsets = placed
    out = bytearray(MAGIC + struct.pack("<I", len(head)) + head)
    out += bytes(_pad(len(out)))
    for _, raw in blobs:
        out += raw
        out += bytes(_pad(len(raw)))
    return bytes(out)


def decode_frame(frame: bytes) -> tuple[dict, dict[str, array]]:
    """フレーム → (JSON-RPC の応答, {区画の名前: array})。"""
    if frame[:4] != MAGIC:
        raise ValueError("fprobe のバイナリのフレームでない")
    (n,) = struct.unpack_from("<I", frame, 4)
    response = json.loads(frame[8:8 + n].decode())
    data = {}
    for name, s in response.get("result", {}).get("sections", {}).items():
        a = array(DTYPES[s["dtype"]])
        a.frombytes(frame[s["offset"]:s["offset"] + s["length"] * _SIZES[s["dtype"]]])
        if sys.byteorder == "big" and a.itemsize > 1:
            a.byteswap()
        data[name] = a
    return response, data
