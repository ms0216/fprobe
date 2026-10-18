"""async_fifo（WIDTH=8, DEPTH=16）のテストベンチ。書き込み 60 MHz 相当・読み出し 100 MHz 相当。"""

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge


async def setup(dut):
    dut.in_valid.value = 0
    dut.in_data.value = 0
    dut.out_ready.value = 0
    dut.out_flush.value = 0
    cocotb.start_soon(Clock(dut.in_clk, 16, unit="ns").start())
    cocotb.start_soon(Clock(dut.out_clk, 10, unit="ns").start())
    dut.in_rst.value = 1
    dut.out_rst.value = 1
    await ClockCycles(dut.in_clk, 4)
    dut.in_rst.value = 0
    dut.out_rst.value = 0
    await ClockCycles(dut.in_clk, 4)


async def produce(dut, values, rng, idle=0.3):
    for v in values:
        while rng.random() < idle:
            dut.in_valid.value = 0
            await RisingEdge(dut.in_clk)
        dut.in_valid.value = 1
        dut.in_data.value = v
        while True:
            await ReadOnly()
            ok = int(dut.in_ready.value)
            await RisingEdge(dut.in_clk)
            if ok:
                break
    dut.in_valid.value = 0


async def consume(dut, count, rng, ready=0.5):
    got = []
    while len(got) < count:
        dut.out_ready.value = int(rng.random() < ready)
        await ReadOnly()
        if int(dut.out_valid.value) and int(dut.out_ready.value):
            got.append(int(dut.out_data.value))
        await RisingEdge(dut.out_clk)
    dut.out_ready.value = 0
    return got


@cocotb.test()
async def random_traffic_crosses_clock_domains_in_order(dut):
    await setup(dut)
    rng = random.Random(42)
    values = [rng.randrange(256) for _ in range(1000)]
    producer = cocotb.start_soon(produce(dut, values, rng))
    got = await consume(dut, len(values), random.Random(7))
    await producer
    assert got == values


@cocotb.test()
async def fills_up_and_drains_without_loss(dut):
    await setup(dut)
    rng = random.Random(1)
    # 容量は DEPTH + 1（出力レジスタに 1 語先読みする）
    await produce(dut, list(range(17)), rng, idle=0)
    await ClockCycles(dut.in_clk, 4)
    await ReadOnly()
    assert int(dut.in_ready.value) == 0                 # 満杯
    await RisingEdge(dut.in_clk)
    got = await consume(dut, 17, rng, ready=1.0)
    assert got == list(range(17))
    await ClockCycles(dut.out_clk, 10)
    await ReadOnly()
    assert int(dut.out_valid.value) == 0                # 空


@cocotb.test()
async def out_flush_discards_what_was_written_before(dut):
    """B10: 読み出し側の out_flush で、それまでに書かれた語を捨てる（満杯でも）。後から書いた語は順に届く。"""
    await setup(dut)
    rng = random.Random(3)
    await produce(dut, list(range(17)), rng, idle=0)      # 満杯（出力レジスタの 1 語を含む）
    await ClockCycles(dut.out_clk, 6)                     # 書き込みポインタが読み出し側に届く
    await RisingEdge(dut.out_clk)
    dut.out_flush.value = 1
    await RisingEdge(dut.out_clk)
    dut.out_flush.value = 0
    await ReadOnly()
    assert int(dut.out_valid.value) == 0
    await ClockCycles(dut.out_clk, 10)
    await ReadOnly()
    assert int(dut.out_valid.value) == 0                  # 捨てた語は出てこない
    await RisingEdge(dut.in_clk)
    await ReadOnly()
    assert int(dut.in_ready.value) == 1                   # 空きが戻った
    await RisingEdge(dut.in_clk)
    fresh = [0xA0 + i for i in range(20)]
    producer = cocotb.start_soon(produce(dut, fresh, rng))
    got = await consume(dut, len(fresh), random.Random(5))
    await producer
    assert got == fresh
