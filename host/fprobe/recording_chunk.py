"""波形のかたまりのファイル（recording の waveform/ の中）の形式: 見出しの行と本体の読み書き。"""

import json
import sys
from array import array
from typing import Any

from .la import Transitions
from .recording_model import Chunk

# 生の拍のかたまり（形式 2）のファイル: 拡張子と、見出しの行の頭の印。古い読み手（形式 1 しか知らない）は見出しの行を JSON として読む
# ので、印があると JSON として読めず、必ず「読めない」誤りで止まる（変化点と読み違えない）。見出しの欄を足しただけでは、
# 欄を見ない古い読み手が通ってしまう（見出しだけを読む口: chunk_skew）。仕様 2026-11-03-recording-raw-waveform-design.md §3
RAW_SUFFIX = ".fpraw"
RAW_MAGIC = b"FPRAW2 "


def head_of(line: bytes) -> dict:
    """かたまりのファイルの見出しの行（改行の前まで）。形式 2 は RAW_MAGIC の後ろが JSON。"""
    if line.startswith(RAW_MAGIC):
        return json.loads(line[len(RAW_MAGIC):])
    return json.loads(line)


def raw_bytes(values: Any, code: str) -> bytes | memoryview:
    """かたまりのファイルに書く値の列のバイト列（array(code) の tobytes と同じ）。符号なし整数の同じ幅の連続した
    バッファ（array・numpy の配列）は写さずにそのまま、ほかは array にして。"""
    try:
        view = memoryview(values)
    except TypeError:
        return array(code, values).tobytes()
    fmt = view.format
    if fmt[:1] in ("@", "=") or (fmt[:1] == "<" and sys.byteorder == "little"):
        fmt = fmt[1:]                                         # 自分の機械の並びのバイト順
    if view.ndim == 1 and view.c_contiguous and fmt in ("B", "H", "I", "L", "Q") and view.itemsize == array(code).itemsize:
        return view.cast("B")
    return array(code, values).tobytes()


def decode_chunk(data: bytes, chunk: Chunk, numpy: bool) -> Transitions:
    """かたまりのファイルの中身を変化点にする。見出しの format で形式を分ける: 無し = 変化点（値の列 + 開始の列）、
    2 = 生の拍（stream_transitions で変化点にする。かたまりの頭が続いている値と同じなら頭に (previous, 0) を足す:
    変化点の形式と同じ変化点になる）。numpy = True なら写さずに numpy の配列（numpy がある時だけ）。"""
    from .la_wave import _array_of, _np, stream_transitions
    newline = data.index(b"\n")
    head = head_of(data[:newline])
    skew = tuple(head.get("channel_skew", ()))                  # 古い記録（ずれの無いもの）は ()
    if head.get("format", 1) == 2:
        values, starts = stream_transitions(memoryview(data)[newline + 1:], head["rle"], head["pack"], 0, head["previous"])
        previous = head["previous"]
        if previous is not None and (not len(starts) or int(starts[0]) != 0):
            if _np is not None:
                values = _np.concatenate([_np.array([previous], dtype=_np.uint16), values])
                starts = _np.concatenate([_np.zeros(1, dtype=_np.uint64), starts])
            else:
                values, starts = array("H", [previous]) + values, array("Q", [0]) + starts
        if not numpy and _np is not None:
            values, starts = _array_of("H", values), _array_of("Q", starts)
        return Transitions(values, starts, chunk.length, 0, 0, head["period_s"], head["channels"], skew)
    n = head["count"]
    if numpy:
        values = _np.frombuffer(data, dtype=_np.uint16, count=n, offset=newline + 1)
        starts = _np.frombuffer(data, dtype=_np.uint64, count=n, offset=newline + 1 + 2 * n)
    else:
        values = array("H")
        values.frombytes(data[newline + 1:newline + 1 + 2 * n])
        starts = array("Q")
        starts.frombytes(data[newline + 1 + 2 * n:newline + 1 + 10 * n])
    return Transitions(values, starts, head["length"], head["trigger"], 0, head["period_s"], head["channels"], skew)
