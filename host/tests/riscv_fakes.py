"""試験用: RISC-V の Debug Module（0.13）の偽物。RP2350 の Hazard3 に倣う: ハート 2 つ、抽象コマンドは GPR だけ
（CSR は not_supported）、プログラムバッファ 2 語（csrr / csrw / ebreak だけを実行する）、32 ビットまでのシステムバス、
トリガ 4 つ（実行アドレスの一致だけ。読み・書きの一致のビットは書いても残らない）。

走っているハートの進みは試験が reach(番地) で与える（トリガが一致すれば止まる）。"""

from dataclasses import dataclass, field

from fprobe import riscv_dm as dm

CSR_TSELECT, CSR_TDATA1, CSR_TDATA2, CSR_DCSR, CSR_DPC = 0x7A0, 0x7A1, 0x7A2, 0x7B0, 0x7B1


@dataclass
class Hart:
    halted: bool = False
    gprs: list[int] = field(default_factory=lambda: [0] * 32)
    csrs: dict[int, int] = field(default_factory=lambda: {CSR_DCSR: 0x40000003, CSR_DPC: 0x20000000, 0x301: 0x40901105})
    tselect: int = 0
    triggers: list[list[int]] = field(default_factory=lambda: [[0x20000000, 0] for _ in range(4)])  # [tdata1, tdata2]
    resumeack: bool = False
    pc: int = 0x20000000                                   # 走っているときの位置（reach で進める）


class FakeHazard3Dm:
    def __init__(self, harts: int = 2, memory: dict[int, int] | None = None):
        self.harts = [Hart() for _ in range(harts)]
        self.memory = dict(memory or {})                   # バイトの番地 → バイト
        self.dmcontrol = 0
        self.cmderr = 0
        self.data0 = 0
        self.progbuf = [0, 0]
        self.sbcs = (1 << 29) | (32 << 5) | 0b111         # sbversion 1、sbasize 32、8/16/32 ビット
        self.sbaddress = 0
        self.sbdata = 0
        self.bus_errors: set[int] = set()
        self.log: list[tuple] = []

    # --- DMI ----------------------------------------------------------------------------------

    @property
    def hartsel(self) -> int:
        return (self.dmcontrol >> 16) & 0x3FF

    def hart(self) -> Hart | None:
        return self.harts[self.hartsel] if self.hartsel < len(self.harts) else None

    def read(self, index: int) -> int:
        if index == dm.DMCONTROL:
            return self.dmcontrol & ~(dm.HALTREQ | dm.RESUMEREQ)
        if index == dm.DMSTATUS:
            h = self.hart()
            v = 2 | dm.AUTHENTICATED
            if h is None:
                return v | dm.ALLNONEXISTENT | (1 << 14)
            if h.halted:
                v |= dm.ALLHALTED | (1 << 8)
            else:
                v |= dm.ALLRUNNING | (1 << 10)
            if h.resumeack:
                v |= dm.ALLRESUMEACK | (1 << 16)
            return v
        if index == dm.ABSTRACTCS:
            return (2 << 24) | (self.cmderr << 8) | 1
        if index == dm.DATA0:
            return self.data0
        if index == dm.SBCS:
            return self.sbcs
        if index == dm.SBDATA0:
            value = self.sbdata
            if self.sbcs & dm.SBREADONDATA:
                self._sb_read()
            return value
        if index == dm.SBADDRESS0:
            return self.sbaddress
        return 0

    def write(self, index: int, value: int) -> None:
        self.log.append(("w", index, value))
        if index == dm.DMCONTROL:
            self.dmcontrol = value & ~(dm.HALTREQ | dm.RESUMEREQ)
            h = self.hart()
            if h is None:
                return
            if value & dm.HALTREQ and not h.halted:
                self._halt(h, 3, h.pc)
            if value & dm.RESUMEREQ and h.halted:
                h.resumeack = True
                if h.csrs[CSR_DCSR] & dm.DCSR_STEP:
                    self._halt(h, 4, h.csrs[CSR_DPC] + 4)
                else:
                    h.halted = False
                    h.pc = h.csrs[CSR_DPC]
            elif value & dm.RESUMEREQ:
                h.resumeack = True
            return
        if index == dm.ABSTRACTCS:
            self.cmderr &= ~((value >> 8) & 7)
            return
        if index == dm.DATA0:
            self.data0 = value
            return
        if index in (dm.PROGBUF0, dm.PROGBUF0 + 1):
            self.progbuf[index - dm.PROGBUF0] = value
            return
        if index == dm.COMMAND:
            self._command(value)
            return
        if index == dm.SBCS:
            keep = self.sbcs & ~((0x7 << 17) | dm.SBREADONADDR | dm.SBAUTOINCREMENT | dm.SBREADONDATA | (7 << 12))
            errors = (self.sbcs >> 12) & 7 & ~((value >> 12) & 7)
            self.sbcs = keep | (value & ((0x7 << 17) | dm.SBREADONADDR | dm.SBAUTOINCREMENT | dm.SBREADONDATA)) \
                | (errors << 12)
            return
        if index == dm.SBADDRESS0:
            self.sbaddress = value
            if self.sbcs & dm.SBREADONADDR:
                self._sb_read()
            return
        if index == dm.SBDATA0:
            size = self._sb_size()
            if self.sbaddress in self.bus_errors:
                self.sbcs |= 2 << 12
            else:
                for i in range(size):
                    self.memory[self.sbaddress + i] = (value >> (8 * i)) & 0xFF
            self._sb_advance()
            return

    # --- 実行 -----------------------------------------------------------------------------------

    def _halt(self, h: Hart, cause: int, pc: int) -> None:
        h.halted = True
        h.csrs[CSR_DPC] = pc
        h.csrs[CSR_DCSR] = (h.csrs[CSR_DCSR] & ~(7 << 6)) | (cause << 6)

    def reach(self, address: int, hart: int = 0) -> bool:
        """走っているハートが address の命令に来た。実行のトリガが一致すれば止まって True。"""
        h = self.harts[hart]
        assert not h.halted
        h.pc = address
        for tdata1, tdata2 in ((t[0], t[1]) for t in h.triggers):
            if tdata1 & dm.MC_EXECUTE and tdata2 == address:
                self._halt(h, 2, address)
                return True
        return False

    def _command(self, value: int) -> None:
        h = self.hart()
        if self.cmderr:
            return
        if (value >> 24) != 0 or not h.halted:
            self.cmderr = 4 if not h.halted else 2
            return
        regno = value & 0xFFFF
        if value & dm.TRANSFER:
            if not 0x1000 <= regno < 0x1020:
                self.cmderr = 2                                    # CSR は抽象コマンドで触れない
                return
            n = regno - 0x1000
            if value & dm.WRITE:
                if n:
                    h.gprs[n] = self.data0
            else:
                self.data0 = h.gprs[n]
        if value & dm.POSTEXEC:
            for instruction in self.progbuf:
                if instruction == dm.EBREAK:
                    break
                self._execute(h, instruction)

    def _execute(self, h: Hart, instruction: int) -> None:
        opcode, rd, funct3 = instruction & 0x7F, (instruction >> 7) & 0x1F, (instruction >> 12) & 7
        rs1, csr = (instruction >> 15) & 0x1F, instruction >> 20
        assert opcode == 0x73 and funct3 in (1, 2), hex(instruction)
        old = self._csr_read(h, csr)
        if funct3 == 1:
            self._csr_write(h, csr, h.gprs[rs1])
        elif rs1:
            self._csr_write(h, csr, old | h.gprs[rs1])
        if rd:
            h.gprs[rd] = old

    def _csr_read(self, h: Hart, csr: int) -> int:
        if csr == CSR_TSELECT:
            return h.tselect
        if csr in (CSR_TDATA1, CSR_TDATA2):
            return h.triggers[h.tselect][csr - CSR_TDATA1] if csr == CSR_TDATA1 else h.triggers[h.tselect][1]
        return h.csrs.get(csr, 0)

    def _csr_write(self, h: Hart, csr: int, value: int) -> None:
        if csr == CSR_TSELECT:
            if value < len(h.triggers):
                h.tselect = value
        elif csr == CSR_TDATA1:
            # 実行の一致だけを受け付ける（type 2 は読み出し専用。load / store のビットは残らない）
            h.triggers[h.tselect][0] = (0x2 << 28) | (value & ((1 << 27) | 0xFFFC)) if value else 0x20000000
        elif csr == CSR_TDATA2:
            h.triggers[h.tselect][1] = value
        else:
            h.csrs[csr] = value

    # --- システムバス ------------------------------------------------------------------------------

    def _sb_size(self) -> int:
        return 1 << ((self.sbcs >> 17) & 7)

    def _sb_read(self) -> None:
        size = self._sb_size()
        if self.sbaddress in self.bus_errors:
            self.sbcs |= 2 << 12
            self.sbdata = 0
        else:
            self.sbdata = sum(self.memory.get(self.sbaddress + i, 0) << (8 * i) for i in range(size))
        self.log.append(("sbread", self.sbaddress, size))
        self._sb_advance()

    def _sb_advance(self) -> None:
        if self.sbcs & dm.SBAUTOINCREMENT:
            self.sbaddress += self._sb_size()
