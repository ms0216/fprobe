"""ターゲットの読み手の抽象と、DWARF のレジスタ番号（ARM の ABI）。

DWARF の評価・巻き戻しはターゲット（pyOCD 等）に依存せず、この `TargetAccess` だけを通してメモリとレジスタを読む。
`ImageAccess` は ELF の初期値（PT_LOAD）を疎なメモリとして持つ実装で、試験の偽物にも、ターゲット無しで
大域変数の初期値を見る用途にも使う。
"""

from collections.abc import Iterable, Mapping
from typing import Protocol, runtime_checkable

__all__ = ["AccessError", "ImageAccess", "TargetAccess", "WritableAccess", "register_name", "register_number"]


class AccessError(Exception):
    """メモリ・レジスタが読めない（番地の外・名前の無いレジスタ・通信の失敗）。"""


@runtime_checkable
class TargetAccess(Protocol):
    def read_memory(self, address: int, length: int) -> bytes: ...

    def read_register(self, name: str) -> int:
        """名前: r0〜r12, sp, lr, pc, xpsr, msp, psp, s0〜s31, d0〜d15（値は生のビット列の整数）。"""
        ...


@runtime_checkable
class WritableAccess(TargetAccess, Protocol):
    def write_memory(self, address: int, data: bytes) -> None: ...


_CORE = [f"r{i}" for i in range(13)] + ["sp", "lr", "pc"]


def register_name(number: int) -> str:
    """DWARF のレジスタ番号（ARM の DWARF ABI）→ 名前。0〜15: r0〜pc、64〜95: s0〜s31、256〜287: d0〜d31。"""
    if 0 <= number < 16:
        return _CORE[number]
    if 64 <= number < 96:
        return f"s{number - 64}"
    if 256 <= number < 288:
        return f"d{number - 256}"
    raise KeyError(f"DWARF のレジスタ番号 {number} は扱わない")


def register_number(name: str) -> int:
    name = name.lower()
    if name in _CORE:
        return _CORE.index(name)
    if name[0] in "rsd" and name[1:].isdigit():
        n = int(name[1:])
        if name[0] == "r" and n < 16:
            return n
        if name[0] == "s" and n < 32:
            return 64 + n
        if name[0] == "d" and n < 32:
            return 256 + n
    raise KeyError(f"レジスタ {name!r} に DWARF の番号は無い")


class ImageAccess:
    """疎なメモリ（区画の並び + 書き込み）と、決めたレジスタ値の読み手。

    d レジスタは s の 2 つ（d*n* = s*2n*（下位）:s*2n+1*（上位））から作る。
    """

    def __init__(self, segments: Iterable[tuple[int, bytes]] = (), registers: Mapping[str, int] | None = None):
        self._bytes: dict[int, int] = {}
        for address, data in segments:
            self.write_memory(address, data)
        self.registers: dict[str, int] = dict(registers or {})

    @classmethod
    def from_symbols(cls, symbols, registers: Mapping[str, int] | None = None) -> "ImageAccess":
        """fprobe.symbols.Symbols の読み込む区画から作る。"""
        return cls(((s.address, s.data) for s in symbols.segments), registers)

    def read_memory(self, address: int, length: int) -> bytes:
        try:
            return bytes(self._bytes[a] for a in range(address, address + length))
        except KeyError as e:
            raise AccessError(f"0x{e.args[0]:08X} は読めない") from None

    def write_memory(self, address: int, data: bytes) -> None:
        for i, b in enumerate(data):
            self._bytes[address + i] = b

    def write_u32(self, address: int, value: int) -> None:
        self.write_memory(address, (value & 0xFFFFFFFF).to_bytes(4, "little"))

    def read_register(self, name: str) -> int:
        name = name.lower()
        if name in self.registers:
            return self.registers[name]
        if name.startswith("d") and name[1:].isdigit():
            n = int(name[1:])
            return self.read_register(f"s{2 * n}") | self.read_register(f"s{2 * n + 1}") << 32
        if name == "r13":
            return self.read_register("sp")
        raise AccessError(f"レジスタ {name} は分からない")
