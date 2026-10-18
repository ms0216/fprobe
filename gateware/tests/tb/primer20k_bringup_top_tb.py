"""Primer 20K の bringup トップ（rPLL はシミュレーションモデル: clkout = clk27）。

sys_clk の周期で時間を数える。UART 分周比は SYS_CLK_HZ / 115200 を丸めた値。
"""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, ReadOnly

from fprobe_gw.boards.primer20k import PRIMER20K
from tests.models.uart import decode_uart_line, rx_divisor, tx_divisor, uart_bits
from tests.tb.common import LevelRecorder

SYS_CLK_HZ = PRIMER20K.sync_pll.fout_hz
DIV = rx_divisor(SYS_CLK_HZ, 115_200)
TX_DIV = tx_divisor(SYS_CLK_HZ, 115_200)


@cocotb.test()
async def echo_through_board_top(dut):
    cocotb.start_soon(Clock(dut.clk27, 10, unit="ns").start())
    dut.uart_rx.value = 1
    await ClockCycles(dut.clk27, 20)              # PLL ロック + リセット解除
    line = LevelRecorder(dut, dut.uart_tx, clock=dut.clk27)
    for byte in b"ok":
        for level in uart_bits(byte):
            dut.uart_rx.value = level
            await ClockCycles(dut.clk27, DIV)
    await ClockCycles(dut.clk27, DIV * 12)
    assert decode_uart_line(line.levels, TX_DIV) == b"ok"


@cocotb.test()
async def leds_are_active_low(dut):
    cocotb.start_soon(Clock(dut.clk27, 10, unit="ns").start())
    await ClockCycles(dut.clk27, 20)
    await ReadOnly()
    led = int(dut.led.value)
    assert (led >> 1) & 1 == 0          # LED1（設定完了表示）は点灯 = '0'
    assert (led >> 3) & 0b111 == 0b111  # 取りこぼし無し（LED3）・未使用 LED4..5 は消灯 = '1'

