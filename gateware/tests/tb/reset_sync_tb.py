"""reset_sync（STAGES=3）のテストベンチ。"""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge, Timer

STAGES = 3


@cocotb.test()
async def held_in_reset_after_configuration(dut):
    dut.arst_in.value = 1
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    await ClockCycles(dut.clk, 5)
    await ReadOnly()
    assert int(dut.rst.value) == 1


@cocotb.test()
async def releases_exactly_stages_clocks_after_request_drops(dut):
    dut.arst_in.value = 1
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    await ClockCycles(dut.clk, 4)
    dut.arst_in.value = 0
    for _ in range(STAGES - 1):
        await RisingEdge(dut.clk)
        await ReadOnly()
        assert int(dut.rst.value) == 1
    await RisingEdge(dut.clk)
    await ReadOnly()
    assert int(dut.rst.value) == 0


@cocotb.test()
async def asserts_immediately_without_a_clock_edge(dut):
    dut.arst_in.value = 0
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    await ClockCycles(dut.clk, STAGES + 2)
    await ReadOnly()
    assert int(dut.rst.value) == 0
    await RisingEdge(dut.clk)
    await Timer(3, unit="ns")                    # クロックエッジの合間で要求
    dut.arst_in.value = 1
    await Timer(1, unit="ns")
    await ReadOnly()
    assert int(dut.rst.value) == 1
