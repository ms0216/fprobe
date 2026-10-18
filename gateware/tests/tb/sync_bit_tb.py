"""sync_bit（INIT='1' でビルド）のテストベンチ。"""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge


@cocotb.test()
async def starts_at_init_and_follows_after_two_clocks(dut):
    dut.d.value = 0
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    await ReadOnly()
    assert int(dut.q.value) == 1                 # コンフィギュレーション直後は INIT
    await RisingEdge(dut.clk)                    # 1 段目が 0 を取り込む
    await ReadOnly()
    assert int(dut.q.value) == 1
    await RisingEdge(dut.clk)                    # 2 段目へ
    await ReadOnly()
    assert int(dut.q.value) == 0
    await RisingEdge(dut.clk)
    dut.d.value = 1
    await ClockCycles(dut.clk, 1)
    await ReadOnly()
    assert int(dut.q.value) == 0                 # 1 クロック後はまだ旧値
    await RisingEdge(dut.clk)
    await ReadOnly()
    assert int(dut.q.value) == 1
