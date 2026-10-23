"""obs_snap_core（極小の FPGA 向けの標本型の観測の回路。JTAG の 4 本だけ、TCK を clk で標本化する 1 クロック版）。
USER の DR = 32 ビット: 0xA7(31:24) | 前の Capture-DR からの値の変化の回数(23:16。255 で止める) | Capture-DR の値(15:0)。"""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, FallingEdge

from tests.tb.obs_soft_core_tb import Jtag, bits_of, word


async def setup(dut, probes=0x1234):
    cocotb.start_soon(Clock(dut.clk, 20, unit="ns").start())
    dut.rst.value = 1
    dut.probes.value = probes
    jtag = Jtag(dut)
    await ClockCycles(dut.clk, 5)
    dut.rst.value = 0
    await jtag.reset()
    await jtag.shift(bits_of(0x2, 4), ir=True)               # USER
    return jtag


async def snap(jtag) -> int:
    return word(await jtag.shift([0] * 32, ir=False), 0)


@cocotb.test()
async def a_scan_reads_the_value_and_the_changes_since_the_last(dut):
    jtag = await setup(dut)
    first = await snap(jtag)
    assert first >> 24 == 0xA7 and first & 0xFFFF == 0x1234
    for v in (1, 2, 3):
        await FallingEdge(dut.clk)
        dut.probes.value = v
        await ClockCycles(dut.clk, 5)
    s = await snap(jtag)
    assert s & 0xFFFF == 3 and s >> 16 & 0xFF == 3
    assert await snap(jtag) >> 16 & 0xFF == 0                # 変化なし


@cocotb.test()
async def the_change_count_stops_at_255(dut):
    jtag = await setup(dut)
    await snap(jtag)
    for v in range(1, 300):
        await FallingEdge(dut.clk)
        dut.probes.value = v
    await ClockCycles(dut.clk, 5)
    assert (await snap(jtag)) >> 16 & 0xFF == 255


@cocotb.test()
async def the_idcode_is_still_there(dut):
    jtag = await setup(dut)
    await jtag.reset()                                       # Test-Logic-Reset → IDCODE
    assert word(await jtag.shift([0] * 32, ir=False), 0) == 0x0F0B5001


@cocotb.test()
async def a_change_on_the_capture_cycle_is_counted_in_that_snapshot(dut):
    # Capture-DR の周期の変化は、その標本の値に入るので回数もその標本に数える（次の標本に持ち越すと、値が変わらない
    # 次の標本に回数 1 が出て、PC が無い切れ目を出していた）
    jtag = await setup(dut, probes=0x0001)
    await snap(jtag)
    armed = [True]

    async def change_on_capture():
        while armed[0]:
            await FallingEdge(dut.clk)
            if dut.rise.value == 1 and dut.u_sel.value == 1 and dut.u_capture.value == 1:
                dut.probes.value = 0x0002
                armed[0] = False
    cocotb.start_soon(change_on_capture())
    first = await snap(jtag)
    second = await snap(jtag)
    assert first & 0xFFFF == 0x0002 and first >> 16 & 0xFF == 1
    assert second >> 16 & 0xFF == 0
