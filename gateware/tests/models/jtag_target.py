"""JTAG のチェーン（TAP 状態機械 + 装置）のピンレベル振る舞いモデル（cocotb, テスト専用）。

- TAP は TCK の立ち上がりで TMS / TDI を取り込み、立ち下がりで TDO を変える（tdo_delay_ns: 立ち下がりから TDO が変わるまでの
  遅れ。配線の往復とターゲットの出力遅延）。
- チェーン: TDI → devices[-1] → … → devices[0] → TDO（CMSIS-DAP の装置番号 0 が TDO 側）。
- 装置: Bypass（任意長の IR、IDCODE か BYPASS）と JtagDp（ARM の JTAG-DP: IR 4 ビット、ABORT / DPACC / APACC /
  IDCODE）。JTAG-DP の DP・AP のレジスタは SWD モデル（swd_target）と同じ振る舞いを使い、読み値は次のスキャンで返る。
"""

from types import SimpleNamespace

import cocotb
from cocotb.triggers import FallingEdge, ReadOnly, RisingEdge, Timer

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

    def __init__(self, wait_after_ap: int = 0, **registers):
        self.regs = SwdTarget(SimpleNamespace(target_swdio=SimpleNamespace(value=None)), **registers)
        self.result = 0                       # 直前の読みの結果（次のスキャンで返す）
        # AP の転送を受けた後、続く wait_after_ap 回の DPACC / APACC のスキャンは WAIT を返し、そのスキャンの要求は捨てる
        # （ADIv5: 前の転送が終わっていない。実機の Cortex-A7 の DCC の速い読みで起きる）。WAIT のスキャンの値は不定
        self.wait_after_ap = wait_after_ap
        self.busy = 0
        self.waiting = False
        self.waits = 0                        # 返した WAIT の数
        self.scans: list[tuple] = []          # (IR, RnW, A, 値)
        super().__init__(ir_len=4, idcode=JTAG_DP_IDCODE, idcode_ir=self.IR_IDCODE)

    def dr_length(self) -> int:
        if self.ir in (self.IR_ABORT, self.IR_DPACC, self.IR_APACC):
            return 35
        return super().dr_length()

    def capture_dr(self) -> int:
        if self.ir in (self.IR_DPACC, self.IR_APACC):
            self.waiting = self.busy > 0
            if self.waiting:
                self.waits += 1
                return 0x5A5A5A5A << 3 | ACK_WAIT        # 値は不定（前の結果ではない）
            return self.result << 3 | ACK_OK_FAULT
        return super().capture_dr()

    def update_dr(self, value: int):
        if self.ir not in (self.IR_ABORT, self.IR_DPACC, self.IR_APACC):
            return
        if self.ir != self.IR_ABORT and self.waiting:
            self.busy -= 1                                # WAIT のスキャン: 要求は捨てる
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
            self.busy = self.wait_after_ap
        else:
            regs._write(1, addr, data)
            self.result = 0
            self.busy = self.wait_after_ap


class JtagChain:
    def __init__(self, dut, devices, *, tck="tck", tms="tms", tdi="tdi", tdo="tdo", tdo_delay_ns: float = 0):
        self.dut, self.devices, self.tdo_delay_ns = dut, devices, tdo_delay_ns
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
                if self.tdo_delay_ns:
                    cocotb.start_soon(self._late_tdo(self.shift[0] & 1))
                else:
                    self.tdo.value = self.shift[0] & 1

    async def _late_tdo(self, bit: int):
        await Timer(self.tdo_delay_ns, unit="ns")
        self.tdo.value = bit

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
