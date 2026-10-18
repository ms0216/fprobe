"""JTAG のチェーン（TAP 状態機械 + 装置）のピンレベル振る舞いモデル（cocotb, テスト専用）。

- TAP は TCK の立ち上がりで TMS / TDI を取り込み、立ち下がりで TDO を変える。
- チェーン: TDI → devices[-1] → … → devices[0] → TDO（CMSIS-DAP の装置番号 0 が TDO 側）。
- 装置: Bypass（任意長の IR、IDCODE か BYPASS）と JtagDp（ARM の JTAG-DP: IR 4 ビット、ABORT / DPACC / APACC /
  IDCODE）。JTAG-DP の DP・AP のレジスタは SWD モデル（swd_target）と同じ振る舞いを使い、読み値は次のスキャンで返る。
"""

from types import SimpleNamespace

from cocotb.triggers import FallingEdge, ReadOnly, RisingEdge

from tests.models.swd_target import SwdTarget

JTAG_DP_IDCODE = 0x4BA00477
BSC_IDCODE = 0x06433041
ACK_OK_FAULT, ACK_WAIT = 0b010, 0b001

RESET, IDLE, SEL_DR, CAP_DR, SH_DR, EX1_DR, PAU_DR, EX2_DR, UPD_DR, \
    SEL_IR, CAP_IR, SH_IR, EX1_IR, PAU_IR, EX2_IR, UPD_IR = range(16)
NEXT = {  # state: (TMS=0, TMS=1)
    RESET: (IDLE, RESET), IDLE: (IDLE, SEL_DR), SEL_DR: (CAP_DR, SEL_IR), CAP_DR: (SH_DR, EX1_DR),
    SH_DR: (SH_DR, EX1_DR), EX1_DR: (PAU_DR, UPD_DR), PAU_DR: (PAU_DR, EX2_DR), EX2_DR: (SH_DR, UPD_DR),
    UPD_DR: (IDLE, SEL_DR), SEL_IR: (CAP_IR, RESET), CAP_IR: (SH_IR, EX1_IR), SH_IR: (SH_IR, EX1_IR),
    EX1_IR: (PAU_IR, UPD_IR), PAU_IR: (PAU_IR, EX2_IR), EX2_IR: (SH_IR, UPD_IR), UPD_IR: (IDLE, SEL_DR),
}


class Bypass:
    """IDCODE と BYPASS だけの装置（例: STM32 のバウンダリスキャン TAP、IR 5 ビット）。"""

    def __init__(self, ir_len=5, idcode=BSC_IDCODE, idcode_ir=0b00001):
        self.ir_len, self.idcode, self.idcode_ir = ir_len, idcode, idcode_ir
        self.reset()

    def reset(self):
        self.ir = self.idcode_ir

    def dr_length(self) -> int:
        return 32 if self.ir == self.idcode_ir else 1

    def capture_dr(self) -> int:
        return self.idcode if self.ir == self.idcode_ir else 0

    def update_dr(self, value: int):
        pass


class JtagDp(Bypass):
    IR_ABORT, IR_DPACC, IR_APACC, IR_IDCODE = 0x8, 0xA, 0xB, 0xE

    def __init__(self, **registers):
        self.regs = SwdTarget(SimpleNamespace(target_swdio=SimpleNamespace(value=None)), **registers)
        self.result = 0                       # 直前の読みの結果（次のスキャンで返す）
        self.scans: list[tuple] = []          # (IR, RnW, A, 値)
        super().__init__(ir_len=4, idcode=JTAG_DP_IDCODE, idcode_ir=self.IR_IDCODE)

    def dr_length(self) -> int:
        if self.ir in (self.IR_ABORT, self.IR_DPACC, self.IR_APACC):
            return 35
        return super().dr_length()

    def capture_dr(self) -> int:
        if self.ir in (self.IR_DPACC, self.IR_APACC):
            return self.result << 3 | ACK_OK_FAULT
        return super().capture_dr()

    def update_dr(self, value: int):
        if self.ir not in (self.IR_ABORT, self.IR_DPACC, self.IR_APACC):
            return
        rnw, addr, data = value & 1, (value >> 1 & 3) << 2, value >> 3 & 0xFFFFFFFF
        self.scans.append((self.ir, rnw, addr, data))
        regs = self.regs
        if self.ir == self.IR_ABORT:
            regs._write(0, 0x0, data)
        elif self.ir == self.IR_DPACC:
            if rnw:
                self.result = 0 if addr == 0xC else regs._read(0, addr)     # JTAG-DP の RDBUFF は 0
            else:
                regs._write(0, addr, data)
                self.result = 0
        elif rnw:
            self.result = regs._ap_read((regs.select >> 4 & 0xF) << 4 | addr)
        else:
            regs._write(1, addr, data)
            self.result = 0


class JtagChain:
    def __init__(self, dut, devices, *, tck="tck", tms="tms", tdi="tdi", tdo="tdo"):
        self.dut, self.devices = dut, devices
        self.tck, self.tms, self.tdi, self.tdo = (getattr(dut, n) for n in (tck, tms, tdi, tdo))
        self.state = RESET
        self.shift = [0] * len(devices)       # 装置ごとのシフトレジスタ
        self.length = [1] * len(devices)
        self.tdo.value = 1

    async def run(self):
        while True:
            await RisingEdge(self.tck)
            await ReadOnly()
            tms, tdi = int(self.tms.value), int(self.tdi.value)
            self._edge(tms, tdi)
            await FallingEdge(self.tck)
            if self.state in (SH_DR, SH_IR):
                self.tdo.value = self.shift[0] & 1

    def _edge(self, tms: int, tdi: int):
        if self.state in (SH_DR, SH_IR):                          # TDI 側から 1 ビットずつ送る
            carry = tdi
            for k in reversed(range(len(self.devices))):
                out = self.shift[k] & 1
                self.shift[k] = self.shift[k] >> 1 | carry << (self.length[k] - 1)
                carry = out
        nxt = NEXT[self.state][tms]
        # Capture / Update は、その状態に入った時点で行う（間にシフトが無いので実機と同じ結果）
        if nxt == RESET:
            for d in self.devices:
                d.reset()
        elif nxt == CAP_DR:
            for k, d in enumerate(self.devices):
                self.length[k] = d.dr_length()
                self.shift[k] = d.capture_dr()
        elif nxt == CAP_IR:
            for k, d in enumerate(self.devices):
                self.length[k] = d.ir_len
                self.shift[k] = 0b0001
        elif nxt == UPD_DR:
            for k, d in enumerate(self.devices):
                d.update_dr(self.shift[k] & ((1 << self.length[k]) - 1))
        elif nxt == UPD_IR:
            for k, d in enumerate(self.devices):
                d.ir = self.shift[k] & ((1 << d.ir_len) - 1)
        self.state = nxt
