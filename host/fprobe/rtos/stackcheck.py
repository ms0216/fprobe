"""スタックの塗りつぶし（RTOS がタスクを作るときに決めた値で埋める）から、まだ使われていない深さを数える。

下へ伸びるスタック: 底（最も低い番地）から上へ、塗りの値が続くバイト数 = これまでに一度も届いていない深さ。
塗りの値と同じ値を書いたスタックは使っていないと数える（FreeRTOS の uxTaskGetStackHighWaterMark と同じ限界）。
塗りは 1 バイトの値（FreeRTOS の 0xA5 等）か、底から繰り返すバイト列（NuttX の語 0xdeadbeef = b"\\xef\\xbe\\xad\\xde"）。
"""

from __future__ import annotations

from collections.abc import Callable

from ..dwarf.access import AccessError
from .model import StackInfo

__all__ = ["CHUNK", "GUARD", "check_stack", "untouched_bytes"]

CHUNK = 256                       # 1 回に読むバイト数
GUARD = 16                        # 底のこのバイト数が塗りのままかを見る（FreeRTOS の方法 2 と同じ幅）


def _pattern(fill: int | bytes) -> bytes:
    """塗り → 底から繰り返すバイト列。"""
    return bytes([fill]) if isinstance(fill, int) else bytes(fill)


def untouched_bytes(read: Callable[[int, int], bytes], base: int, limit: int, fill: int | bytes) -> int:
    """base から上へ、塗りのバイトが続く数（limit まで）。語の塗りは base からの位置で比べる。"""
    pattern = _pattern(fill)
    count = 0
    while count < limit:
        n = min(CHUNK, limit - count)
        data = read(base + count, n)
        for i, b in enumerate(data):
            if b != pattern[(count + i) % len(pattern)]:
                return count + i
        count += n
    return count


def check_stack(read: Callable[[int, int], bytes], base: int, end: int | None, pointer: int | None,
                fill: int | bytes) -> StackInfo:
    """スタックの範囲と SP から StackInfo（塗りの残り・SP が範囲の外か）を作る。読めなければ fill = unknown。
    底が塗りでないときは、底の少し上に塗りが残っているか（fill_above）を事実として返す（あふれたとは推定しない）。"""
    pattern = _pattern(fill)
    info = StackInfo(base, end, pointer)
    if pointer is not None and pointer < base:
        info.overflow = True
    try:
        bottom = read(base, GUARD)
    except AccessError:
        return info
    if bottom != (pattern * GUARD)[:GUARD]:
        info.fill = "none"                          # 塗らない設定か、底まで使った（区別できない）
        needle = (pattern * 4)[:max(4, len(pattern))]
        if pointer is not None and end is not None:
            info.fill_above = needle in _sample(read, base, pointer)
        return info
    info.fill = "intact"
    limit = (pointer if pointer is not None else end if end is not None else base + CHUNK) - base
    try:
        info.untouched = untouched_bytes(read, base, max(limit, 0), pattern)
    except AccessError:
        info.fill = "unknown"
    return info


def _sample(read: Callable[[int, int], bytes], base: int, pointer: int) -> bytes:
    """底の少し上（GUARD から CHUNK まで、SP より下）を読む。"""
    hi = min(pointer, base + CHUNK)
    if hi <= base + GUARD:
        return b""
    try:
        return read(base + GUARD, hi - base - GUARD)
    except AccessError:
        return b""
