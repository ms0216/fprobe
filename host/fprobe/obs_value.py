"""FPGA の観測の値の読み方と比べ方。FPGA に書く一致（obs_condition.compile_match）とホストで数える条件（obs_condition）が
同じ意味で値を扱うように、ここ 1 か所に置く: 数は値で比べる（16・"16"・"0x10" は同じ）、"0" は 0、列挙の名前は文字で比べる。"""

from typing import Any

__all__ = ["is_on", "number", "same"]


def number(x: Any) -> int | None:
    """数（int・"16"・"0x10"・"0b101"）なら整数、列挙の名前などは None。"""
    if isinstance(x, bool):
        return int(x)
    if isinstance(x, int):
        return x
    if isinstance(x, str):
        try:
            return int(x, 0)
        except ValueError:
            pass
        try:
            return int(x, 10) if x.strip().lstrip("+-").isdigit() else None    # 先頭 0 の十進（08）は十進
        except ValueError:
            return None
    return None


def is_on(x: Any) -> bool:
    """0 以外（"0"・"0x0" も 0）。列挙の名前は 0 以外の値の名前として「オン」。None（値が無い）は偽。"""
    if x is None:
        return False
    n = number(x)
    return True if n is None else n != 0


def same(cur: Any, want: str | None) -> bool:
    """数同士は値で、そうでなければ文字で比べる。値が無い（None）ものは一致しない。"""
    if cur is None or want is None:
        return False
    a, b = number(cur), number(want)
    return a == b if a is not None and b is not None else str(cur) == want
