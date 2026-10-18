"""usbtest_responder のテストベンチ。入出力は 9 ビット（last & data）のストリーム。"""

import random

import cocotb
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge

from tests.tb.common import start_clock_and_reset

ECHO, SINK = 0x00, 0x01


async def setup(dut):
    dut.in_valid.value = 0
    dut.in_data.value = 0
    dut.out_ready.value = 1
    await start_clock_and_reset(dut)


async def send_message(dut, message: bytes):
    for i, b in enumerate(message):
        dut.in_valid.value = 1
        dut.in_data.value = (int(i == len(message) - 1) << 8) | b
        while True:
            await ReadOnly()
            ok = int(dut.in_ready.value)
            await RisingEdge(dut.clk)
            if ok:
                break
    dut.in_valid.value = 0


def collector(dut):
    messages, current = [], bytearray()

    async def run():
        nonlocal current
        while True:
            await ReadOnly()
            if int(dut.out_valid.value) and int(dut.out_ready.value):
                word = int(dut.out_data.value)
                current.append(word & 0xFF)
                if word >> 8:
                    messages.append(bytes(current))
                    current = bytearray()
            await RisingEdge(dut.clk)

    cocotb.start_soon(run())
    return messages


@cocotb.test()
async def echoes_and_sinks_by_first_byte(dut):
    await setup(dut)
    out = collector(dut)
    await send_message(dut, bytes([ECHO, 1, 2, 3]))
    await send_message(dut, bytes([SINK]) + bytes(100))
    await send_message(dut, bytes([ECHO]))
    await send_message(dut, bytes([0x7F, 9]))          # 未知の種別は破棄
    await ClockCycles(dut.clk, 20)
    assert out == [bytes([ECHO, 1, 2, 3]), bytes([ECHO])]


@cocotb.test()
async def sink_never_blocks_even_without_reader(dut):
    await setup(dut)
    dut.out_ready.value = 0
    await send_message(dut, bytes([SINK]) + random.Random(1).randbytes(2000))
    await ReadOnly()
    assert int(dut.out_valid.value) == 0
