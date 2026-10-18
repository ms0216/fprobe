"""Flash に書いた BKPT の下の元の命令を、Flash を書き換えずに 1 つ実行する（Flash ブレークポイントの踏み越え）。

Flash のソフトのブレークポイント（fprobe.flash_bp）で止まったところから再開・ステップするとき、素直にやると
「元の命令に書き戻す → 1 命令進める → BKPT を書き直す」で Flash を 2 回書き換える。J-Link の Unlimited Flash
Breakpoints が命令セットシミュレータで避けているところを、ここでは次の 3 つに分けて行う:

- emulate: PC を読む命令（分岐 B・B<c>・BL・CBZ・CBNZ・BLX rm・TBB・TBH、PC 相対の読み LDR・LDRB・LDRH・LDRSB・
  LDRSH、ADR・ADDW/SUBW rd, pc, #imm）は、デバッガがレジスタとメモリを読み書きして結果を作る（コアは動かさない）。
- displace: PC を読まない命令は、借りた RAM（scratch）に写して 1 命令だけ実行し、PC を Flash の次の番地へ戻す
  （GDB の displaced stepping と同じ）。PC を書く命令（POP {pc}・BX LR 等）は行き先がそのまま正しい。命令が例外に
  入ったとき（SVC・故障）は、積まれた戻り先を scratch の番地から元の Flash の番地に直す（元の場所で起きたのと同じ）。
- rewrite: どちらでもできないもの（BKPT・UDF、模擬しない PC の読み、IT ブロックの中の PC を読む命令、scratch と重なる
  メモリの読み書き、MPU が有効、解読できない）は NeedsRewrite を投げ、呼び手が Flash を書き戻して進める。

displace は割り込みを止めて 1 命令進める（DHCSR.C_MASKINTS。入った割り込みの戻り先が scratch にならないように）。
ponytail: scratch の 8 バイトは止めている間だけ借りて戻す。DMA がその瞬間にそこを使う場合と、PC の値で一致する DWT の
比較器（scratch では一致しない）は扱わない。
"""

from dataclasses import dataclass

import capstone
from capstone import arm

__all__ = ["NeedsRewrite", "OutOfLine", "Plan", "invalidate_icache", "plan"]

DFSR, CFSR, MPU_CTRL = 0xE000ED30, 0xE000ED28, 0xE000ED94
DFSR_ALL = 0x1F
SCRATCH_SIZE = 8
NOP = b"\x00\xbf"
REGISTERS = [f"r{i}" for i in range(13)] + ["sp", "lr", "pc", "xpsr", "msp", "psp"]

_md = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB | capstone.CS_MODE_MCLASS)
_md.detail = True

_BRANCHES = {arm.ARM_INS_B, arm.ARM_INS_BL, arm.ARM_INS_CBZ, arm.ARM_INS_CBNZ, arm.ARM_INS_BLX,
             arm.ARM_INS_TBB, arm.ARM_INS_TBH}
_LITERAL_LOADS = {"ldr": (4, False), "ldrb": (1, False), "ldrh": (2, False), "ldrsb": (1, True), "ldrsh": (2, True)}
_NEVER = {arm.ARM_INS_BKPT, arm.ARM_INS_UDF}
_REG_LIST = ("push", "pop", "ldm", "stm", "vpush", "vpop", "vldm", "vstm")
ACCESS_WIDTH = 8                   # 1 回の読み書きの最大（LDRD / STRD）。範囲は控えめに広く取る


class NeedsRewrite(Exception):
    """Flash を書き戻さないと、この命令は正しく実行できない。"""


@dataclass(frozen=True)
class Plan:
    how: str                      # emulate / displace / rewrite
    reason: str = ""
    insn: object = None           # capstone の命令


def _itstate(xpsr: int) -> int:
    return ((xpsr >> 25) & 0x3) | ((xpsr >> 10) & 0x3F) << 2


def _mnemonic(insn) -> str:
    return insn.mnemonic.split(".")[0]


def _reads_pc(insn) -> bool:
    read, _ = insn.regs_access()
    if arm.ARM_REG_PC in read:
        return True
    return any(op.type == arm.ARM_OP_MEM and op.mem.base == arm.ARM_REG_PC for op in insn.operands)


def _literal(insn) -> bool:
    ops = insn.operands
    return (_mnemonic(insn) in _LITERAL_LOADS and len(ops) == 2 and ops[1].type == arm.ARM_OP_MEM
            and ops[1].mem.base == arm.ARM_REG_PC and ops[1].mem.index == 0)


def _pc_arithmetic(insn) -> bool:
    ops = insn.operands
    return (_mnemonic(insn) in ("addw", "subw", "add", "sub") and len(ops) == 3 and ops[1].type == arm.ARM_OP_REG
            and ops[1].reg == arm.ARM_REG_PC and ops[2].type == arm.ARM_OP_IMM and ops[0].reg != arm.ARM_REG_PC)


def plan(code: bytes, address: int, xpsr: int) -> Plan:
    """code（その番地の元のバイト列。先頭の 1 命令を見る）をどう実行するか。"""
    insn = next(_md.disasm(bytes(code), address, 1), None)
    if insn is None:
        return Plan("rewrite", "解読できない")
    if insn.id in _NEVER:
        return Plan("rewrite", f"{insn.mnemonic} は元から止まる命令", insn)
    emulated = (insn.id in _BRANCHES or _literal(insn) or insn.id == arm.ARM_INS_ADR or _pc_arithmetic(insn))
    if insn.id == arm.ARM_INS_BLX and insn.operands[0].type == arm.ARM_OP_REG \
            and insn.operands[0].reg == arm.ARM_REG_PC:
        emulated = False
    if insn.id == arm.ARM_INS_SVC:
        return Plan("displace", "", insn)                    # 例外の入口で積まれた戻り先を直す
    if emulated:
        if _itstate(xpsr):
            return Plan("rewrite", "IT ブロックの中の PC を使う命令", insn)
        return Plan("emulate", "", insn)
    if _reads_pc(insn):
        return Plan("rewrite", f"{insn.mnemonic} {insn.op_str} は PC を読む", insn)
    return Plan("displace", "", insn)


def _passed(cc: int, xpsr: int) -> bool:
    n, z, c, v = (bool(xpsr >> b & 1) for b in (31, 30, 29, 28))
    table = {arm.ARM_CC_EQ: z, arm.ARM_CC_NE: not z, arm.ARM_CC_HS: c, arm.ARM_CC_LO: not c, arm.ARM_CC_MI: n,
             arm.ARM_CC_PL: not n, arm.ARM_CC_VS: v, arm.ARM_CC_VC: not v, arm.ARM_CC_HI: c and not z,
             arm.ARM_CC_LS: not c or z, arm.ARM_CC_GE: n == v, arm.ARM_CC_LT: n != v,
             arm.ARM_CC_GT: not z and n == v, arm.ARM_CC_LE: z or n != v}
    return table.get(cc, True)


class OutOfLine:
    """target: pyOCD のターゲット（止まっている）。scratch: 借りる RAM の番地（8 バイト、止めている間だけ使って戻す）。
    read(address, length) -> bytes: 模擬で読むメモリ（Flash の BKPT を元の中身に見せる読み手。省略時はターゲット）。"""

    def __init__(self, target, scratch: int, read=None):
        self.target = target
        self.scratch = scratch
        self.read = read if read is not None else (lambda a, n: bytes(target.read_memory_block8(a, n)))

    def step(self, address: int, code: bytes) -> str:
        """address（PC）の元の命令 code を 1 つ実行する。"emulated" / "displaced"。できなければ NeedsRewrite。"""
        xpsr = self.target.read_core_register_raw("xpsr")
        p = plan(code, address, xpsr)
        if p.how == "rewrite":
            raise NeedsRewrite(p.reason)
        if p.how == "emulate":
            self._emulate(p.insn, address, xpsr)
            return "emulated"
        self._displace(p.insn, address)
        return "displaced"

    # --- 模擬 ---

    def _reg(self, number: int) -> int:
        return self.target.read_core_register_raw(_md.reg_name(number))

    def _emulate(self, insn, address: int, xpsr: int) -> None:
        t = self.target
        size, ops, name = insn.size, insn.operands, _mnemonic(insn)
        pc_value = address + 4                               # 命令が読む PC
        writes: dict[str, int] = {}
        if insn.id == arm.ARM_INS_B:
            writes["pc"] = ops[0].imm if _passed(insn.cc, xpsr) else address + size
        elif insn.id == arm.ARM_INS_BL:
            writes.update(lr=(address + size) | 1, pc=ops[0].imm)
        elif insn.id in (arm.ARM_INS_CBZ, arm.ARM_INS_CBNZ):
            zero = self._reg(ops[0].reg) == 0
            writes["pc"] = ops[1].imm if zero == (insn.id == arm.ARM_INS_CBZ) else address + size
        elif insn.id == arm.ARM_INS_BLX:
            writes.update(lr=(address + size) | 1, pc=self._reg(ops[0].reg) & ~1)
        elif insn.id in (arm.ARM_INS_TBB, arm.ARM_INS_TBH):
            index = self._reg(ops[0].mem.index)
            if insn.id == arm.ARM_INS_TBB:
                entry = self.read(pc_value + index, 1)[0]
            else:
                entry = int.from_bytes(self.read(pc_value + 2 * index, 2), "little")
            writes["pc"] = pc_value + 2 * entry
        elif _literal(insn):
            length, signed = _LITERAL_LOADS[name]
            disp = -ops[1].mem.disp if ops[1].subtracted else ops[1].mem.disp
            value = int.from_bytes(self.read((pc_value & ~3) + disp, length), "little", signed=signed) & 0xFFFFFFFF
            if ops[0].reg == arm.ARM_REG_PC:
                writes["pc"] = value & ~1
            else:
                writes.update({_md.reg_name(ops[0].reg): value, "pc": address + size})
        elif insn.id == arm.ARM_INS_ADR:
            writes.update({_md.reg_name(ops[0].reg): (pc_value & ~3) + ops[1].imm, "pc": address + size})
        else:                                                # addw / subw rd, pc, #imm
            sign = -1 if name.startswith("sub") else 1
            writes.update({_md.reg_name(ops[0].reg): ((pc_value & ~3) + sign * ops[2].imm) & 0xFFFFFFFF,
                           "pc": address + size})
        names = list(writes)
        t.write_core_registers_raw(names, [writes[n] for n in names])
        t.write32(DFSR, DFSR_ALL)                            # 1 命令進めたのと同じく、止まった理由を消す
        t.flush()

    # --- RAM での実行 ---

    def _displace(self, insn, address: int) -> None:
        t = self.target
        if t.read32(MPU_CTRL) & 1:
            raise NeedsRewrite("MPU が有効（借りた RAM で命令を実行できるか分からない）")
        regs = dict(zip(REGISTERS, t.read_core_registers_raw(REGISTERS)))
        span = self._access_span(insn, regs)
        if span is None:
            raise NeedsRewrite(f"{insn.mnemonic} {insn.op_str} の読み書きの範囲が分からない")
        if span and span[0] < self.scratch + SCRATCH_SIZE and self.scratch < span[1]:
            raise NeedsRewrite(f"{insn.mnemonic} {insn.op_str} が借りる RAM（0x{self.scratch:08X}）に触る")
        size = insn.size
        saved = bytes(t.read_memory_block8(self.scratch, SCRATCH_SIZE))
        code = bytes(insn.bytes) + NOP * ((SCRATCH_SIZE - size) // 2)
        try:
            t.write_memory_block8(self.scratch, list(code))
            t.flush()
            invalidate_icache(t)
            t.write_core_register_raw("pc", self.scratch)
            t.step(disable_interrupts=True)
            after = dict(zip(("pc", "lr", "xpsr", "msp", "psp"),
                             t.read_core_registers_raw(["pc", "lr", "xpsr", "msp", "psp"])))
            if after["pc"] == self.scratch + size:
                t.write_core_register_raw("pc", address + size)
            elif (after["xpsr"] & 0x1FF) != (regs["xpsr"] & 0x1FF) and after["lr"] >> 24 == 0xFF:
                frame = after["psp"] if after["lr"] & 0x4 else after["msp"]
                stacked = t.read32(frame + 24)
                if self.scratch <= stacked <= self.scratch + size:
                    t.write32(frame + 24, address + stacked - self.scratch)
        finally:
            t.write_memory_block8(self.scratch, list(saved))
            t.flush()
            invalidate_icache(t)

    @staticmethod
    def _access_span(insn, regs: dict[str, int]) -> tuple[int, int] | tuple | None:
        """命令が読み書きするメモリの範囲 [a, b)（控えめに広く）。触らなければ ()、分からなければ None。"""
        name = _mnemonic(insn)
        ops = insn.operands

        def value(reg: int) -> int:
            return regs[_md.reg_name(reg)]

        if name.startswith(_REG_LIST):
            base = regs["sp"] if name in ("push", "pop", "vpush", "vpop") else value(ops[0].reg)
            reach = 8 * len(ops)
            return (base - reach, base + reach)
        mems = [op for op in ops if op.type == arm.ARM_OP_MEM]
        if not mems:
            return ()
        mem = mems[0].mem
        if mem.base == arm.ARM_REG_PC:
            return None
        base = value(mem.base)
        offset = value(mem.index) << max(mem.lshift, 0) if mem.index else mem.disp
        if mems[0].subtracted:
            offset = -offset
        low, high = min(base, base + offset), max(base, base + offset)
        return (low, high + ACCESS_WIDTH)


def invalidate_icache(target) -> None:
    """命令キャッシュを捨てる（Cortex-M7 等。無いコアでは何もしない）。pyOCD の SoC のターゲットはこれをコアへ渡さない
    ので、選んでいるコアに直接頼む。"""
    core = getattr(target, "selected_core", None) or target
    invalidate = getattr(core, "invalidate_instruction_cache", None)
    if invalidate is not None:
        invalidate()
