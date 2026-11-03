"""xilinx_oddr（ODDR の包み、GHDL では tests/rtl/sim_xilinx_prims.vhd の模型）のテストベンチ。"""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, ReadOnly, RisingEdge, Timer


@cocotb.test()
async def first_half_is_d1_and_second_half_is_d2_one_cycle_later(dut):
    """立ち上がり w で取った d1・d2 を、w から clk の前半（High）に d1、後半（Low）に d2 として出す。"""
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    pairs = [(0x00, 0xFF), (0xA5, 0xA5), (0x3C, 0xC3), (0xFF, 0x00)]
    dut.d1.value, dut.d2.value = 0, 0
    await RisingEdge(dut.clk)
    for d1, d2 in pairs:
        dut.d1.value, dut.d2.value = d1, d2
        await RisingEdge(dut.clk)                      # この辺で取る
        await Timer(1, "ns")
        await ReadOnly()
        assert int(dut.q.value) == d1, (hex(d1), hex(d2))
        await FallingEdge(dut.clk)
        await Timer(1, "ns")
        await ReadOnly()
        assert int(dut.q.value) == d2, (hex(d1), hex(d2))
        await Timer(1, "ns")                           # ReadOnly を抜けてから次の値を入れる
