"""swo_receiver 単体のテストベンチ（clk 100 MHz）。B10: 受信の開始（run 0→1）で下流の古いバイトを捨てさせる flush。"""

import cocotb
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge, Timer

from tests.models.uart import uart_bits
from tests.tb.common import start_clock_and_reset

DIVISOR = 50                  # 100 MHz / 2 Mbaud
FLUSH = 32                    # ハーネスの FLUSH_CYCLES


async def send(dut, data: bytes):
    for byte in data:
        for bit in uart_bits(byte):
            dut.swo.value = bit
            await Timer(DIVISOR * 10, unit="ns")


async def collect(dut, out: list):
    while True:
        await RisingEdge(dut.clk)
        await ReadOnly()
        if dut.out_valid.value == 1 and dut.out_ready.value == 1:
            out.append(int(dut.out_data.value))


async def setup(dut):
    dut.swo.value = 1
    dut.run.value = 0
    dut.out_ready.value = 1
    dut.divisor.value = DIVISOR
    dut.manchester.value = 0
    await start_clock_and_reset(dut)


@cocotb.test()
async def start_raises_flush_for_the_guard_window_and_holds_output(dut):
    await setup(dut)
    await ReadOnly()
    assert int(dut.flush.value) == 0
    await RisingEdge(dut.clk)
    dut.run.value = 1
    high = 0
    for _ in range(FLUSH + 8):
        await RisingEdge(dut.clk)
        await ReadOnly()
        if dut.flush.value == 1:
            high += 1
            assert int(dut.out_valid.value) == 0           # 窓の間は下流に出さない
    assert high == FLUSH
    assert int(dut.flush.value) == 0


@cocotb.test()
async def bytes_after_the_window_pass_and_restart_flushes_again(dut):
    await setup(dut)
    received = []
    cocotb.start_soon(collect(dut, received))
    dut.run.value = 1
    await ClockCycles(dut.clk, FLUSH + 2)
    await send(dut, b"\x11\x22\x33")
    await ClockCycles(dut.clk, 20)
    assert bytes(received) == b"\x11\x22\x33"
    dut.run.value = 0
    await ClockCycles(dut.clk, 5)
    await ReadOnly()
    assert int(dut.flush.value) == 0                        # 止めるときは捨てない（残りは読める）
    await RisingEdge(dut.clk)
    dut.run.value = 1
    await RisingEdge(dut.clk)
    await ReadOnly()
    assert int(dut.flush.value) == 1                        # 開始のたびに捨てさせる


@cocotb.test()
async def downstream_stall_in_the_window_is_not_an_overrun(dut):
    await setup(dut)
    dut.out_ready.value = 0
    dut.run.value = 1
    await ClockCycles(dut.clk, FLUSH + 2)
    await ReadOnly()
    assert int(dut.overrun.value) == 0
