"""reset_fanout（COUNT=5）のテストベンチ: 全ビットが rst を 2 周期遅らせたものになる（1 段目 = 塊ごとの写し、
2 段目 = 配線を短くする複製の段。2026-10-29）。"""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ReadOnly, RisingEdge, Timer

COUNT = 5
ALL = (1 << COUNT) - 1


@cocotb.test()
async def every_copy_follows_rst_two_clocks_late(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    bits = [1, 1, 0, 0, 0, 1, 0, 1, 1, 0, 0]
    seen = []
    for bit in bits:
        dut.rst.value = bit
        await RisingEdge(dut.clk)           # このエッジで bit を取り込む
        await ReadOnly()
        seen.append(int(dut.rst_out.value))
        await Timer(1, unit="ns")           # ReadOnly の後は書けないので時間を進める
    # エッジ k の直後の出力は、エッジ k − 1 で取り込んだ値（2 周期遅れ）。全ビットが同じ
    assert seen[1:] == [ALL if b else 0 for b in bits[:-1]], seen
