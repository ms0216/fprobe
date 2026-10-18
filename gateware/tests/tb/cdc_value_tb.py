"""cdc_value（WIDTH=32）のテストベンチ。送り 60 MHz 相当・受け 100 MHz 相当。"""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge

from tests.tb.common import PulseCounter


@cocotb.test()
async def values_arrive_with_one_pulse_each(dut):
    dut.src_load.value = 0
    dut.src_value.value = 0
    cocotb.start_soon(Clock(dut.src_clk, 16, unit="ns").start())
    cocotb.start_soon(Clock(dut.dst_clk, 10, unit="ns").start())
    pulses = PulseCounter(dut, dut.dst_changed, clock=dut.dst_clk)
    for value in (115_200, 9_600, 0xDEADBEEF):
        await RisingEdge(dut.src_clk)
        dut.src_value.value = value
        dut.src_load.value = 1
        await RisingEdge(dut.src_clk)
        dut.src_load.value = 0
        dut.src_value.value = 0                       # 保持されるので後で変えてよい
        await ClockCycles(dut.dst_clk, 10)
        await ReadOnly()
        assert int(dut.dst_value.value) == value
    assert pulses.count == 3
