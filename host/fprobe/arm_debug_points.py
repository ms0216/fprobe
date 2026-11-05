"""ArmDebugCore のブレークポイント・ウォッチポイント（ハードウェアの組だけ）の部品（U32）。

ArmDebugCore（fprobe.arm_debug）に mixin される。
"""

from .arm_debug_defs import (
    BP_ANY_PRIVILEGE, BP_ENABLE, WP_KINDS
)


def _byte_select(address: int, thumb: bool) -> int:
    """BCR の BAS: ARM の命令は 4 バイト（0b1111）、Thumb は番地の bit 1 で下か上の半語。"""
    if address & 2:
        return 0xC << 5
    return (0x3 if thumb else 0xF) << 5


class _PointsMixin:
    """ブレークポイント・ウォッチポイントの設定。"""

    def set_breakpoint(self, index: int, address: int, thumb: bool | None = None) -> None:
        """index（0〜breakpoint_count - 2）の組を、番地の一致に置く。thumb は命令の種類（None なら止まったときの
        CPSR.T）: ARM は 4 バイト、Thumb は半語を見る。"""
        if not 0 <= index < self.breakpoint_count - 1:
            raise ValueError(f"ブレークポイントの組は 0〜{self.breakpoint_count - 2}（最後の組はステップ用）")
        thumb = self.thumb if thumb is None else thumb
        self.port.write_breakpoint(index, address & ~3, _byte_select(address, thumb) | BP_ANY_PRIVILEGE | BP_ENABLE)

    def clear_breakpoint(self, index: int) -> None:
        self.port.write_breakpoint(index, 0, 0)

    def set_watchpoint(self, index: int, address: int, size: int, kind: str) -> bool:
        """index の組を、番地の size バイト（1 / 2 / 4、1 語の中）の kind（read / write / access）に置く。"""
        if size not in (1, 2, 4) or (address & 3) + size > 4 or address % size:
            return False
        if not 0 <= index < self.watchpoint_count:
            raise ValueError(f"ウォッチポイントの組は 0〜{self.watchpoint_count - 1}")
        byte_select = ((1 << size) - 1) << (address & 3)
        self.port.write_watchpoint(index, address & ~3,
                                   byte_select << 5 | WP_KINDS[kind] | BP_ANY_PRIVILEGE | BP_ENABLE)
        return True

    def clear_watchpoint(self, index: int) -> None:
        self.port.write_watchpoint(index, 0, 0)
