"""sim_smoke（STEP=3 でビルド）用テストベンチ。"""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, ReadOnly


async def _count_four_cycles(dut) -> int:
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.rst.value = 1
    await ClockCycles(dut.clk, 2)
    dut.rst.value = 0
    await ClockCycles(dut.clk, 4)
    await ReadOnly()
    return int(dut.q.value)


@cocotb.test()
async def counts_by_generic_step(dut):
    assert await _count_four_cycles(dut) == 12
