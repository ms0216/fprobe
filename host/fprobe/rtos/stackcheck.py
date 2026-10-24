"""スタックの塗りつぶし（RTOS がタスクを作るときに決めた値で埋める）から、まだ使われていない深さを数える。

下へ伸びるスタック: 底（最も低い番地）から上へ、塗りの値が続くバイト数 = これまでに一度も届いていない深さ。
塗りの値と同じ値を書いたスタックは使っていないと数える（FreeRTOS の uxTaskGetStackHighWaterMark と同じ限界）。
塗りは 1 バイトの値（FreeRTOS の 0xA5 等）か、底から繰り返すバイト列（NuttX の語 0xdeadbeef = b"\\xef\\xbe\\xad\\xde"）。
"""

from __future__ import annotations

from collections.abc import Callable

from ..dwarf.access import AccessError
from .model import StackInfo

__all__ = ["CHUNK", "check_stack", "untouched_bytes"]

CHUNK = 256                       # 1 回に読むバイト数


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
    untouched = 底から上へ塗りの値が続くバイト数（SP まで。事実）。底のバイトが塗りなら fill = intact（底の何バイトが
    塗りなら「塗りのまま」とするかの幅は持たない。塗りと同じ値を書いた所も塗りに数える）。底が塗りでないときは
    fill = none（塗らない設定か、底まで使った。区別できない）で、底から上（SP の下、CHUNK バイトまで）で最も長い
    塗りの続き（fill_run_above）を事実として返す（あふれたとは推定しない）。"""
    pattern = _pattern(fill)
    info = StackInfo(base, end, pointer)
    if pointer is not None and pointer < base:
        info.overflow = True
    limit = (pointer if pointer is not None else end if end is not None else base + CHUNK) - base
    try:
        untouched = untouched_bytes(read, base, max(limit, 1), pattern)
    except AccessError:
        return info
    if untouched == 0:
        info.fill = "none"
        if pointer is not None and end is not None:
            info.fill_run_above = _longest_run(_sample(read, base, pointer), pattern)
        return info
    info.fill = "intact"
    info.untouched = min(untouched, max(limit, 0))
    return info


def _sample(read: Callable[[int, int], bytes], base: int, pointer: int) -> bytes:
    """底から上（CHUNK まで、SP より下）を読む。"""
    hi = min(pointer, base + CHUNK)
    if hi <= base:
        return b""
    try:
        return read(base, hi - base)
    except AccessError:
        return b""


def _longest_run(data: bytes, pattern: bytes) -> dict | None:
    """data（底から）の中で、底からの位置で塗りと一致するバイトが最も長く続く所。無ければ None。"""
    best = (0, 0)
    start = None
    for i, b in enumerate(data):
        if b == pattern[i % len(pattern)]:
            start = i if start is None else start
            if i + 1 - start > best[1]:
                best = (start, i + 1 - start)
        else:
            start = None
    return None if best[1] == 0 else {"offset": best[0], "length": best[1], "searched": len(data)}
