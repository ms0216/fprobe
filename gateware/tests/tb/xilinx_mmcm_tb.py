"""xilinx_mmcm（GHDL の模型の MMCME2_BASE）: lock が立ち、出力のクロックが出る。"""
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge, with_timeout


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
