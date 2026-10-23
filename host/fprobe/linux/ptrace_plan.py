"""ptrace のソフトのブレークポイントの計画。

エージェントは、当たったら元の命令に戻し、番地 + 命令の長さに仮のブレークポイントを置いて 1 歩進め、置き直す。
これは最初の命令が PC を書かない（分岐しない）ときだけ成り立つので、そうでなければ付ける前に断る。
Thumb の IT も断る（後ろの命令が IT の中になり、そこに置く BKPT の振る舞いが決まらない）。"""
from __future__ import annotations

from pathlib import Path

import capstone
from capstone import arm as cs_arm

from .uprobe import Site

_REFUSED_GROUPS = (capstone.CS_GRP_JUMP, capstone.CS_GRP_CALL, capstone.CS_GRP_RET, capstone.CS_GRP_BRANCH_RELATIVE)


class BreakpointRefused(ValueError):
    """この場所には ptrace のブレークポイントを付けない（理由は文に書く）"""


def plan_from_bytes(code: bytes, site: Site) -> dict:
    if site.machine != "EM_ARM":
        raise BreakpointRefused(f"ptrace のブレークポイントは段階 1 では 32 ビットの ARM だけ（この ELF は {site.machine}）")
    md = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB if site.thumb else capstone.CS_MODE_ARM)
    md.detail = True
    insn = next(md.disasm(code, site.vaddr, 1), None)
    if insn is None:
        raise BreakpointRefused(f"0x{site.vaddr:x} の最初の命令が読めない")
    text = f"{insn.mnemonic} {insn.op_str}".strip()
    if cs_arm.ARM_REG_PC in insn.regs_access()[1] or any(insn.group(g) for g in _REFUSED_GROUPS):
        raise BreakpointRefused(f"最初の命令 {text} が PC を書く（分岐する）ので、1 歩進めて置き直す方式が成り立たない")
    if site.thumb and insn.mnemonic.startswith("it"):
        raise BreakpointRefused(f"最初の命令 {text} は Thumb の IT（後ろの命令に置く BKPT の振る舞いが決まらない）")
    return {"path": site.path, "offset": site.offset, "thumb": site.thumb, "length": insn.size, "insn": text}


def plan_breakpoint(elf: Path, site: Site) -> dict:
    with open(elf, "rb") as f:
        f.seek(site.offset)
        return plan_from_bytes(f.read(4), site)
