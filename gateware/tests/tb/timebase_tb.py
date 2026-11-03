"""timebase のテストベンチ。"""

import cocotb
from cocotb.handle import Force, Release
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge, Timer

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
    dut.upper.value = Force(0x1234_5678)                  # 16 周期後に上位が繰り上がる（上位・下位に分けて数える）
    dut.low.value = Force(0xFFFF_FFF0)
    await RisingEdge(dut.clk)
    dut.upper.value = Release()
    dut.low.value = Release()
    dut.hold.value = 1
    await RisingEdge(dut.clk)
    dut.hold.value = 0
    await ClockCycles(dut.clk, 20)
    await ClockCycles(dut.clk, 5)
    await ReadOnly()
    assert int(dut.held_hi.value) == 0x1234_5678
    assert int(dut.count.value) >> 32 == 0x1234_5679       # カウンタの上位は進んだ


@cocotb.test()
async def the_count_crosses_the_32_bit_boundary_one_by_one(dut):
    """下位 32 ビットと上位 32 ビットに分けた数え方（2026-10-29、sys を上げるため）でも、下位の桁あふれの前後で値が毎周期
    ちょうど 1 ずつ増える（上位の繰り上がりが 1 周期遅れない）。下位を境目の直前に置いて確かめる。"""
    from cocotb.triggers import Timer
    dut.hold.value = 0
    await start_clock_and_reset(dut)
    dut.low.value = Force(0xFFFFFFF8)                 # 境目の 8 周期前（上位は 0）
    await RisingEdge(dut.clk)
    dut.low.value = Release()
    seen = []
    for _ in range(16):
        await RisingEdge(dut.clk)
        await ReadOnly()
        seen.append(int(dut.count.value))
        await Timer(1, unit="ns")
    assert all(b - a == 1 for a, b in zip(seen, seen[1:])), [hex(x) for x in seen]
    assert seen[0] < 1 << 32 <= seen[-1]
