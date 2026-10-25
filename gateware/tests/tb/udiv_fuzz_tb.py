"""udiv のランダム試験（基準モデルは Python の //・%）: 端の値（0・1・2^31・2^32-1）、除数 0（商 全 1・余り = 被除数）、
実行中の start は無視、計算中のリセットの後の最初の計算、連続の計算。"""

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge

from tests.tb.common import CLK_PERIOD_NS

MASK = 0xFFFFFFFF
EDGES = (0, 1, 2, 3, 0x7FFFFFFF, 0x80000000, 0x80000001, MASK - 1, MASK, 100_000_000, 115_200)


def expected(a, b):
    return (MASK, a) if b == 0 else (a // b, a % b)


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


async def setup(dut):
    dut.start.value = 0
    dut.dividend.value = 0
    dut.divisor.value = 1
    cocotb.start_soon(Clock(dut.clk, CLK_PERIOD_NS, unit="ns").start())
    dut.rst.value = 1
    await ClockCycles(dut.clk, 3)
    dut.rst.value = 0
    await RisingEdge(dut.clk)


@cocotb.test()
async def random_and_edge_operands(dut):
    await setup(dut)
    rng = random.Random(8)
    cases = [(a, b) for a in EDGES for b in EDGES]
    cases += [(rng.getrandbits(32), rng.getrandbits(rng.randrange(0, 33))) for _ in range(300)]
    for a, b in cases:
        assert await divide(dut, a, b) == expected(a, b), (a, b)


@cocotb.test()
async def start_while_busy_is_ignored_and_reset_aborts(dut):
    await setup(dut)
    rng = random.Random(9)
    for _ in range(20):
        a, b = rng.getrandbits(32), rng.getrandbits(20) or 1
        dut.dividend.value, dut.divisor.value = a, b
        dut.start.value = 1
        await RisingEdge(dut.clk)
        dut.start.value = 0
        await ClockCycles(dut.clk, rng.randrange(1, 30))
        if rng.random() < 0.5:
            # 実行中の start（別の被除数）は無視される
            dut.dividend.value, dut.divisor.value = rng.getrandbits(32), 3
            dut.start.value = 1
            await RisingEdge(dut.clk)
            dut.start.value = 0
            await ClockCycles(dut.clk, 40)
            await ReadOnly()
            assert int(dut.quotient.value) == a // b
            await RisingEdge(dut.clk)
        else:
            # 計算中のリセット: busy が落ち、次の計算は前の途中の値を引きずらない
            dut.rst.value = 1
            await RisingEdge(dut.clk)
            await RisingEdge(dut.clk)
            dut.rst.value = 0
            await ReadOnly()
            assert int(dut.busy.value) == 0
            await RisingEdge(dut.clk)
            a2, b2 = rng.getrandbits(32), rng.getrandbits(32) or 1
            assert await divide(dut, a2, b2) == expected(a2, b2)
            await ClockCycles(dut.clk, 40)
            await ReadOnly()
            assert int(dut.done.value) == 0
            await RisingEdge(dut.clk)


@cocotb.test()
async def back_to_back_results_are_independent(dut):
    await setup(dut)
    rng = random.Random(10)
    for _ in range(10):
        a, b = rng.getrandbits(32), rng.getrandbits(16) or 1
        c, d = rng.getrandbits(32), rng.getrandbits(16) or 1
        dut.dividend.value, dut.divisor.value = a, b
        dut.start.value = 1
        await RisingEdge(dut.clk)
        dut.start.value = 0
        for _ in range(40):
            await ReadOnly()
            if int(dut.done.value):
                break
            await RisingEdge(dut.clk)
        else:
            raise AssertionError("done が来ない")
        # 結果を読んだ直後に次の計算
        assert (int(dut.quotient.value), int(dut.remainder.value)) == expected(a, b)
        await RisingEdge(dut.clk)
        dut.dividend.value, dut.divisor.value = c, d
        dut.start.value = 1
        await RisingEdge(dut.clk)
        dut.start.value = 0
        for _ in range(40):
            await ReadOnly()
            if int(dut.done.value):
                break
            await RisingEdge(dut.clk)
        else:
            raise AssertionError("2 つ目の done が来ない")
        assert (int(dut.quotient.value), int(dut.remainder.value)) == expected(c, d)
        await RisingEdge(dut.clk)
