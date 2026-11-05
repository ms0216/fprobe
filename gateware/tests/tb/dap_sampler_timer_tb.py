"""dap_sampler_timer（DAP サンプラの周期の時計とゆらぎ）のテストベンチ。

約束（rtl/debug/dap_sampler_timer.vhd）: run = 1 で period > 0 なら due は period 周期ごとに 1 周期。run = 0・period = 0 では
出さない。jitter = k > 0 なら間隔は period − 2^(k−1) 〜 period + 2^(k−1) − 1 で、平均は period。"""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, FallingEdge, ReadOnly, RisingEdge


async def setup(dut, period, jitter=0, run=1):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.rst.value = 1
    dut.run.value = 0
    dut.period.value = period
    dut.jitter.value = jitter
    await ClockCycles(dut.clk, 3)
    dut.rst.value = 0
    dut.run.value = run
    await RisingEdge(dut.clk)


async def due_cycles(dut, cycles):
    """cycles 周期の間に due = 1 だった周期の番号。"""
    seen = []
    for n in range(cycles):
        await RisingEdge(dut.clk)
        await ReadOnly()
        if dut.due.value == 1:
            seen.append(n)
    return seen


@cocotb.test()
async def period_without_jitter(dut):
    await setup(dut, period=10)
    seen = await due_cycles(dut, 200)
    gaps = [b - a for a, b in zip(seen, seen[1:])]
    assert len(seen) >= 18 and set(gaps) == {10}, seen


@cocotb.test()
async def stopped_gives_no_due(dut):
    await setup(dut, period=10, run=0)
    assert await due_cycles(dut, 100) == []
    await FallingEdge(dut.clk)
    dut.run.value = 1
    dut.period.value = 0
    assert await due_cycles(dut, 100) == []


@cocotb.test()
async def jitter_keeps_the_mean(dut):
    await setup(dut, period=100, jitter=4)
    seen = await due_cycles(dut, 100 * 400)
    gaps = [b - a for a, b in zip(seen, seen[1:])][2:]   # 最初の数回はゆらぎの段が埋まる前
    assert min(gaps) >= 100 - 8 and max(gaps) <= 100 + 7, (min(gaps), max(gaps))
    assert len(set(gaps)) > 4, set(gaps)
    mean = sum(gaps) / len(gaps)
    assert abs(mean - 100) < 1.5, mean
