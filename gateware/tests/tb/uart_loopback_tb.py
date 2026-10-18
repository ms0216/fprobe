"""uart_loopback（tx → rx 直結）のテストベンチ。"""

import cocotb
from cocotb.triggers import ClockCycles

from tests.tb.common import StreamCollector, start_clock_and_reset, stream_send

DIV = 8


@cocotb.test()
async def loopback_transfers_bytes(dut):
    dut.divisor.value = DIV
    dut.in_valid.value = 0
    await start_clock_and_reset(dut)
    got = StreamCollector(dut, "out")
    await stream_send(dut, "in", b"fprobe")
    await ClockCycles(dut.clk, DIV * 12)
    assert bytes(got.items) == b"fprobe"
