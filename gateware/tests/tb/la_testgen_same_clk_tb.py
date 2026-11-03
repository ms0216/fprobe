"""la_testgen を clk と cap_clk に同じクロックで動かす（fprobe_pynq_deep の形）。cdc_value が同一クロックでも値を正しく届けることの確認。
クロックは 10 ns（sys 100 MHz 相当）。周期・high は clk の周期の数。"""

import cocotb
from cocotb.triggers import ClockCycles, Edge, ReadOnly, RisingEdge
from cocotb.utils import get_sim_time

from tests.tb.common import start_clock_and_reset
from tests.tb.la_testgen_tb import check_pattern

CLK_PS = 10000


async def setup(dut):
    for name in ("enable", "edge_sel", "period", "high"):
        getattr(dut, name).value = 0
    await start_clock_and_reset(dut, period_ns=10)


async def configure(dut, *, enable, period, high, edge_sel=0):
    await RisingEdge(dut.clk)
    dut.enable.value, dut.period.value, dut.high.value, dut.edge_sel.value = int(enable), period, high, int(edge_sel)
    await ClockCycles(dut.clk, 40)


async def sample(dut, n):
    out = []
    for _ in range(n):
        await RisingEdge(dut.clk)
        await ReadOnly()
        out.append(int(dut.out_q.value))
    return out


@cocotb.test()
async def square_wave_has_the_period_and_the_high_time(dut):
    await setup(dut)
    for period, high in ((2, 1), (3, 1), (3, 2), (7, 3), (30, 15), (300, 299)):
        await configure(dut, enable=True, period=period, high=high)
        await ReadOnly()
        assert dut.out_oe.value == 1
        check_pattern(await sample(dut, 4 * period + 2), period, high)


@cocotb.test()
async def out_of_range_values_are_clamped_the_same_way(dut):
    await setup(dut)
    for period, high, want_period, want_high in ((0, 0, 2, 1), (5, 5, 5, 4), (5000, 1, 4095, 1)):
        await configure(dut, enable=True, period=period, high=high)
        check_pattern(await sample(dut, 4 * want_period + 2), want_period, want_high)


@cocotb.test()
async def disabled_output_is_low_and_not_driven(dut):
    await setup(dut)
    await configure(dut, enable=True, period=3, high=1)
    await configure(dut, enable=False, period=3, high=1)
    await ReadOnly()
    assert dut.out_oe.value == 0 and dut.out_q.value == 0
    await RisingEdge(dut.clk)


@cocotb.test()
async def falling_edge_launch_is_half_a_cycle_later(dut):
    await setup(dut)

    async def residues():
        seen = set()

        async def watch():
            while True:
                await Edge(dut.out_q)
                seen.add(get_sim_time("ps") % CLK_PS)
        task = cocotb.start_soon(watch())
        await ClockCycles(dut.clk, 70)
        task.cancel()
        return seen
    await configure(dut, enable=True, period=7, high=3, edge_sel=0)
    rising = await residues()
    await configure(dut, enable=True, period=7, high=3, edge_sel=1)
    falling = await residues()
    assert len(rising) == 1 and len(falling) == 1, (rising, falling)
    assert (next(iter(falling)) - next(iter(rising))) % CLK_PS == CLK_PS // 2


@cocotb.test()
async def reset_stops_driving_the_pin_and_the_pattern_comes_back_after_it(dut):
    await setup(dut)
    await configure(dut, enable=True, period=7, high=3)
    await ReadOnly()
    assert dut.out_oe.value == 1
    await RisingEdge(dut.clk)
    dut.rst.value = 1
    await ClockCycles(dut.clk, 4)
    await ReadOnly()
    assert dut.out_oe.value == 0 and dut.out_q.value == 0
    await RisingEdge(dut.clk)
    dut.rst.value = 0
    await ClockCycles(dut.clk, 8)
    await ReadOnly()
    assert dut.out_oe.value == 1
    check_pattern(await sample(dut, 30), 7, 3)
