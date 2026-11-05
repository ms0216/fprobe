"""DWARF 索引の部品: 変数・関数・スコープ・フレームの様子と、名前の曖昧さの誤り（DebugInfo が返すデータ）。

索引そのもの（DIE を辿って作る・引く・位置式を評価する）は info.py の DebugInfo。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .location import Place
from .typesys import CType

if TYPE_CHECKING:
    from .info import DebugInfo

__all__ = ["AmbiguousNameError", "FrameView", "Function", "Scope", "Variable", "in_file", "is_file", "location_key"]


class AmbiguousNameError(LookupError):
    """同じ名前の変数が複数あって 1 つに決まらない（黙って 1 つを選ばない）。candidates = 候補（name・kind・
    function・file・line）。"<ファイル>::<名前>"・"<ファイル>::<関数>::<名前>" で選べる。"""

    def __init__(self, name: str, candidates: list[Variable]):
        self.name = name
        self.candidates = [{"name": v.name, "kind": v.kind, "function": v.function, "file": v.decl_file,
                            "line": v.decl_line} for v in candidates]
        where = ", ".join(f"{c['file'] or '?'}:{c['line'] or '?'}" for c in self.candidates)
        examples = " / ".join(dict.fromkeys(
            f"{c['file'] or '?'}::{c['function'] + '::' if c['function'] else ''}{c['name']}" for c in self.candidates))
        super().__init__(f"{name!r} は {len(candidates)} 個ある（{where}）。ファイルで選ぶ: {examples}")

    def __str__(self) -> str:                    # KeyError と違い、引用符を付けずに文で
        return self.args[0]


def in_file(variable: Variable, file: str) -> bool:
    """変数の宣言のファイルが file（パスの末尾の一致。区切りは / と \\ を同じに）か。"""
    path = (variable.decl_file or "").replace("\\", "/")
    want = file.replace("\\", "/")
    return path == want or path.endswith("/" + want)


def location_key(variable: Variable) -> object:
    """同じ実体かを見分ける鍵: 位置式のバイト列（番地を含む）。無ければその変数だけ。"""
    attributes = getattr(variable.die, "attributes", None) or {}
    attr = attributes.get("DW_AT_location")
    return tuple(attr.value) if attr is not None and isinstance(attr.value, (list, bytes)) else id(variable)


def is_file(qualifier: str) -> bool:
    """"<ファイル>::<名前>" の前置きか（C の名前に無い . / \\ を含む）。"""
    return any(ch in qualifier for ch in "./\\")


@dataclass
class FrameView:
    """変数の場所を評価するための、1 つのフレームの様子。

    pc: 変数・スコープを引く番地（**リンク時の番地**。呼び出し元のフレームでは戻り先 − 1 = 呼び出し命令の中）。
    read_register: DWARF のレジスタ番号 → 値（復元できなければ AccessError / NotAvailable）。
    load_bias: 実行時の番地 − リンク時の番地（PIE・共有ライブラリ）。大域・静的変数の番地（DW_OP_addr・addrx）に足す。
    """
    pc: int | None
    read_memory: Callable[[int, int], bytes]
    read_register: Callable[[int], int]
    cfa: int | None = None
    load_bias: int = 0


@dataclass(eq=False)
class Variable:
    name: str
    kind: str                     # global / static / parameter / local
    die: object = field(repr=False)
    info: "DebugInfo" = field(repr=False)
    function: str | None = None
    decl_file: str | None = None
    decl_line: int | None = None

    @property
    def type(self) -> CType:
        return self.info.types.type_of(self.die)

    def place(self, frame: FrameView | None = None) -> Place:
        return self.info.place_of(self.die, frame)

    def __str__(self) -> str:
        return f"{self.kind} {self.name}"


@dataclass(eq=False)
class Function:
    name: str
    ranges: tuple[tuple[int, int], ...]
    die: object = field(repr=False)
    decl_file: str | None = None
    decl_line: int | None = None

    @property
    def low(self) -> int:
        return min(lo for lo, _ in self.ranges)

    def contains(self, pc: int) -> bool:
        return any(lo <= pc < hi for lo, hi in self.ranges)


@dataclass
class Scope:
    """PC から見える関数の変数。locals は外側のブロックから内側の順（同じ名前は内側が勝つ）。"""
    function: Function
    parameters: list[Variable]
    locals: list[Variable]
    statics: list[Variable]       # 関数の中の static

    def lookup(self, name: str) -> Variable | None:
        for v in reversed(self.locals):
            if v.name == name:
                return v
        for v in self.parameters + self.statics:
            if v.name == name:
                return v
        return None

