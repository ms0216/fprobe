"""試験用: Arm の A プロファイル（Cortex-A7）と ARM11（ARM1176）の外部デバッグの偽物（U32）。

- ArmCore: 停止デバッグのコア。DSCR・DTR・ブレークポイント・ウォッチポイントと、fprobe が ITR に入れる命令だけを
  実行する（DCC の MCR / MRC、MOV r0, pc / MOV pc, r0、MRS / MSR、r1・[r0] の後置き加算の LDR / STR、キャッシュの
  操作、DSB / ISB）。知らない命令は AssertionError。走っているコアの進みは試験が reach(番地) で与える。
- V7DebugBlock: ARMv7 のメモリの番地のデバッグレジスタ（APB の 4 KB の窓）。OS ロックが掛かっている間は DSCR・DRCR・
  ITR への書き込みを無視する（解かずに使うと動かない）。
- ApbAp: APB-AP（FakeMemAp）。ROM テーブル・CTI・コアのデバッグの窓を番地に並べる。
- Arm11Tap: ARM1176 の TAP（jtag_fakes.Tap）。SCAN_N・INTEST / EXTEST・ITRSEL・HALT・RESTART とチェーン 0 / 1 / 5 / 7。
  ITR の命令は Update-DR の直後に Run-Test/Idle に入ったとき（IR が ITRSEL か、INTEST / EXTEST でチェーン 4 / 5）に
  実行される（OpenOCD の arm11 の手順の前提と同じ）。"""

from dataclasses import dataclass, field

from dap_fakes import FakeMemAp
from jtag_fakes import Tap

from fprobe.arm_debug import CP15, DSB, ISB, LDC_DTRTX, LOAD_STORE, MOV_PC_R0, MOV_R0_PC, MRS_R0_CPSR, MSR_CPSR_R0

HALTED, RESTARTED = 1 << 0, 1 << 1
SDABORT, UND = 1 << 6, 1 << 8
INTDIS, ITREN, HDBGEN = 1 << 11, 1 << 13, 1 << 14
WRITABLE = (1 << 11) | (1 << 12) | (1 << 13) | (1 << 14) | (1 << 15) | (3 << 20)
CPSR_T, CPSR_I = 1 << 5, 1 << 7
EXTDCC, FAST = 3 << 20, 2 << 20                         # DSCR.ExtDCCmode と速いモード
_LOADS = {v[0]: k for k, v in LOAD_STORE.items()}
_STORES = {v[1]: k for k, v in LOAD_STORE.items()}
_CACHE_OPS = {v: k for k, v in CP15.items() if k not in ("ctr", "mpidr")}


@dataclass
class ArmCore:
    arch: int = 7
    brps: int = 6
    wrps: int = 4
    mpidr: int = 0x80000F00
    ctr: int = 0x84448003                                  # Cortex-A7: D の行 64 バイト
    memory: dict[int, int] = field(default_factory=dict)   # バイトの番地 → バイト
    faults: list[tuple[int, int]] = field(default_factory=list)   # アボートする番地の範囲
    regs: list[int] = field(default_factory=lambda: [0] * 16)     # r15 = PC（止まっている命令の番地）
    cpsr: int = 0x13                                       # SVC、ARM、割り込みは許可
    irq_pending: bool = False                              # 再開するとすぐ IRQ に入る（ステップの割り込みの止め方）
    halted: bool = False
    restarted: bool = False
    moe: int = 0
    ctrl: int = 0                                          # DSCR の書けるビット
    sticky: int = 0
    rx: int = 0
    rxfull: bool = False
    tx: int = 0
    txfull: bool = False
    bvr: list[int] = field(default_factory=lambda: [0] * 16)
    bcr: list[int] = field(default_factory=lambda: [0] * 16)
    wvr: list[int] = field(default_factory=lambda: [0] * 16)
    wcr: list[int] = field(default_factory=lambda: [0] * 16)
    executed: list[int] = field(default_factory=list)
    ignored: list[int] = field(default_factory=list)       # ITR が無効・走っている間に来た命令
    cache_ops: list[tuple[str, int]] = field(default_factory=list)
    restarts: int = 0
    latched: int | None = None                             # DCC の速いモードで覚えた命令（DTRTX を読むたびに実行し直す）

    @property
    def thumb(self) -> bool:
        return bool(self.cpsr & CPSR_T)

    def dscr(self) -> int:
        return (HALTED if self.halted else 0) | (RESTARTED if self.restarted else 0) | self.moe << 2 | self.sticky \
            | self.ctrl | 1 << 24 | (1 << 29 | 1 << 26 if self.txfull else 0) \
            | (1 << 30 | 1 << 27 if self.rxfull else 0)

    def write_dscr(self, value: int) -> None:
        self.ctrl = value & WRITABLE
        if self.arch == 6:
            self.sticky &= value                            # ARM11: 0 を書いた sticky ビットは消える（前提）

    def didr(self) -> int:
        return (self.wrps - 1) << 28 | (self.brps - 1) << 24 | (5 if self.arch == 7 else 2) << 16

    # --- 実行制御 --------------------------------------------------------------------------------------

    def halt_request(self) -> None:
        if self.ctrl & HDBGEN and not self.halted:
            self.halted, self.restarted, self.moe = True, False, 0

    def restart(self) -> None:
        if not self.halted:
            return
        self.restarts += 1
        self.halted, self.restarted = False, True
        for i in range(self.brps):                          # mismatch: 今の命令だけを実行して次で止まる
            if self.bcr[i] & 1 and (self.bcr[i] >> 20) & 0xF == 4 and self.bvr[i] == self.regs[15] & ~3:
                nxt = self.regs[15] + (2 if self.thumb else 4)
                masked = self.ctrl & INTDIS if self.arch == 7 else self.cpsr & CPSR_I
                if self.irq_pending and not masked:
                    nxt = 0x18
                self.regs[15] = nxt
                self.halted, self.moe = True, 1
                return

    def reach(self, address: int) -> bool:
        """走っているコアが address の命令に来た。番地の一致のブレークポイントがあれば止まって True。"""
        assert not self.halted
        self.regs[15] = address
        for i in range(self.brps):
            if self.bcr[i] & 1 and (self.bcr[i] >> 20) & 0xF == 0 and self.bvr[i] == address & ~3 \
                    and (self.bcr[i] >> 5) >> (address & 3) & 1:
                self.halted, self.moe = True, 1
                return True
        return False

    def access(self, address: int, write: bool) -> bool:
        """走っているコアが address を読む / 書く。一致するウォッチポイントがあれば止まって True。"""
        assert not self.halted
        for i in range(self.wrps):
            wcr = self.wcr[i]
            if wcr & 1 and self.wvr[i] == address & ~3 and (wcr >> 5) >> (address & 3) & 1 \
                    and (wcr >> 3) & (2 if write else 1):
                self.halted, self.moe = True, 0xA if self.arch == 7 else 2
                return True
        return False

    # --- ITR -------------------------------------------------------------------------------------------

    def _fault(self, address: int, size: int) -> bool:
        return any(lo <= a < hi for a in range(address, address + size) for lo, hi in self.faults)

    def issue(self, op: int) -> None:
        if not (self.halted and self.ctrl & ITREN):
            self.ignored.append(op)
            return
        self.executed.append(op)
        r = self.regs
        if op & 0xFFF00FFF == 0xEE000E15:                   # MCR p14 → DTRTX
            self.tx, self.txfull = r[(op >> 12) & 0xF], True
        elif op & 0xFFF00FFF == 0xEE100E15:                 # MRC p14 ← DTRRX
            r[(op >> 12) & 0xF], self.rxfull = self.rx, False
        elif op == MOV_R0_PC:
            r[0] = r[15] + (4 if self.thumb else 8)
        elif op == MOV_PC_R0:
            r[15] = r[0] & ~(1 if self.thumb else 3)
        elif op == MRS_R0_CPSR:
            r[0] = self.cpsr
        elif op == MSR_CPSR_R0:
            self.cpsr = r[0]
        elif op == LDC_DTRTX:                               # LDC p14, c5, [r0], #4: メモリの語を DTRTX へ
            if self._fault(r[0], 4):
                self.sticky |= SDABORT
                return
            self.tx, self.txfull = sum(self.memory.get(r[0] + i, 0) << 8 * i for i in range(4)), True
            r[0] += 4
        elif op in _LOADS:
            size = _LOADS[op]
            if self._fault(r[0], size):
                self.sticky |= SDABORT
                return
            r[1] = sum(self.memory.get(r[0] + i, 0) << 8 * i for i in range(size))
            r[0] += size
        elif op in _STORES:
            size = _STORES[op]
            if self._fault(r[0], size):
                self.sticky |= SDABORT
                return
            for i in range(size):
                self.memory[r[0] + i] = (r[1] >> 8 * i) & 0xFF
            r[0] += size
        elif op in (DSB, ISB):
            if self.arch != 7:
                self.sticky |= UND
            self.cache_ops.append(("dsb" if op == DSB else "isb", 0))
        elif op == CP15["ctr"]:
            r[0] = self.ctr
        elif op == CP15["mpidr"]:
            r[0] = self.mpidr
        elif op in _CACHE_OPS:
            self.cache_ops.append((_CACHE_OPS[op], r[0]))
        else:
            raise AssertionError(f"知らない命令 0x{op:08X}")


# --- ARMv7: メモリの番地のデバッグレジスタ ----------------------------------------------------------------

ARM_PIDR = {0xFE0: 0x07, 0xFE4: 0xBC, 0xFE8: 0x0B, 0xFEC: 0x00, 0xFD0: 0x04}     # 部品 0xC07（Cortex-A7）、Arm


def cidr(cls: int) -> dict[int, int]:
    return {0xFF0: 0x0D, 0xFF4: cls << 4, 0xFF8: 0x05, 0xFFC: 0xB1}


class V7DebugBlock:
    def __init__(self, core: ArmCore):
        self.core = core
        self.os_lock = True                                 # 電源投入で OS ロックが掛かっている（ARMv7.1）
        self.unlocked = False

    def read(self, offset: int) -> int:
        c = self.core
        if offset == 0x000:
            return c.didr()
        if offset == 0x088:
            return c.dscr()
        if offset == 0x08C:
            value, c.txfull = c.tx, False
            if c.ctrl & EXTDCC == FAST and c.latched is not None and not c.sticky:
                c.issue(c.latched)                          # 速いモード: 読むたびに覚えた命令を実行し直す
            return value
        if offset == 0x304:
            return 1 | (2 if self.os_lock else 0)
        if offset == 0x314:
            return 1                                        # PU（電源が入っている）
        if offset == 0xFCC:
            return 0x15                                     # 処理系のデバッグ
        for base, regs in ((0x100, c.bvr), (0x140, c.bcr), (0x180, c.wvr), (0x1C0, c.wcr)):
            if base <= offset < base + 0x40:
                return regs[(offset - base) // 4]
        return {**ARM_PIDR, **cidr(9)}.get(offset, 0)

    def write(self, offset: int, value: int) -> None:
        c = self.core
        if offset == 0x300:
            self.os_lock = value == 0xC5ACCE55
            return
        if offset == 0xFB0:
            self.unlocked = value == 0xC5ACCE55
            return
        if offset == 0x080:
            c.rx, c.rxfull = value, True
            return
        if self.os_lock and offset in (0x084, 0x088, 0x090):
            return
        if offset == 0x084:
            if c.ctrl & EXTDCC == FAST:                     # 速いモード: 書いても実行しない（DTRTX を読んだときに実行する。実機の Cortex-A7）
                c.latched = value
            else:
                c.latched = None
                c.issue(value)
        elif offset == 0x088:
            c.write_dscr(value)
        elif offset == 0x090:
            if value & 4:
                c.sticky = 0
            if value & 1:
                c.halt_request()
            if value & 2:
                c.restart()
        else:
            for base, regs in ((0x100, c.bvr), (0x140, c.bcr), (0x180, c.wvr), (0x1C0, c.wcr)):
                if base <= offset < base + 0x40:
                    regs[(offset - base) // 4] = value


class ApbAp(FakeMemAp):
    """APB-AP: ROM テーブル（rom）・部品の窓（blocks: 番地 → V7DebugBlock か {番地: 値}）。"""

    def __init__(self, rom: int | None, blocks: dict[int, object]):
        super().__init__(idr=0x44770002, base=(rom | 3) if rom is not None else 0x00000002,
                         readable=(), on_write=self._write)
        self.blocks = blocks

    def _find(self, address: int):
        base = address & ~0xFFF
        return self.blocks.get(base), address - base

    def read(self, address: int) -> int:
        block, offset = self._find(address)
        if block is None:
            return super().read(address)
        return block.read(offset) if isinstance(block, V7DebugBlock) else block.get(offset, 0)

    def _write(self, offset: int, value: int) -> bool:
        if offset != 0x0C:
            return False
        block, inner = self._find(self.tar)
        if isinstance(block, V7DebugBlock):
            block.write(inner, value)
        return True


def rom_table(base: int, children: list[int]) -> dict[int, int]:
    """class 1 の ROM テーブル: 子の番地（base からの差）を並べる。"""
    words = {4 * i: ((child - base) & 0xFFFFF000) | 3 for i, child in enumerate(children)}
    words[4 * len(children)] = 0
    return {**words, **cidr(1), 0xFE0: 0xA1, 0xFE4: 0xB4, 0xFE8: 0x0B, 0xFD0: 0x04}


def cti() -> dict[int, int]:
    return {**cidr(9), 0xFCC: 0x14, 0xFE0: 0x06, 0xFE4: 0xB9, 0xFE8: 0x0B, 0xFD0: 0x04}


def bcm2836(cores: list[ArmCore], with_rom: bool = True) -> tuple[ApbAp, list[V7DebugBlock]]:
    """Raspberry Pi 2（BCM2836）の APB の並び: デバッグ 0x80010000 + 0x2000 × n、CTI 0x80018000 + 0x1000 × n、
    ROM テーブル 0x80000000（with_rom = False なら BASE が「無い」）。"""
    debug = [V7DebugBlock(c) for c in cores]
    blocks: dict[int, object] = {0x80010000 + 0x2000 * i: b for i, b in enumerate(debug)}
    blocks.update({0x80018000 + 0x1000 * i: cti() for i in range(len(cores))})
    rom = None
    if with_rom:
        rom = 0x80000000
        children = sorted(blocks)
        blocks[rom] = rom_table(rom, children)
    return ApbAp(rom, blocks), debug


# --- ARM11: JTAG の TAP ---------------------------------------------------------------------------------

EXTEST, SCAN_N, RESTART, HALT, INTEST, ITRSEL, IDCODE = 0x00, 0x02, 0x04, 0x08, 0x0C, 0x1D, 0x1E


@dataclass
class Arm11Tap(Tap):
    core: ArmCore | None = None
    chain: int = 3
    itr: int = 0
    sc7_read: tuple[int, int] = (0, 0)
    sc7_writes: list[tuple[int, int]] = field(default_factory=list)

    def dr(self) -> tuple[int, int]:
        c = self.core
        if self.ir == SCAN_N:
            return 5, 0b10000
        if self.ir == ITRSEL:
            return 33, self.itr | 1 << 32                   # InstCompl（命令はすぐ終わる）
        if self.ir in (EXTEST, INTEST):
            if self.chain == 0:
                return 32, c.didr()
            if self.chain == 1:
                return 32, c.dscr()
            if self.chain == 5:
                if self.ir == INTEST:
                    value, valid = c.tx, c.txfull
                    c.txfull = False
                    return 34, value | int(valid) << 32 | 1 << 33
                return 34, int(not c.rxfull) << 32 | 1 << 33
            if self.chain == 7:
                address, value = self.sc7_read
                return 40, 1 | value << 1 | address << 33
            return 1, 0
        return super().dr()

    def on_state(self, prev: str, state: str) -> None:
        c = self.core
        if state == "UPDR":
            value = self.sr
            if self.ir == SCAN_N:
                self.chain = value & 0x1F
            elif self.ir == ITRSEL:
                self.itr = value & 0xFFFFFFFF
            elif self.ir == EXTEST and self.chain == 1:
                c.write_dscr(value & 0xFFFFFFFF)
            elif self.ir == EXTEST and self.chain == 5:
                c.rx, c.rxfull = value & 0xFFFFFFFF, True
            elif self.ir == EXTEST and self.chain == 7:
                self._sc7(value & 1, (value >> 1) & 0xFFFFFFFF, (value >> 33) & 0x7F)
        elif state == "RTI" and prev == "UPDR":
            if self.ir == ITRSEL or (self.ir in (EXTEST, INTEST) and self.chain in (4, 5)):
                c.issue(self.itr)
        elif state == "RTI" and prev == "UPIR":
            if self.ir == HALT:
                c.halt_request()
            elif self.ir == RESTART:
                c.restart()

    def _sc7(self, write: int, value: int, address: int) -> None:
        c = self.core
        regs = {64: c.bvr, 80: c.bcr, 96: c.wvr, 112: c.wcr}
        base = address & ~0xF
        if not write:
            self.sc7_read = (address, regs[base][address & 0xF] if base in regs else 0)
            return
        self.sc7_writes.append((address, value))
        if base in regs:
            regs[base][address & 0xF] = value


def arm11_tap(core: ArmCore) -> Arm11Tap:
    return Arm11Tap(ir_length=5, idcode=0x07B7617F, idcode_instruction=IDCODE, core=core)
