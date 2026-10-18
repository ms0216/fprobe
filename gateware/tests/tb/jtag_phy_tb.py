"""jtag_phy のテストベンチ。チェーンは STM32 と同じ: TDO 側に JTAG-DP（装置 0、IR 4）、TDI 側にバウンダリスキャン
TAP（装置 1、IR 5）。"""

import cocotb
from cocotb.triggers import RisingEdge

from tests.models.jtag_target import BSC_IDCODE, JTAG_DP_IDCODE, Bypass, JtagChain, JtagDp
from tests.models.swd_target import CDBGPWRUPACK, CDBGPWRUPREQ
from tests.tb.common import start_clock_and_reset

OP_SEQ_OUT, OP_SEQ_IN, OP_TRANSFER, OP_PINS = 0, 1, 2, 3
OK, WAIT = 0b001, 0b010
DP_R, DP_W, AP_R, AP_W = 0b0010, 0b0000, 0b0011, 0b0001
A_CTRL, A_SELECT, A_RDBUFF = 0x4, 0x8, 0xC
A_CSW, A_TAR, A_DRW = 0x0, 0x4, 0xC


def request(kind: int, addr: int) -> int:
    return kind | (addr >> 2) << 2


class Phy:
    def __init__(self, dut):
        self.dut = dut

    async def command(self, op, *, count=1, req=0, data=0):
        d = self.dut
        d.cmd_op.value, d.cmd_count.value, d.cmd_request.value, d.cmd_data.value = op, count, req, data
        d.cmd_valid.value = 1
        while True:
            await RisingEdge(d.clk)
            if d.cmd_ready.value == 1:
                break
        d.cmd_valid.value = 0
        while True:
            await RisingEdge(d.clk)
            if d.rsp_valid.value == 1:
                return int(d.rsp_ack.value), int(d.rsp_data.value)

    async def transfer(self, kind, addr, data=0):
        return await self.command(OP_TRANSFER, req=request(kind, addr), data=data)


async def setup(dut, idle_cycles=0):
    dp = JtagDp()
    chain = JtagChain(dut, [dp, Bypass()])
    dut.enable.value = 1
    dut.half_period.value = 2
    dut.idle_cycles.value = idle_cycles
    dut.dr_before.value, dut.dr_after.value = 0, 1
    dut.ir_before.value, dut.ir_after.value, dut.ir_len.value = 0, 5, 4
    dut.cmd_valid.value = 0
    await start_clock_and_reset(dut)
    cocotb.start_soon(chain.run())
    phy = Phy(dut)
    await phy.command(OP_SEQ_IN, count=6, req=1, data=0x3F)          # Test-Logic-Reset
    await phy.command(OP_SEQ_IN, count=1, req=0, data=1)             # Run-Test/Idle
    return phy, dp


@cocotb.test()
async def sequence_reads_both_idcodes(dut):
    phy, _ = await setup(dut)                                        # リセット後は両方 IDCODE が選ばれている
    await phy.command(OP_SEQ_IN, count=1, req=1)                     # Select-DR
    await phy.command(OP_SEQ_IN, count=2, req=0)                     # Capture-DR → Shift-DR
    _, tdo = await phy.command(OP_SEQ_IN, count=64, req=0, data=(1 << 64) - 1)
    assert tdo & 0xFFFFFFFF == JTAG_DP_IDCODE and tdo >> 32 == BSC_IDCODE


@cocotb.test()
async def dp_and_memory_access_like_swd(dut):
    phy, dp = await setup(dut, idle_cycles=2)
    assert await phy.transfer(DP_R, 0x0) == (OK, JTAG_DP_IDCODE)     # IDCODE 命令
    assert (await phy.transfer(DP_W, A_CTRL, CDBGPWRUPREQ))[0] == OK
    ack, ctrl = await phy.transfer(DP_R, A_CTRL)                      # DP の読みは値がその場で返る
    assert ack == OK and ctrl & CDBGPWRUPACK
    await phy.transfer(DP_W, A_SELECT, 0)
    await phy.transfer(AP_W, A_CSW, 0x23000012)                       # 32 ビット、自動増加
    await phy.transfer(AP_W, A_TAR, 0x20000000)
    for v in (0x11111111, 0x22222222, 0x33333333):
        assert (await phy.transfer(AP_W, A_DRW, v))[0] == OK
    assert dp.regs.memory[0x20000004] == 0x22222222
    await phy.transfer(AP_W, A_TAR, 0x20000000)
    await phy.transfer(AP_R, A_DRW)                                   # AP の読みはポスト（SWD と同じ）
    assert (await phy.transfer(AP_R, A_DRW))[1] == 0x11111111
    assert (await phy.transfer(DP_R, A_RDBUFF))[1] == 0x22222222
    # IR は変わるときだけスキャンする
    irs = [s[0] for s in dp.scans]
    assert irs.count(JtagDp.IR_APACC) >= 7


@cocotb.test()
async def abort_and_pins(dut):
    phy, dp = await setup(dut)
    assert (await phy.transfer(DP_W, 0x0, 0x1E))[0] == OK             # DP の A = 0 への書き込み = ABORT
    assert dp.scans[-1][0] == JtagDp.IR_ABORT and dp.scans[-1][3] == 0x1E
    _, pins = await phy.command(OP_PINS, data=1 << 10 | 1 << 9 | 1 << 8)   # TCK = TMS = TDI = 0 にする
    assert pins & 0b101 == 0b101                                      # 実行前: TCK = TDI = 1
    _, pins = await phy.command(OP_PINS, data=0)
    assert pins & 0b111 == 0b000
