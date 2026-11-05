"""`ArmACores` の部品: レジスタ・メモリ・ハードウェアのブレークポイント/ウォッチポイント。

コアへの 1 回ごとの読み書きの薄い層。物理アドレスはカーネルの線形マップの仮想アドレスに直し、コア 0 を通して特権モードで読み書きする。
"""
from __future__ import annotations

import struct
from typing import Iterable

from fprobe.arm_debug import REGISTER_NAMES, ArmDebugError

from .core_access import OutsideLinearMap

MODE_SVC = 0x13
_WATCH_KIND = {"w": "write", "r": "read", "rw": "access"}


class ArmARegMemMixin:
    """レジスタ・メモリ・HW ブレークポイント（`ArmACores` の状態を使う）。"""

    def regs(self, core: int) -> list[int]:
        return self._cores[core].read_registers(REGISTER_NAMES)            # 1 回の往復（r0・r1・PC・CPSR は覚えた値）

    def set_regs(self, core: int, vals: list[int]) -> None:
        self._cores[core].write_registers(list(zip(REGISTER_NAMES, vals)))  # 1 回の往復

    def read_reg(self, core: int, name: str) -> int:
        if name == "sp_svc":
            return self._cores[core].read_banked_sp(MODE_SVC)
        return self._cores[core].read_register(name)

    def write_reg(self, core: int, name: str, value: int) -> None:
        self._cores[core].write_register(name, value)

    def read_cp15(self, core: int, name: str) -> int:
        return self._cores[core].read_cp15(name)

    # ----------------------------------------------------------------- memory
    def _va(self, pa: int, length: int) -> int:
        va = pa - self.phys_offset + self.page_offset
        if va < self.page_offset or va + length > self.linear_end:
            raise OutsideLinearMap("physical 0x%x (+%d) is outside the kernel linear map (virtual 0x%08x..0x%08x)"
                                   % (pa, length, self.page_offset, self.linear_end))
        return va

    def read_kernel(self, va: int, length: int) -> bytes:
        """カーネルの仮想アドレスを、コア 0 の MMU を通して読む（USR で止まっていても特権モードで）。vmalloc も読める。"""
        a0 = va & ~3
        words = (va + length + 3 - a0) // 4
        c = self._cores[0]
        with c.privileged():
            vals = c.read_memory_fast(a0, words)
        if len(vals) != words:
            raise ArmDebugError("short read at 0x%x (+%d)" % (va, length))
        return b"".join(struct.pack("<I", v) for v in vals)[va - a0:va - a0 + length]

    def write_kernel(self, va: int, data: bytes) -> None:
        """書く。D キャッシュの PoU へのクリーンと I キャッシュ・分岐予測の無効化は `write_memory` が行う（MP 拡張があれば内側の
        共有域の全コアに）。"""
        c = self._cores[0]
        with c.privileged():
            if va % 4 == 0 and len(data) % 4 == 0:
                c.write_memory(va, list(struct.unpack("<%dI" % (len(data) // 4), data)), 4)
            else:
                c.write_memory(va, list(data), 1)

    def read_phys(self, addr: int, length: int) -> bytes:
        return self.read_kernel(self._va(addr, length), length)

    def write_phys(self, addr: int, data: bytes) -> None:
        self.write_kernel(self._va(addr, len(data)), data)

    def fetch(self, core: int, va: int, width: int) -> int:
        return self._cores[core].read_memory(va, 1, width)[0]

    def sync_caches(self, cores: Iterable[int] | None = None) -> None:
        for n in (range(self.n_cores) if cores is None else cores):
            self._cores[n].invalidate_instruction_caches()

    # ------------------------------------------------- hardware bp / watchpoints
    def set_hw_bp(self, core: int, slot: int, addr: int, width: int) -> None:
        self._cores[core].set_breakpoint(slot, addr, thumb=(width == 2))
        self._hw_bps[(core, slot)] = (addr, width)

    def clear_hw_bp(self, core: int, slot: int) -> None:
        self._cores[core].clear_breakpoint(slot)
        self._hw_bps.pop((core, slot), None)

    def set_hw_watch(self, core: int, slot: int, addr: int, length: int, kind: str) -> None:
        if not self._cores[core].set_watchpoint(slot, addr, length, _WATCH_KIND[kind]):
            raise RuntimeError("watchpoint of %d bytes at 0x%x is not possible: the hardware watches 1, 2 or 4 bytes "
                               "inside one word (size and alignment)" % (length, addr))

    def clear_hw_watch(self, core: int, slot: int) -> None:
        self._cores[core].clear_watchpoint(slot)
