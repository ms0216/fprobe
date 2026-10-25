"""reset_fanout（COUNT=5）のテストベンチ: 全ビットが rst を 1 周期遅らせたものになる。"""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ReadOnly, RisingEdge, Timer

COUNT = 5
ALL = (1 << COUNT) - 1


@cocotb.test()
async def every_copy_follows_rst_one_clock_late(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    for bit in [1, 1, 0, 0, 0, 1, 0, 1, 1, 0]:
        dut.rst.value = bit
        await RisingEdge(dut.clk)           # このエッジで bit を取り込む
        await ReadOnly()                    # エッジの直後: 全ビットが取り込んだ値になっている（エッジの前は 1 周期前の値）
        assert int(dut.rst_out.value) == (ALL if bit else 0)
        await Timer(1, unit="ns")           # ReadOnly の後は書けないので時間を進める
