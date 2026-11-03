"""2026-11-03 のレビュー（DAP の線の速さ）: JTAG の TransferBlock を half_period 1（50 MHz）・TDO の遅れ + 取り込みの遅れ・WAIT ありで回し、
先出し（dap_link の 2 段・jtag_phy の cmd_cond・owed）で値・処理数・応答がずれないかを見る。"""

import random
import struct

import cocotb

from tests.models.jtag_target import Bypass, JtagChain, JtagDp
from tests.tb.common import start_clock_and_reset
from tests.tb.control_port import ControlPort
from tests.tb.dap_processor_tb import AP_DRW_R, AP_DRW_W, AP_TAR_W, DP_CTRL_R, OK, Dap
from tests.tb.dap_jtag_tb import JTAG_CONFIGURE, JTAG_SEQUENCE, sequence


async def setup(dut, hz, sample_delay, tdo_delay_ns, **dp_args):
    dut.packet_size.value = 512
    dut.nreset_ext.value = "Z"
    dut.target_swdio.value = "Z"
    dut.jtag_sample_delay.value = sample_delay
    dp = JtagDp(**dp_args)
    chain = JtagChain(dut, [dp, Bypass()], tck="swclk", tms="swdio", tdi="tdi", tdo="tdo", tdo_delay_ns=tdo_delay_ns)
    port = ControlPort(dut, request="in", response="out", rng=random.Random(5), gap=0.2, stall=0.2)
    await start_clock_and_reset(dut)
    cocotb.start_soon(chain.run())
    dap = Dap(dut, port)
    assert await dap.command(bytes([0x02, 2])) == bytes([0x02, 2])
    assert await dap.command(bytes([0x11]) + struct.pack("<I", hz)) == bytes([0x11, 0])
    assert await dap.command(bytes([JTAG_CONFIGURE, 2, 4, 5])) == bytes([JTAG_CONFIGURE, 0])
    reset = bytes([JTAG_SEQUENCE, 2]) + sequence(6, tms=1, tdi=0x3F) + sequence(1, tms=0)
    assert await dap.command(reset) == bytes([JTAG_SEQUENCE, 0])
    return dap, dp


async def run_case(dut, hz, sd, delay, wait_after_ap):
    dap, dp = await setup(dut, hz, sd, delay, wait_after_ap=wait_after_ap)
    await dap.mem_setup()
    words = [random.Random(hz + sd).getrandbits(32) for _ in range(24)]
    await dap.transfer((AP_TAR_W, 0x20000000))
    assert (await dap.block(AP_DRW_W, len(words), words))[:2] == (24, OK), (hz, sd, delay)
    assert [dp.regs.memory[0x20000000 + 4 * i] for i in range(24)] == words, (hz, sd, delay)
    await dap.transfer((AP_TAR_W, 0x20000000))
    assert await dap.block(AP_DRW_R, 24) == (24, OK, words), (hz, sd, delay)
    # DP の読み（RDBUFF を続ける DP の読み）を TransferBlock で
    count, resp, data = await dap.block(DP_CTRL_R, 6)
    assert (count, resp) == (6, OK) and len(data) == 6, (hz, sd, delay, count, resp)


@cocotb.test()
async def jtag_block_50mhz_no_delay_with_wait(dut):
    await run_case(dut, 50_000_000, 0, 0, 2)


@cocotb.test()
async def jtag_block_50mhz_late_tdo_with_wait(dut):
    # 往復 55 ns、hp 1（T = 10 ns）: d·T ∈ (RT − T, RT + T) = (45, 65) → d = 5 or 6
    await run_case(dut, 50_000_000, 6, 55, 2)


@cocotb.test()
async def jtag_block_25mhz_late_tdo_with_wait(dut):
    # hp 2: d·T ∈ (RT − 3T, RT + T) = (25, 65) → d = 3..6
    await run_case(dut, 25_000_000, 6, 55, 1)
