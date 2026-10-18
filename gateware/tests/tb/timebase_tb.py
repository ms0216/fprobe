"""timebase のテストベンチ。"""

import cocotb
from cocotb.handle import Force, Release
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge

from tests.tb.common import start_clock_and_reset


@cocotb.test()
async def counts_every_cycle_from_zero_after_reset(dut):
    dut.hold.value = 0
    await start_clock_and_reset(dut)
    await ReadOnly()
    first = int(dut.count.value)
    await ClockCycles(dut.clk, 100)
    await ReadOnly()
    assert int(dut.count.value) - first == 100
    await RisingEdge(dut.clk)
    dut.rst.value = 1
    await RisingEdge(dut.clk)
    dut.rst.value = 0
    await ReadOnly()
    assert int(dut.count.value) == 0


@cocotb.test()
async def hold_keeps_the_high_half_of_that_cycle(dut):
    # 上位が変わるのを待てないので、強制した値で確かめる（hold の周期の上位が残り、以後は変わらない）
    dut.hold.value = 0
    await start_clock_and_reset(dut)
    dut.value.value = Force((0x1234_5678 << 32) | 0xFFFF_FFF0)   # 16 周期後に上位が繰り上がる
    await RisingEdge(dut.clk)
    dut.value.value = Release()
    dut.hold.value = 1
    await RisingEdge(dut.clk)
    dut.hold.value = 0
    await ClockCycles(dut.clk, 20)
    await ClockCycles(dut.clk, 5)
    await ReadOnly()
    assert int(dut.held_hi.value) == 0x1234_5678
    assert int(dut.count.value) >> 32 == 0x1234_5679       # カウンタの上位は進んだ
