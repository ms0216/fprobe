"""カーネルオブジェクトを変数の型から探す（RTOS に依らない部分）。

FreeRTOS はオブジェクトの一覧を持たず、Zephyr のオブジェクトは静的な変数。どちらも「型がオブジェクトの構造体か、
それを指すハンドル」の大域・ファイルの static 変数を DWARF から探せば、名前（変数の式）付きで見つかる。
構造体・配列の中も辿る（`app.queues[2]`）。辿る深さ・配列の要素・読むポインタの数には上限がある（大きなアプリでも止まる）。

    found = typed_variables(info, mem, {"QueueDefinition": "queue", "xSTATIC_QUEUE": "queue"})
    [(f.expression, f.address, f.family) for f in found]
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from ..dwarf.access import AccessError
from ..dwarf.info import DebugInfo
from ..dwarf.location import MemoryPlace
from ..dwarf.typesys import ArrayType, CType, PointerType, QualifiedType, StructType, TypedefType, strip
from .structs import Memory

__all__ = ["Found", "typed_variables"]

MAX_DEPTH = 4                    # 構造体・配列の入れ子をこの深さまで
MAX_ELEMENTS = 64                # 配列の要素をこの数まで
MAX_POINTERS = 4096              # 読むハンドル（ポインタ）の数


@dataclass(frozen=True)
class Found:
    expression: str              # 変数の式（"cmd_queue"・"app.queues[2]"）
    address: int                 # オブジェクトの番地（ハンドルなら指す先）
    family: str                  # 呼び出し側の分類（structs の値）
    pointer: bool                # ハンドル（指す先がオブジェクト）か、変数そのものがオブジェクトか


def _family(t: CType, structs: Mapping[str, str]) -> str | None:
    """型（typedef の名前か、構造体の名前）が structs にあればその分類。"""
    while isinstance(t, (TypedefType, QualifiedType)):
        if isinstance(t, TypedefType) and t.name in structs:
            return structs[t.name]
        t = t.target
    s = strip(t)
    if isinstance(s, StructType) and s.name in structs:
        return structs[s.name]
    return None


def typed_variables(info: DebugInfo, mem: Memory, structs: Mapping[str, str], *, max_depth: int = MAX_DEPTH,
                    max_elements: int = MAX_ELEMENTS, max_pointers: int = MAX_POINTERS,
                    exclude: frozenset[str] = frozenset()) -> list[Found]:
    """大域・ファイルの static 変数のうち、型が structs の構造体（変数そのものがオブジェクト）か、それを指すポインタ
    （ハンドル。0 でないもの）。structs は 構造体か typedef の名前 → 分類。読めないポインタは飛ばす。
    exclude の変数（カーネルの表など、名前を与えないもの）は辿らない。"""
    out: list[Found] = []
    memo: dict[int, bool] = {}
    budget = max_pointers

    def may_contain(t: CType, depth: int) -> bool:
        """この型の中に（深さの上限までに）オブジェクトかハンドルがあり得るか。"""
        if _family(t, structs) is not None:
            return True
        s = strip(t)
        if isinstance(s, PointerType):
            return _family(s.target, structs) is not None
        if depth >= max_depth:
            return False
        key = id(s)
        if key not in memo:
            memo[key] = False                                   # 自分を含む型（あり得ないが）でも止まる
            if isinstance(s, StructType) and s.kind == "struct":
                memo[key] = any(m.bit_size is None and may_contain(m.type, depth + 1) for m in s.members)
            elif isinstance(s, ArrayType) and s.length:
                memo[key] = may_contain(s.element, depth + 1)
        return memo[key]

    def walk(t: CType, address: int, expression: str, depth: int) -> None:
        nonlocal budget
        family = _family(t, structs)
        if family is not None:
            out.append(Found(expression, address, family, False))
            return
        s = strip(t)
        if isinstance(s, PointerType):
            family = _family(s.target, structs)
            if family is None or budget <= 0:
                return
            budget -= 1
            try:
                value = mem.u32(address)
            except AccessError:
                return
            if value:
                out.append(Found(expression, value, family, True))
            return
        if depth >= max_depth or not may_contain(s, depth):
            return
        if isinstance(s, StructType) and s.kind == "struct":
            for m in s.members:
                if m.bit_size is None:
                    walk(m.type, address + m.offset, f"{expression}.{m.name}" if m.name else expression, depth + 1)
        elif isinstance(s, ArrayType) and s.length:
            size = s.element.size or 0
            for i in range(min(s.length, max_elements)):
                walk(s.element, address + i * size, f"{expression}[{i}]", depth + 1)

    for var in info.global_variables():
        if var.name in exclude:
            continue
        try:
            t = var.type
            if not may_contain(t, 0):
                continue
            place = var.place()
        except (KeyError, ValueError, NotImplementedError):
            continue
        if isinstance(place, MemoryPlace):
            walk(t, place.address, var.name, 0)
    return out
