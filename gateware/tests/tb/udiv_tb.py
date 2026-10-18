"""udiv（WIDTH=32）のテストベンチ。"""

import random

import cocotb
from cocotb.triggers import ReadOnly, RisingEdge

from tests.tb.common import start_clock_and_reset


async def divide(dut, a, b):
    dut.dividend.value = a
    dut.divisor.value = b
    dut.start.value = 1
    await RisingEdge(dut.clk)
    dut.start.value = 0
    for _ in range(40):
        await RisingEdge(dut.clk)
        await ReadOnly()
        if int(dut.done.value):
            result = int(dut.quotient.value), int(dut.remainder.value)
            await RisingEdge(dut.clk)
            return result
    raise AssertionError("done が来ない")


@cocotb.test()
async def divides_correctly(dut):
    dut.start.value = 0
    await start_clock_and_reset(dut)
    rng = random.Random(1)
    cases = [(100_285_714, 115_200), (0, 7), (7, 7), (0xFFFFFFFF, 1), (0xFFFFFFFF, 0xFFFFFFFF), (5, 9)]
    cases += [(rng.getrandbits(32), rng.getrandbits(rng.randrange(1, 33)) or 1) for _ in range(30)]
    for a, b in cases:
        assert await divide(dut, a, b) == (a // b, a % b), (a, b)
