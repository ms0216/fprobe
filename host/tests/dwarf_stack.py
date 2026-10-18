"""試験用: 偽のスタックを CFI とは独立に組み立てる（逆アセンブルで関数の前置きをなぞる）。

巻き戻し（CFI）の試験の正しさを CFI 自身に頼らないため、関数の先頭の命令（push / vpush / mov r7, sp /
add r7, sp, #n / sub sp, #n / str rX, [sp, #n]）を capstone で読み、レジスタとメモリに実際に積む。
呼び出しは bl の番地を逆アセンブルで探し、lr = 戻り先 | 1 にして呼ばれる側の前置きをなぞる。
例外は Cortex-M のハードと同じフレーム（8 語、FP の拡張なら 26 語、8 バイト揃えの詰め物）を積む。
"""

from __future__ import annotations

import capstone
from capstone import arm

from fprobe.dwarf.access import ImageAccess
from fprobe.symbols import Symbols

_MD = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB | capstone.CS_MODE_MCLASS)
_MD.detail = True
_CORE = [f"r{i}" for i in range(13)] + ["sp", "lr", "pc"]


class StackBuilder:
    def __init__(self, symbols: Symbols, top: int = 0x2000_8000):
        self.symbols = symbols
        self.memory = ImageAccess.from_symbols(symbols)
        # 初期値は見分けのつく値（0xDEAD00nn）。巻き戻しで取り違えれば分かる
        self.regs: dict[str, int] = {f"r{n}": 0xDEAD_0000 + n for n in range(13)}
        self.regs.update(lr=0xDEAD_000E, msp=top, psp=0)
        self.on_psp = False
        self.regs["sp"] = top
        self.calls: list[tuple[str, int, dict]] = []     # (関数, 前置きの後の番地, その時のレジスタ)

    # --- レジスタ ----------------------------------------------------------------------

    def set(self, **values: int) -> StackBuilder:
        for name, value in values.items():
            self.regs[name] = value & 0xFFFFFFFF
        return self

    def _sp(self, value: int | None = None) -> int:
        if value is not None:
            self.regs["sp"] = value
            self.regs["psp" if self.on_psp else "msp"] = value
        return self.regs["sp"]

    def use_psp(self, top: int) -> StackBuilder:
        """スレッドを PSP で動かす（RTOS のタスクと同じ）。"""
        self.regs["msp"] = self.regs["sp"]
        self.on_psp = True
        self._sp(top)
        return self

    # --- 命令をなぞる ----------------------------------------------------------------------

    def _code(self, address: int, length: int = 64):
        data = self.memory.read_memory(address, length)
        return list(_MD.disasm(data, address))

    def enter(self, function: str, *args: int, lr: int | None = None) -> int:
        """関数の先頭から前置きをなぞる。引数は r0.. に入れる。戻り値: 前置きの後の番地。"""
        start = self.symbols.symbol(function).address
        for i, value in enumerate(args):
            self.regs[f"r{i}"] = value & 0xFFFFFFFF
        if lr is not None:
            self.regs["lr"] = lr
        for insn in self._code(start):
            if not self._prologue(insn):
                self.regs["pc"] = insn.address
                self.calls.append((function, insn.address, dict(self.regs)))
                return insn.address
        raise AssertionError(f"{function} の前置きが終わらない")

    def _prologue(self, insn) -> bool:
        ops = insn.operands
        regs = [insn.reg_name(op.reg) for op in ops if op.type == arm.ARM_OP_REG]
        m = insn.mnemonic
        if m in ("push", "push.w", "stmdb") and (m != "stmdb" or regs[0] == "sp"):
            names = regs if m != "stmdb" else regs[1:]
            sp = self._sp(self._sp() - 4 * len(names))
            for i, name in enumerate(sorted(names, key=_number)):
                self.memory.write_u32(sp + 4 * i, self.regs[name])
            return True
        if m == "vpush":
            sp = self._sp(self._sp() - 8 * len(regs))
            for i, name in enumerate(sorted(regs, key=lambda r: int(r[1:]))):
                k = int(name[1:])
                self.memory.write_u32(sp + 8 * i, self.regs.get(f"s{2 * k}", 0))
                self.memory.write_u32(sp + 8 * i + 4, self.regs.get(f"s{2 * k + 1}", 0))
            return True
        if m == "mov" and regs == ["r7", "sp"]:
            self.regs["r7"] = self._sp()
            return True
        if m in ("add", "add.w", "adds") and regs[:2] == ["r7", "sp"] and ops[-1].type == arm.ARM_OP_IMM:
            self.regs["r7"] = self._sp() + ops[-1].imm
            return True
        if m in ("sub", "sub.w", "subs") and regs and regs[0] == "sp" and ops[-1].type == arm.ARM_OP_IMM:
            self._sp(self._sp() - ops[-1].imm)
            return True
        if m in ("str", "str.w") and ops[1].type == arm.ARM_OP_MEM and insn.reg_name(ops[1].mem.base) == "sp":
            self.memory.write_u32(self._sp() + ops[1].mem.disp, self.regs[regs[0]])
            return True
        return False

    def call_site(self, caller: str, callee: str) -> int:
        """caller の中で callee を呼ぶ bl / b.w（末尾呼び出し）の番地。"""
        sym = self.symbols.symbol(caller)
        target = self.symbols.symbol(callee).address
        for insn in self._code(sym.address, sym.size):
            if insn.mnemonic in ("bl", "blx", "b.w", "b") and insn.operands[0].type == arm.ARM_OP_IMM \
                    and insn.operands[0].imm == target:
                return insn.address
        raise AssertionError(f"{caller} の中に {callee} の呼び出しが無い")

    def call(self, caller: str, callee: str, *args: int) -> int:
        """caller の bl から callee を呼ぶ: lr = 戻り先 | 1。戻り値: callee の前置きの後の番地。"""
        site = self.call_site(caller, callee)
        size = self._code(site, 4)[0].size
        return self.enter(callee, *args, lr=(site + size) | 1)

    def interrupt(self, handler: str, pc: int, exc_return: int = 0xFFFFFFF9, xpsr: int = 0x0100_0000,
                  fp: list[int] | None = None, fpscr: int = 0) -> int:
        """pc で動いていた処理に例外が入る: ハードと同じく積み、lr = EXC_RETURN でハンドラへ（MSP で動く）。"""
        self.regs["pc"] = pc
        extended = not exc_return & 0x10
        size = 0x68 if extended else 0x20
        sp = self._sp()
        frame = (sp - size) & ~7                          # 積んだ後の SP を 8 バイトに揃える
        if sp - size != frame:
            xpsr |= 1 << 9
        words = [self.regs.get(r, 0) for r in ("r0", "r1", "r2", "r3", "r12", "lr")] + [pc | 0, xpsr]
        for i, w in enumerate(words):
            self.memory.write_u32(frame + 4 * i, w)
        if extended:
            for i, w in enumerate((fp or [0] * 16) + [fpscr, 0]):
                self.memory.write_u32(frame + 0x20 + 4 * i, w)
        self._sp(frame)
        if self.on_psp:
            self.on_psp = False                           # ハンドラは MSP
            self.regs["sp"] = self.regs["msp"]
        self.regs["xpsr"] = 15 if handler == "SysTick_Handler" else 3
        return self.enter(handler, lr=exc_return)

    def halt_at(self, pc: int, **values: int) -> ImageAccess:
        self.set(**values)
        self.regs["pc"] = pc
        self.memory.registers = dict(self.regs)
        return self.memory


def _number(name: str) -> int:
    return _CORE.index(name) if name in _CORE else 99
