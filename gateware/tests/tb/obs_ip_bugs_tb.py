"""target_ip/fprobe_obs の不具合の再現（台帳 docs/reports/bugs/rtl-obs-ip.md）。
どれも「直った状態」を期待する試験。今は失敗するので、ラッパ（tests/test_obs_ip_bugs.py）が strict の xfail で包む。"""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles


@cocotb.test()
async def idcode_bit0_zero_is_accepted_silently(dut):
    """BUG-RTL-OBSIP-01: IDCODE の bit0 が 0 でも組み立てられる。直った状態 = 組み立てで assert failure（この試験は
    何もしないので、組み立てが通る今は成功し、ラッパの「失敗するはず」が外れる）。"""
    cocotb.start_soon(Clock(dut.clk, 20, unit="ns").start())
    await ClockCycles(dut.clk, 2)


@cocotb.test()
async def undeclared_obs_option_leaves_tdo_undriven(dut):
    """BUG-RTL-OBSIP-03: shrike_demo_core の OBS が 0・1・2 以外だと tdo・tdo_oe をだれも駆動しない。"""
    cocotb.start_soon(Clock(dut.clk, 20, unit="ns").start())
    dut.nreset.value = 0
    for s in (dut.spi_sck, dut.spi_cs_n, dut.spi_mosi, dut.tck, dut.tms, dut.tdi):
        s.value = 1
    await ClockCycles(dut.clk, 4)
    dut.nreset.value = 1
    await ClockCycles(dut.clk, 4)
    assert str(dut.tdo.value) in ("0", "1"), f"tdo = {dut.tdo.value}"
    assert str(dut.tdo_oe.value) in ("0", "1"), f"tdo_oe = {dut.tdo_oe.value}"
