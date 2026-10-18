"""ulpi_probe_core のテストベンチ（PHY モデル + UART 線の復号）。

generics: CLK_HZ=60_000_000, BAUD=6_000_000 → UART 分周比 10。
"""

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, FallingEdge, ReadOnly

from tests.models.uart import decode_uart_line, uart_bits
from tests.models.ulpi_phy import REG_SCRATCH, UlpiPhy

DIV = 10
REPORT = b"ulpi vid=0424 pid=0006 scratch=A5/5A fc=41\r\n"


async def start(dut, *, throttle=0.3):
    dut.uart_rx.value = 1
    phy = UlpiPhy(dut, dut.clk, rng=random.Random(7), nxt_throttle=throttle)
    cocotb.start_soon(Clock(dut.clk, 16, unit="ns").start())
    dut.rst.value = 1
    await ClockCycles(dut.clk, 3)
    dut.rst.value = 0
    levels = []

    async def record():
        while True:
            await FallingEdge(dut.clk)
            await ReadOnly()
            levels.append(int(dut.uart_tx.value))

    cocotb.start_soon(record())
    return phy, levels


@cocotb.test()
async def reports_phy_registers_after_startup(dut):
    phy, levels = await start(dut)
    await ClockCycles(dut.clk, DIV * 10 * (len(REPORT) + 2) + 2000)
    assert decode_uart_line(levels, DIV) == REPORT
    assert phy.regs[REG_SCRATCH] == 0x5A


@cocotb.test()
async def question_mark_repeats_the_report(dut):
    phy, levels = await start(dut)
    await ClockCycles(dut.clk, DIV * 10 * (len(REPORT) + 2) + 2000)
    for level in uart_bits(ord("?")):
        dut.uart_rx.value = level
        await ClockCycles(dut.clk, DIV)
    await ClockCycles(dut.clk, DIV * 10 * (len(REPORT) + 2) + 2000)
    assert decode_uart_line(levels, DIV) == REPORT * 2
