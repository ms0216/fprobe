"""PYNQ-Z2 の P0 の最小の回路: LED がおよそ DIV 周期ごとに反転する。PMOD の端子は口に無い（動かさない）。"""
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, ReadOnly


@cocotb.test()
async def led_toggles_every_div_cycles(dut):
    cocotb.start_soon(Clock(dut.sys_clk_in, 8, unit="ns").start())
    await ClockCycles(dut.sys_clk_in, 2)
    await ReadOnly()
    first = int(dut.led.value) & 1
    await ClockCycles(dut.sys_clk_in, 100)          # DIV = 100: 100 周期で反転
    await ReadOnly()
    assert int(dut.led.value) & 1 != first
    await ClockCycles(dut.sys_clk_in, 100)
    await ReadOnly()
    assert int(dut.led.value) & 1 == first


@cocotb.test()
async def no_pmod_ports(dut):
    names = {h._name.lower() for h in dut}
    assert not any(n.startswith(("ja", "jb", "pmod")) for n in names), names
