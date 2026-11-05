"""ベンダーの道具（Gowin EDA・GHDL・Vivado 等）の所在解決の共通部品。

道具ごとに違うのは、探す場所の候補（環境変数・既定の場所・PATH）と、そこに無ければならないファイルだけ。
候補を順に試して最初に揃ったものを返し、どれも揃わなければ候補ごとの不足を並べて ToolchainNotFound にする。
"""

from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TypeVar

__all__ = ["ToolchainNotFound", "first_usable"]

T = TypeVar("T")


class ToolchainNotFound(RuntimeError):
    """必要なツール（Gowin・Vivado・GHDL 等）が見つからない。"""


def first_usable(homes: Iterable[Path], make: Callable[[Path], T], missing: Callable[[T], list[Path]], *,
                 what: str, env_var: str) -> T:
    """homes を順に make で道具にして、missing が空（必要なファイルが揃う）の最初のものを返す。"""
    problems = []
    for home in homes:
        toolchain = make(home)
        absent = missing(toolchain)
        if not absent:
            return toolchain
        problems.append(f"{home}: " + ", ".join(p.name for p in absent) + " が無い")
    detail = "; ".join(problems) if problems else "候補なし"
    raise ToolchainNotFound(f"{what} が見つからない（{env_var} で指定可能）: {detail}")
