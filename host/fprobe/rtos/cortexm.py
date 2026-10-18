"""Cortex-M のタスクの保存された文脈: RTOS の port が積んだレジスタを読み、F2 の巻き戻しに渡す読み手にする。

    ctx = read_saved_context(access.read_memory, tcb_top_of_stack, "armv7m_fp")
    inspector = Inspector(info, ContextAccess(access, ctx.registers), symbols)

port の積み方（`pxTopOfStack` から上へ）:
- "armv7m_fp"（FreeRTOS の GCC/ARM_CM4F・ARM_CM7）: r4〜r11、EXC_RETURN、[EXC_RETURN の bit 4 = 0 なら s16〜s31]、
  ハードのフレーム
- "armv7m"（FreeRTOS の GCC/ARM_CM3・ARM_CM0 等）: r4〜r11、ハードのフレーム（EXC_RETURN は 0xFFFFFFFD）
- RTX5（irq_armv7m.S・irq_armv6m.S）は `read_fp_first_context`: [EXC_RETURN の bit 4 = 0 なら s16〜s31]、r4〜r11、
  ハードのフレーム（EXC_RETURN は積まず、TCB の stack_frame に下位 8 ビットを持つ）
- ThreadX（ports/cortex_m*/gnu/src/tx_thread_schedule.S）は `read_exc_return_first_context`: EXC_RETURN、続きは
  "armv7m"（M3・M4・M7・M33 等のメインライン）が [s16〜s31]、r4〜r11、"armv6m"（M0・M0+・M23 のベースライン。STM が
  上向きにしか積めない）が r8〜r11、r4〜r7。どちらもその上がハードのフレーム。ベースラインかは `arm_baseline`（ELF の属性）
ハードのフレーム: r0 r1 r2 r3 r12 lr pc xpsr [+ s0〜s15 FPSCR 予約]。xpsr の bit 9 なら 4 バイトの詰め物。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from ..dwarf.access import AccessError, TargetAccess

__all__ = ["EXC_RETURN_FIRST_LAYOUTS", "LAYOUTS", "ContextAccess", "SavedContext", "arm_baseline", "arm_has_fpu",
           "read_exc_return_first_context", "read_fp_first_context", "read_saved_context", "unstack_exception_frame"]

LAYOUTS = ("armv7m", "armv7m_fp")
EXC_RETURN_FIRST_LAYOUTS = ("armv7m", "armv6m")
BASELINE_ARCHS = {11, 12, 16}                 # Tag_CPU_arch: v6-M・v6S-M・v8-M.baseline
THREAD_PSP_EXC_RETURN = 0xFFFF_FFFD           # スレッドモード・PSP・FP の文脈なし
_HW = ("r0", "r1", "r2", "r3", "r12", "lr", "pc", "xpsr")
_CALLEE = tuple(f"r{n}" for n in range(4, 12))
XPSR_ALIGNED = 1 << 9

Read = Callable[[int, int], bytes]


def _words(read: Read, address: int, count: int) -> list[int]:
    data = read(address & 0xFFFF_FFFF, 4 * count)
    return [int.from_bytes(data[i:i + 4], "little") for i in range(0, 4 * count, 4)]


@dataclass(frozen=True)
class SavedContext:
    top: int                      # 文脈の先頭（FreeRTOS の pxTopOfStack）
    registers: dict[str, int]     # r0〜r12・sp・lr・pc・xpsr・psp（FP の文脈なら s0〜s31・fpscr も）
    exc_return: int
    fp: bool                      # FP の文脈（拡張フレーム）を持つ

    @property
    def sp(self) -> int:
        """タスクの SP（文脈を戻した後の値）。"""
        return self.registers["sp"]


def unstack_exception_frame(read: Read, address: int, exc_return: int) -> tuple[dict[str, int], int]:
    """例外の入口でハードが積んだフレーム → (レジスタ, フレームを外した後の SP)。"""
    extended = not exc_return & 0x10
    words = _words(read, address, 26 if extended else 8)
    regs = dict(zip(_HW, words[:8]))
    regs["pc"] &= ~1
    size = 0x20
    if extended:
        regs.update({f"s{n}": words[8 + n] for n in range(16)})
        regs["fpscr"] = words[24]
        size = 0x68
    if regs["xpsr"] & XPSR_ALIGNED:
        size += 4
    return regs, (address + size) & 0xFFFF_FFFF


def read_saved_context(read: Read, top: int, layout: str) -> SavedContext:
    """port の積み方 layout で、top から積まれた文脈を読む。"""
    if layout not in LAYOUTS:
        raise ValueError(f"port の積み方 {layout!r} は扱わない（{', '.join(LAYOUTS)}）")
    regs: dict[str, int] = {}
    if layout == "armv7m_fp":
        words = _words(read, top, 9)
        exc_return, at = words[8], top + 36
        if not exc_return & 0x10:
            regs.update({f"s{16 + n}": w for n, w in enumerate(_words(read, at, 16))})
            at += 64
    else:
        words = _words(read, top, 8)
        exc_return, at = THREAD_PSP_EXC_RETURN, top + 32
    regs.update(zip(_CALLEE, words[:8]))
    hw, sp = unstack_exception_frame(read, at, exc_return)
    regs.update(hw)
    regs["sp"] = regs["psp"] = sp
    return SavedContext(top, regs, exc_return, not exc_return & 0x10)


def read_fp_first_context(read: Read, top: int, exc_return: int) -> SavedContext:
    """FP の文脈が下に積まれる port（RTX5）: top から [s16〜s31（拡張フレーム）]、r4〜r11、ハードのフレーム。
    exc_return は port が別に持つ値（RTX5 は TCB の stack_frame | 0xFFFFFF00）。"""
    regs: dict[str, int] = {}
    at = top
    if not exc_return & 0x10:
        regs.update({f"s{16 + n}": w for n, w in enumerate(_words(read, at, 16))})
        at += 64
    regs.update(zip(_CALLEE, _words(read, at, 8)))
    hw, sp = unstack_exception_frame(read, at + 32, exc_return)
    regs.update(hw)
    regs["sp"] = regs["psp"] = sp
    return SavedContext(top, regs, exc_return, not exc_return & 0x10)


def read_exc_return_first_context(read: Read, top: int, layout: str) -> SavedContext:
    """EXC_RETURN が一番下に積まれる port（ThreadX）: top から EXC_RETURN、"armv7m" なら [s16〜s31（拡張フレーム）]、
    r4〜r11、"armv6m" なら r8〜r11、r4〜r7。その上がハードのフレーム。"""
    if layout not in EXC_RETURN_FIRST_LAYOUTS:
        raise ValueError(f"port の積み方 {layout!r} は扱わない（{', '.join(EXC_RETURN_FIRST_LAYOUTS)}）")
    exc_return = _words(read, top, 1)[0]
    if layout == "armv7m":
        context = read_fp_first_context(read, top + 4, exc_return)
        return SavedContext(top, context.registers, exc_return, context.fp)
    words = _words(read, top + 4, 8)
    regs = dict(zip(("r8", "r9", "r10", "r11", "r4", "r5", "r6", "r7"), words))
    hw, sp = unstack_exception_frame(read, top + 36, exc_return)
    regs.update(hw)
    regs["sp"] = regs["psp"] = sp
    return SavedContext(top, regs, exc_return, not exc_return & 0x10)


def _arm_attribute(elf, tag: str) -> int | None:
    """ELF の .ARM.attributes の属性の値（無ければ・読めなければ None）。"""
    section = elf.get_section_by_name(".ARM.attributes")
    if section is None:
        return None
    try:
        for sub in section.iter_subsections():
            for subsub in sub.iter_subsubsections():
                for attr in subsub.iter_attributes():
                    if attr.tag == tag:
                        return attr.value
    except Exception:                                          # 読めない属性
        return None
    return None


def arm_baseline(elf) -> bool | None:
    """ELF の属性 Tag_CPU_arch がベースライン（v6-M・v8-M.baseline）か。属性が無ければ None。"""
    arch = _arm_attribute(elf, "TAG_CPU_ARCH")
    return None if arch is None else arch in BASELINE_ARCHS


def arm_has_fpu(elf) -> bool:
    """ELF の属性 Tag_FP_arch（浮動小数点の命令を使う）があるか。"""
    return bool(_arm_attribute(elf, "TAG_FP_ARCH"))


class ContextAccess:
    """メモリはターゲットから、レジスタは決めた値（保存された文脈）から読む `TargetAccess`。

    無いレジスタ（FP の文脈の無いタスクの s0〜s31、msp 等）は AccessError（嘘の値を出さない）。
    """

    def __init__(self, memory: TargetAccess, registers: Mapping[str, int]):
        self.memory = memory
        self.registers = {k.lower(): v for k, v in registers.items()}

    def read_memory(self, address: int, length: int) -> bytes:
        return self.memory.read_memory(address, length)

    def read_register(self, name: str) -> int:
        name = name.lower()
        if name == "r13":
            name = "sp"
        if name in self.registers:
            return self.registers[name]
        if name.startswith("d") and name[1:].isdigit():
            n = int(name[1:])
            return self.read_register(f"s{2 * n}") | self.read_register(f"s{2 * n + 1}") << 32
        raise AccessError(f"レジスタ {name} はタスクの保存された文脈に無い")
