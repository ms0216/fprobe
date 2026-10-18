"""sync_fifo（WIDTH=8, DEPTH=16）のテストベンチ。"""

import random

import cocotb
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge

from tests.tb.common import StreamCollector, start_clock_and_reset, stream_send

DEPTH = 16


async def setup(dut):
    dut.in_valid.value = 0
    dut.in_data.value = 0
    dut.out_ready.value = 0
    await start_clock_and_reset(dut)


@cocotb.test()
async def empty_after_reset(dut):
    await setup(dut)
    await ReadOnly()
    assert int(dut.out_valid.value) == 0
    assert int(dut.in_ready.value) == 1
    assert int(dut.level.value) == 0


@cocotb.test()
async def holds_exactly_depth_items(dut):
    await setup(dut)
    await stream_send(dut, "in", range(DEPTH))
    await ReadOnly()
    assert int(dut.in_ready.value) == 0
    assert int(dut.level.value) == DEPTH
    await RisingEdge(dut.clk)
    got = StreamCollector(dut, "out")
    await ClockCycles(dut.clk, DEPTH + 4)
    assert got.items == list(range(DEPTH))
    await ReadOnly()
    assert int(dut.out_valid.value) == 0 and int(dut.level.value) == 0


@cocotb.test()
async def first_word_falls_through(dut):
    await setup(dut)
    await stream_send(dut, "in", [0x5A])
    for _ in range(3):   # 書き込み後、数クロック以内に先頭が出力に現れる
        await ReadOnly()
        if int(dut.out_valid.value):
            break
        await RisingEdge(dut.clk)
    assert int(dut.out_valid.value) == 1 and int(dut.out_data.value) == 0x5A


@cocotb.test()
async def random_traffic_preserves_order(dut):
    await setup(dut)
    rng = random.Random(1234)
    sent, received = [], []
    to_send = [rng.randrange(256) for _ in range(500)]

    async def producer():
        for value in to_send:
            while rng.random() < 0.3:          # ときどき休む
                dut.in_valid.value = 0
                await RisingEdge(dut.clk)
            dut.in_valid.value = 1
            dut.in_data.value = value
            while True:
                await ReadOnly()
                accepted = int(dut.in_ready.value)
                await RisingEdge(dut.clk)
                if accepted:
                    sent.append(value)
                    break
        dut.in_valid.value = 0

    async def consumer():
        while len(received) < len(to_send):
            dut.out_ready.value = 1 if rng.random() < 0.6 else 0
            await ReadOnly()
            if int(dut.out_valid.value) and int(dut.out_ready.value):
                received.append(int(dut.out_data.value))
            await RisingEdge(dut.clk)

    prod = cocotb.start_soon(producer())
    await consumer()
    await prod
    assert received == to_send == sent
