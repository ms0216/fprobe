"""mem_test_sys（メモリ試験の sys ⇄ mem の受け渡し）のテストベンチ。sys 100 MHz、mem 99.56 MHz 相当（周期 10.04 ns）。"""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge

from tests.models.mem_port import MemPortModel


async def setup(dut, **model):
    for name in ("start", "base", "count", "seed", "rst", "mem_rst", "mem_ready"):
        getattr(dut, name).value = 0
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    cocotb.start_soon(Clock(dut.mem_clk, 10040, unit="ps").start())
    mem = MemPortModel(dut, clock=dut.mem_clk, **model)
    dut.rst.value = dut.mem_rst.value = 1
    await ClockCycles(dut.clk, 5)
    dut.rst.value = dut.mem_rst.value = 0
    cocotb.start_soon(mem.run())
    return mem


async def run(dut, base, count, seed=0xC0FFEE):
    dut.base.value, dut.count.value, dut.seed.value = base, count, seed
    await RisingEdge(dut.clk)
    dut.start.value = 1
    await RisingEdge(dut.clk)
    dut.start.value = 0
    await ReadOnly()
    assert dut.busy.value == 1 and dut.done.value == 0                      # 前回の done は見えない
    for _ in range(50_000):
        await RisingEdge(dut.clk)
        await ReadOnly()
        if dut.done.value == 1:
            assert dut.busy.value == 0
            result = int(dut.errors.value), int(dut.write_cycles.value), int(dut.read_cycles.value)
            await RisingEdge(dut.clk)
            return result
    raise TimeoutError


@cocotb.test()
async def runs_across_clock_domains(dut):
    mem = await setup(dut)
    dut.mem_ready.value = 1
    await ClockCycles(dut.clk, 5)
    await ReadOnly()
    assert dut.ready.value == 1
    await RisingEdge(dut.clk)
    errors, wc, rc = await run(dut, base=7, count=300)
    assert errors == 0 and mem.writes == 300 and wc >= 300 and rc >= 300
    errors, _, _ = await run(dut, base=0, count=20)                        # 2 回目も前回の結果を引きずらない
    assert errors == 0 and mem.writes == 320


@cocotb.test()
async def errors_are_reported(dut):
    await setup(dut, stuck_bits=1)
    errors, _, _ = await run(dut, base=0, count=50, seed=0)
    assert errors > 0
