"""xilinx_mmcm（GHDL の模型の MMCME2_BASE）: lock が立ち、出力のクロックが出る。"""
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, Edge, RisingEdge, with_timeout
from cocotb.utils import get_sim_time


@cocotb.test()
async def locks_and_outputs_a_clock(dut):
    cocotb.start_soon(Clock(dut.clkin, 8, unit="ns").start())
    await with_timeout(RisingEdge(dut.lock), 50, "us")
    edges = 0
    for _ in range(10):
        await with_timeout(RisingEdge(dut.clkout), 100, "ns")
        edges += 1
    assert edges == 10
    await ClockCycles(dut.clkin, 2)
    assert dut.lock.value == 1


@cocotb.test()
async def the_second_output_is_an_integer_multiple_aligned_with_the_first(dut):
    """CAP_DIV = 5（OUT_DIV 10）: cap_clkout は clkout の 2 倍の周波数で、clkout の立ち上がりのたびに立ち上がる（頭がそろう）。"""
    cocotb.start_soon(Clock(dut.clkin, 8, unit="ns").start())
    if dut.lock.value != 1:                       # 前の試験で立ったまま（同じシミュレーションの続き）なら待たない
        await with_timeout(RisingEdge(dut.lock), 50, "us")
    sys_edges, cap_edges = [], []

    async def watch(sig, out):
        while True:
            await RisingEdge(sig)
            out.append(get_sim_time("ps"))

    cocotb.start_soon(watch(dut.clkout, sys_edges))
    cocotb.start_soon(watch(dut.cap_clkout, cap_edges))
    await ClockCycles(dut.clkin, 40)
    assert len(sys_edges) >= 20 and len(cap_edges) >= 40
    cap_set = set(cap_edges)
    assert all(t in cap_set for t in sys_edges[1:]), "clkout の立ち上がりに cap_clkout の立ち上がりが無い"
    inside = [sum(1 for c in cap_edges if a <= c < b) for a, b in zip(sys_edges, sys_edges[1:])]
    assert set(inside) == {2}, inside
