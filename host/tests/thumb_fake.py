"""試験用: Flash を持ち、本物の Thumb の命令のバイト列を実行する偽の Cortex-M（Flash ブレークポイントの試験）。

flash_fakes.FakeChip（pyOCD の本物のメモリマップと FlashBuilder、NOR の規則の偽の書き込みアルゴリズム）に、
命令の実行・FPB（ハードのブレークポイント、数に限りがある）・DFSR・例外の入口と戻りを足したもの。Flash に BKPT を
書けば実行で本当に止まり、RAM に置いた命令も実行できる（Flash の外での 1 命令の実行の試験）。

実行できる命令（16 ビットは T1 の形）:
    NOP (BF00) / BKPT (BExx) / SVC (DFxx) / MOVS rd,#i8 / ADDS rd,#i8 / B (T2) / B<c> (T1) / BX rm / BLX rm /
    LDR rt,[pc,#i8*4] / ADR rd,#i8*4 / CBZ / CBNZ / PUSH {lr} / POP {pc} / STR rt,[rn,#i5*4] / LDR rt,[rn,#i5*4] /
    BL (32 ビット) / TBB [pc, rm] (32 ビット)
"""

from types import SimpleNamespace

from flash_fakes import FLASH, RAM, FakeChip
from pyocd.core.target import Target

DFSR, CFSR, HFSR, MPU_CTRL = 0xE000ED30, 0xE000ED28, 0xE000ED2C, 0xE000ED94
HALTED, BKPT, DWTTRAP, VCATCH, EXTERNAL = 1, 2, 4, 8, 16
EXC_RETURN_THREAD_MSP = 0xFFFFFFF9
XPSR_T = 1 << 24
N, Z = 1 << 31, 1 << 30
RUN_LIMIT = 10_000                       # resume 1 回で実行する命令の既定の上限（越えたら走ったまま）


def _sign(value: int, bits: int) -> int:
    return value - (1 << bits) if value & (1 << (bits - 1)) else value


class ThumbChip(FakeChip):
    def __init__(self, hw_breakpoints: int = 2, handlers: dict[int, int] | None = None, **kw):
        super().__init__(**kw)
        self.regs.update(pc=FLASH + 0x100, lr=0xFFFFFFFF, xpsr=XPSR_T, sp=RAM + 0x3F00)
        self.regs.pop("msp", None)                           # msp は sp の別名（スレッドモードで MSP を使う）
        self.hw: set[int] = set()
        self.sw: dict[int, bytes] = {}                       # RAM のソフトのブレークポイント（元の 2 バイト）
        self.hw_limit = hw_breakpoints
        self.dfsr = HALTED
        self.periph: dict[int, int] = {}
        self.handlers = dict(handlers or {})
        self.steps: list[int] = []                           # 1 命令の実行を始めた番地
        self.step_masks: list[bool] = []
        self.resumes = 0
        self.run_limit = RUN_LIMIT
        self.executed: list[int] = []                        # 実行した命令の番地（走らせた間も）
        self.icache_invalidations = 0
        self.core_registers = SimpleNamespace(by_name={n: None for n in [*self.regs, "msp"]})

    # --- 命令の置き場 ---

    def load(self, address: int, code: bytes) -> None:
        """Flash・RAM に命令を直接置く（試験の準備。Flash の書き込みアルゴリズムは通さない）。"""
        store, offset = self._store(address, len(code))
        store[offset:offset + len(code)] = code

    def read16(self, address: int) -> int:
        store, offset = self._store(address, 2)
        return int.from_bytes(store[offset:offset + 2], "little")

    # --- レジスタ ---

    @staticmethod
    def _alias(name: str) -> str:
        return "sp" if name == "msp" else name

    def read_core_register(self, name):
        return self.regs[self._alias(name)]

    read_core_register_raw = read_core_register

    def read_core_registers_raw(self, names):
        return [self.regs[self._alias(n)] for n in names]

    def write_core_register(self, name, value):
        self.regs[self._alias(name)] = value & 0xFFFFFFFF

    write_core_register_raw = write_core_register

    def write_core_registers_raw(self, names, values):
        for n, v in zip(names, values):
            self.write_core_register(n, v)

    # --- メモリ（周辺は辞書。DFSR・CFSR・HFSR は 1 を書くと消える） ---

    def read32(self, address):
        if address == DFSR:
            return self.dfsr
        if address >= 0xE0000000:
            return self.periph.get(address, 0)
        return int.from_bytes(bytes(self.read_memory_block8(address, 4)), "little")

    def write32(self, address, value):
        if address == DFSR:
            self.dfsr &= ~value
        elif address in (CFSR, HFSR):
            self.periph[address] = self.periph.get(address, 0) & ~value
        elif address >= 0xE0000000:
            self.periph[address] = value
        else:
            self.write_memory_block8(address, list(value.to_bytes(4, "little")))

    def read_memory(self, address, transfer_size=32, now=True):
        return int.from_bytes(bytes(self.read_memory_block8(address, transfer_size // 8)), "little")

    def read_memory_block32(self, address, count):
        return [self.read32(address + 4 * i) for i in range(count)]

    def invalidate_instruction_cache(self, address=None):
        self.icache_invalidations += 1

    # --- FPB（ハード）と RAM のソフトのブレークポイント（pyOCD の BreakpointManager の振る舞い） ---

    def set_breakpoint(self, address, kind=Target.BreakpointType.AUTO):
        in_code = address < 0x20000000
        if kind == Target.BreakpointType.HW or (kind == Target.BreakpointType.AUTO and in_code):
            if not in_code or (address not in self.hw and len(self.hw) >= self.hw_limit):
                return False
            self.hw.add(address)
            return True
        if in_code:
            return False                                     # Flash にメモリの書き込みで BKPT は書けない
        if address not in self.sw:
            self.sw[address] = bytes(self.read_memory_block8(address, 2))
            self.load(address, b"\x00\xbe")
        return True

    def remove_breakpoint(self, address):
        self.hw.discard(address)
        original = self.sw.pop(address, None)
        if original is not None:
            self.load(address, original)

    def find_breakpoint(self, address):
        if address in self.hw:
            return SimpleNamespace(addr=address, type=Target.BreakpointType.HW)
        if address in self.sw:
            return SimpleNamespace(addr=address, type=Target.BreakpointType.SW)
        return None

    # --- 実行 ---

    def get_halt_reason(self):
        for bit, reason in ((VCATCH, Target.HaltReason.VECTOR_CATCH), (DWTTRAP, Target.HaltReason.WATCHPOINT),
                            (BKPT, Target.HaltReason.BREAKPOINT), (EXTERNAL, Target.HaltReason.EXTERNAL),
                            (HALTED, Target.HaltReason.DEBUG)):
            if self.dfsr & bit:
                return reason
        return Target.HaltReason.USER

    def step(self, disable_interrupts=True, start=0, end=0, hook_cb=None):
        assert self.state == Target.State.HALTED
        self.steps.append(self.regs["pc"])
        self.step_masks.append(disable_interrupts)
        self.dfsr = 0
        if self.regs["pc"] in self.hw:                       # FPB の一致: 実行せずに止まる
            self.dfsr = BKPT
            return
        self.dfsr = self.execute() or HALTED

    def resume(self):
        if self.state != Target.State.HALTED:
            return
        self.resumes += 1
        self.dfsr = 0
        self.state = Target.State.RUNNING
        for _ in range(self.run_limit):
            if self.regs["pc"] in self.hw:                   # 再開した番地でも止まる（実機と同じ）
                self.state, self.dfsr = Target.State.HALTED, BKPT
                return
            reason = self.execute()
            if reason:
                self.state, self.dfsr = Target.State.HALTED, reason
                return

    def halt(self):
        if self.state == Target.State.RUNNING:
            self.state, self.dfsr = Target.State.HALTED, HALTED

    def _flags(self, value: int) -> None:
        value &= 0xFFFFFFFF
        x = self.regs["xpsr"] & ~(N | Z)
        self.regs["xpsr"] = x | (N if value >> 31 else 0) | (Z if value == 0 else 0)

    def _cond(self, cond: int) -> bool:
        x = self.regs["xpsr"]
        z, n = bool(x & Z), bool(x & N)
        return {0: z, 1: not z, 4: n, 5: not n, 14: True}[cond]

    def _enter(self, number: int, return_pc: int) -> None:
        sp = self.regs["sp"] - 32
        frame = [self.regs["r0"], self.regs["r1"], self.regs["r2"], self.regs["r3"], self.regs["r12"],
                 self.regs["lr"], return_pc, self.regs["xpsr"]]
        for i, word in enumerate(frame):
            self.write32(sp + 4 * i, word)
        self.regs.update(sp=sp, lr=EXC_RETURN_THREAD_MSP, pc=self.handlers[number],
                         xpsr=(self.regs["xpsr"] & ~0x1FF) | number)

    def _branch(self, value: int) -> None:
        if value >> 24 == 0xFF:
            sp = self.regs["sp"]
            r0, r1, r2, r3, r12, lr, pc, xpsr = (self.read32(sp + 4 * i) for i in range(8))
            self.regs.update(r0=r0, r1=r1, r2=r2, r3=r3, r12=r12, lr=lr, pc=pc, xpsr=xpsr, sp=sp + 32)
        else:
            self.regs["pc"] = value & ~1

    def execute(self) -> int:
        """1 命令。止まる理由（DFSR のビット。0 = 普通に進んだ）。"""
        r = self.regs
        pc = r["pc"]
        self.executed.append(pc)
        hw = self.read16(pc)
        if hw == 0xBF00:
            r["pc"] = pc + 2
        elif hw >> 8 == 0xBE:
            return BKPT
        elif hw >> 8 == 0xDF:
            self._enter(11, pc + 2)
        elif hw >> 11 == 0b00100:
            r[f"r{(hw >> 8) & 7}"] = hw & 0xFF
            self._flags(hw & 0xFF)
            r["pc"] = pc + 2
        elif hw >> 11 == 0b00110:
            rd = f"r{(hw >> 8) & 7}"
            r[rd] = (r[rd] + (hw & 0xFF)) & 0xFFFFFFFF
            self._flags(r[rd])
            r["pc"] = pc + 2
        elif hw >> 11 == 0b11100:
            r["pc"] = pc + 4 + (_sign(hw & 0x7FF, 11) << 1)
        elif hw >> 12 == 0b1101 and (hw >> 8) & 0xF < 14:
            r["pc"] = pc + 4 + (_sign(hw & 0xFF, 8) << 1) if self._cond((hw >> 8) & 0xF) else pc + 2
        elif hw & 0xFF87 == 0x4700:
            self._branch(r[self._reg((hw >> 3) & 0xF)])
        elif hw & 0xFF87 == 0x4780:
            target = r[self._reg((hw >> 3) & 0xF)]
            r["lr"] = (pc + 2) | 1
            r["pc"] = target & ~1
        elif hw >> 11 == 0b01001:
            r[f"r{(hw >> 8) & 7}"] = self.read32(((pc + 4) & ~3) + (hw & 0xFF) * 4)
            r["pc"] = pc + 2
        elif hw >> 11 == 0b10100:
            r[f"r{(hw >> 8) & 7}"] = ((pc + 4) & ~3) + (hw & 0xFF) * 4
            r["pc"] = pc + 2
        elif hw & 0xF500 == 0xB100:
            offset = ((hw >> 9) & 1) << 6 | ((hw >> 3) & 0x1F) << 1
            zero = r[f"r{hw & 7}"] == 0
            r["pc"] = pc + 4 + offset if zero != bool(hw & 0x0800) else pc + 2
        elif hw == 0xB500:
            r["sp"] -= 4
            self.write32(r["sp"], r["lr"])
            r["pc"] = pc + 2
        elif hw == 0xBD00:
            value = self.read32(r["sp"])
            r["sp"] += 4
            self._branch(value)
        elif hw >> 11 in (0b01100, 0b01101):
            address = r[f"r{(hw >> 3) & 7}"] + ((hw >> 6) & 0x1F) * 4
            if hw >> 11 == 0b01100:
                self.write32(address, r[f"r{hw & 7}"])
            else:
                r[f"r{hw & 7}"] = self.read32(address)
            r["pc"] = pc + 2
        elif hw & 0xF800 == 0xF000 and self.read16(pc + 2) & 0xD000 == 0xD000:
            lo = self.read16(pc + 2)
            s, j1, j2 = (hw >> 10) & 1, (lo >> 13) & 1, (lo >> 11) & 1
            i1, i2 = 1 - (j1 ^ s), 1 - (j2 ^ s)
            offset = _sign(s << 24 | i1 << 23 | i2 << 22 | (hw & 0x3FF) << 12 | (lo & 0x7FF) << 1, 25)
            r["lr"] = (pc + 4) | 1
            r["pc"] = pc + 4 + offset
        elif hw == 0xF8DF and self.read16(pc + 2) & 0xF000 == 0xF000:      # ldr.w pc, [pc, #imm12]
            self._branch(self.read32(((pc + 4) & ~3) + (self.read16(pc + 2) & 0xFFF)))
        elif hw == 0xE8DF and self.read16(pc + 2) & 0xFFF0 == 0xF000:
            index = r[self._reg(self.read16(pc + 2) & 0xF)]
            r["pc"] = pc + 4 + 2 * self.read_memory_block8(pc + 4 + index, 1)[0]
        else:
            raise AssertionError(f"0x{pc:08X}: 知らない命令 {hw:04X}")
        return 0

    @staticmethod
    def _reg(n: int) -> str:
        return {13: "sp", 14: "lr", 15: "pc"}.get(n, f"r{n}")


def thumb(*halfwords: int) -> bytes:
    return b"".join(h.to_bytes(2, "little") for h in halfwords)


def bl(source: int, target: int) -> bytes:
    """BL target（source に置く 32 ビットの命令）。"""
    offset = target - (source + 4)
    s = (offset >> 24) & 1
    i1, i2 = (offset >> 23) & 1, (offset >> 22) & 1
    j1, j2 = (1 - i1) ^ s, (1 - i2) ^ s
    hi = 0xF000 | s << 10 | (offset >> 12) & 0x3FF
    lo = 0xD000 | j1 << 13 | j2 << 11 | (offset >> 1) & 0x7FF
    return thumb(hi, lo)
