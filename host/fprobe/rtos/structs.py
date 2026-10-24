"""RTOS の構造体を DWARF の型で読む: メンバーの位置（設定で変わる形を決め打ちしない）と、ターゲットのメモリの読み。

    tcb = StructLayout.of(info, "TCB_t", "struct tskTaskControlBlock")
    tcb.offset("xStateListItem.pxContainer")     # 入れ子のメンバーの、構造体の先頭からのバイト位置（配列は要素 0）
    tcb.has("pxEndOfStack")                      # 設定で外れたメンバーは False
    mem = Memory(access.read_memory)
    mem.u32(handle + tcb.offset("uxPriority"))
"""

from __future__ import annotations

from collections.abc import Callable

from ..dwarf.info import DebugInfo
from ..dwarf.typesys import ArrayType, CType, StructType, strip, type_name

__all__ = ["Memory", "StructLayout"]


class StructLayout:
    def __init__(self, name: str, t: CType):
        s = strip(t)
        if not isinstance(s, StructType):
            raise TypeError(f"{name} は構造体ではない（{type_name(t)}）")
        self.name = name
        self.type = s
        self.size = s.size or 0
        self._cache: dict[str, tuple[int, CType] | None] = {}

    @classmethod
    def of(cls, info: DebugInfo, *names: str) -> StructLayout:
        """型の名前の候補のうち、DWARF にある最初のもの。"""
        for name in names:
            try:
                return cls(name, info.type_named(name))
            except KeyError:
                continue
        raise KeyError(f"型 {' / '.join(names)} が {info.path.name} の DWARF に無い")

    def _find(self, path: str) -> tuple[int, CType] | None:
        if path not in self._cache:
            offset, t = 0, self.type
            found: tuple[int, CType] | None = None
            for i, part in enumerate(path.split(".")):
                s = strip(t)
                while isinstance(s, ArrayType):          # 配列のメンバーの中は要素 0（"cpus.current" = cpus[0].current）
                    s = strip(s.element)
                if not isinstance(s, StructType):
                    break
                try:
                    member, extra = s.member(part)
                except KeyError:
                    break
                offset += member.offset + extra
                t = member.type
                if i == path.count("."):
                    found = (offset, t)
            self._cache[path] = found
        return self._cache[path]

    def has(self, path: str) -> bool:
        return self._find(path) is not None

    def offset(self, path: str) -> int:
        found = self._find(path)
        if found is None:
            raise KeyError(f"{self.name} にメンバー {path!r} は無い（設定で外れている）")
        return found[0]

    def member_type(self, path: str) -> CType:
        found = self._find(path)
        if found is None:
            raise KeyError(f"{self.name} にメンバー {path!r} は無い（設定で外れている）")
        return found[1]

    def member_size(self, path: str) -> int:
        return self.member_type(path).size or 0

    def length(self, path: str) -> int | None:
        """配列のメンバーの要素の数。"""
        t = strip(self.member_type(path))
        return t.length if isinstance(t, ArrayType) else None


class Memory:
    """ターゲットのメモリの小さな読み（リトルエンディアン）。"""

    def __init__(self, read_memory: Callable[[int, int], bytes],
                 on_truncated: Callable[[int, int], None] | None = None):
        self.read = read_memory
        self.on_truncated = on_truncated          # cstring が上限で切ったとき (番地, 使った上限) で呼ぶ

    def u8(self, address: int) -> int:
        return self.read(address, 1)[0]

    def u32(self, address: int) -> int:
        return int.from_bytes(self.read(address, 4), "little")

    def uint(self, address: int, size: int, signed: bool = False) -> int:
        return int.from_bytes(self.read(address, size), "little", signed=signed)

    def cstring(self, address: int, limit: int, whole: bool = False) -> str:
        """NUL までの文字列（limit バイトまで）。whole = limit がその文字の配列の大きさそのもの（切ったのではない）。
        そうでなく limit までに NUL が無ければ on_truncated(番地, limit) で知らせる（黙って切らない）。"""
        data = self.read(address, limit)
        if b"\0" not in data and not whole and self.on_truncated is not None:
            self.on_truncated(address, limit)
        return data.split(b"\0", 1)[0].decode("utf-8", errors="replace")
