"""カーネルオブジェクトを変数の型から探す（RTOS に依らない部分）。

FreeRTOS はオブジェクトの一覧を持たず、Zephyr のオブジェクトは静的な変数。どちらも「型がオブジェクトの構造体か、
それを指すハンドル」の大域・ファイルの static 変数を DWARF から探せば、名前（変数の式）付きで見つかる。
構造体・配列の中も辿る（`app.queues[2]`）。辿る深さ・配列の要素・読むポインタの数には上限がある（大きなアプリでも止まる）。
上限で見ていない所・読めなかった所は黙って捨てず、結果（TypedSearch）に事実として残す。

    search = typed_variables(info, mem, {"QueueDefinition": "queue", "xSTATIC_QUEUE": "queue"})
    [(f.expression, f.address, f.family) for f in search.found]
    kernel.warnings.extend(search.notes())
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from ..dwarf.access import AccessError
from ..dwarf.info import DebugInfo
from ..dwarf.location import MemoryPlace
from ..dwarf.typesys import ArrayType, CType, PointerType, QualifiedType, StructType, TypedefType, strip
from .structs import Memory

__all__ = ["Found", "TypedSearch", "typed_variables"]

MAX_DEPTH = 4                    # 構造体・配列の入れ子をこの深さまで
MAX_ELEMENTS = 64                # 配列の要素をこの数まで
MAX_POINTERS = 4096              # 読むハンドル（ポインタ）の数


@dataclass(frozen=True)
class Found:
    expression: str              # 変数の式（"cmd_queue"・"app.queues[2]"）
    address: int                 # オブジェクトの番地（ハンドルなら指す先）
    family: str                  # 呼び出し側の分類（structs の値）
    pointer: bool                # ハンドル（指す先がオブジェクト）か、変数そのものがオブジェクトか


@dataclass
class TypedSearch:
    """探した結果と、上限・読めないことで見ていない所の事実。"""
    found: list[Found]
    limits: dict[str, int]                                            # 使った上限（max_depth・max_elements・max_pointers）
    depth_cut: list[str] = field(default_factory=list)                # 深さの上限で中を辿っていない式（中にオブジェクトがある型）
    elements_cut: list[tuple[str, int]] = field(default_factory=list)  # 要素の上限で途中までしか見ていない配列（式, 要素の数）
    pointers_unread: int = 0                                          # 読むハンドルの上限に達して読んでいないハンドルの数
    unreadable: list[str] = field(default_factory=list)               # 値が読めないハンドルの式
    unplaced: list[str] = field(default_factory=list)                 # 型はオブジェクトを含むが場所が分からない変数
    untyped: int = 0                                                  # 型が読めない変数の数

    def notes(self) -> list[str]:
        """見ていない所の事実の文（何も無ければ空）。"""
        what = "変数の型でオブジェクトを探す"
        out = []
        if self.depth_cut:
            out.append(f"{what}: 入れ子の深さの上限 {self.limits['max_depth']} より深い所は辿っていない（中にオブジェクト・"
                       f"ハンドルの型がある {len(self.depth_cut)} か所: {', '.join(self.depth_cut)}）")
        if self.elements_cut:
            arrays = ", ".join(f"{e}（{n} 要素）" for e, n in self.elements_cut)
            out.append(f"{what}: 配列は先頭 {self.limits['max_elements']} 要素までしか見ていない（{arrays}）")
        if self.pointers_unread:
            out.append(f"{what}: 読むハンドルの上限 {self.limits['max_pointers']} 個に達した。読んでいないハンドル "
                       f"{self.pointers_unread} 個")
        if self.unreadable:
            out.append(f"{what}: 値が読めないハンドル {len(self.unreadable)} 個（{', '.join(self.unreadable)}）")
        if self.unplaced:
            out.append(f"{what}: 場所が分からない変数 {len(self.unplaced)} 個（{', '.join(self.unplaced)}）")
        if self.untyped:
            out.append(f"{what}: 型が読めない変数 {self.untyped} 個（オブジェクトを含むかは分からない）")
        return out


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
                    exclude: frozenset[str] = frozenset()) -> TypedSearch:
    """大域・ファイルの static 変数のうち、型が structs の構造体（変数そのものがオブジェクト）か、それを指すポインタ
    （ハンドル。0 でないもの）。structs は 構造体か typedef の名前 → 分類。exclude の変数（カーネルの表など、名前を
    与えないもの）は辿らない。上限（max_depth・max_elements・max_pointers）で見ていない所と、読めないハンドル・
    場所の分からない変数は、結果の TypedSearch に残す（notes() で事実の文）。"""
    search = TypedSearch([], {"max_depth": max_depth, "max_elements": max_elements, "max_pointers": max_pointers})
    memo: dict[int, bool] = {}
    budget = max_pointers

    def may_contain(t: CType) -> bool:
        """この型の中に（深さを問わず）オブジェクトかハンドルがあるか。型の木は有限（値で自分を含む構造体は無い）。"""
        if _family(t, structs) is not None:
            return True
        s = strip(t)
        if isinstance(s, PointerType):
            return _family(s.target, structs) is not None
        key = id(s)
        if key not in memo:
            memo[key] = False                                   # 自分を含む型（あり得ないが）でも止まる
            if isinstance(s, StructType) and s.kind == "struct":
                memo[key] = any(m.bit_size is None and may_contain(m.type) for m in s.members)
            elif isinstance(s, ArrayType) and s.length:
                memo[key] = may_contain(s.element)
        return memo[key]

    def walk(t: CType, address: int, expression: str, depth: int) -> None:
        nonlocal budget
        family = _family(t, structs)
        if family is not None:
            search.found.append(Found(expression, address, family, False))
            return
        s = strip(t)
        if isinstance(s, PointerType):
            family = _family(s.target, structs)
            if family is None:
                return
            if budget <= 0:
                search.pointers_unread += 1
                return
            budget -= 1
            try:
                value = mem.u32(address)
            except AccessError:
                search.unreadable.append(expression)
                return
            if value:
                search.found.append(Found(expression, value, family, True))
            return
        if not may_contain(s):
            return
        if depth >= max_depth:
            search.depth_cut.append(expression)
            return
        if isinstance(s, StructType) and s.kind == "struct":
            for m in s.members:
                if m.bit_size is None:
                    walk(m.type, address + m.offset, f"{expression}.{m.name}" if m.name else expression, depth + 1)
        elif isinstance(s, ArrayType) and s.length:
            size = s.element.size or 0
            if s.length > max_elements:
                search.elements_cut.append((expression, s.length))
            for i in range(min(s.length, max_elements)):
                walk(s.element, address + i * size, f"{expression}[{i}]", depth + 1)

    for var in info.global_variables():
        if var.name in exclude:
            continue
        try:
            t = var.type
            if not may_contain(t):
                continue
        except (KeyError, ValueError, NotImplementedError):
            search.untyped += 1
            continue
        try:
            place = var.place()
        except (KeyError, ValueError, NotImplementedError):
            search.unplaced.append(var.name)
            continue
        if isinstance(place, MemoryPlace):
            walk(t, place.address, var.name, 0)
    return search
