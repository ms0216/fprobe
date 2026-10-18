"""stream_slice（WIDTH=8）のテストベンチ。乱数で両側を止めても、順序どおり欠落・重複なく流れること。"""

import random

import cocotb
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge

from tests.tb.common import start_clock_and_reset


@cocotb.test()
async def passes_everything_in_order_under_random_stalls(dut):
    dut.in_valid.value = 0
    dut.in_data.value = 0
    dut.out_ready.value = 0
    await start_clock_and_reset(dut)
    rng = random.Random(1)
    data = [rng.randrange(256) for _ in range(2000)]
    received = []
    sent = 0
    full_rate_cycles = 0
    for cycle in range(20000):
        offering = sent < len(data) and (cycle < 300 or rng.random() < 0.7)
        dut.in_valid.value = int(offering)
        dut.in_data.value = data[sent] if offering else 0
        dut.out_ready.value = int(cycle < 300 or rng.random() < 0.7)
        await ReadOnly()
        took = offering and int(dut.in_ready.value)
        gave = int(dut.out_valid.value) and int(dut.out_ready.value)
        if gave:
            received.append(int(dut.out_data.value))
        if cycle < 300 and took:
            full_rate_cycles += 1
        await RisingEdge(dut.clk)
        if took:
            sent += 1
        if len(received) == len(data):
            break
    assert received == data
    assert full_rate_cycles >= 295                    # 両側が止まらなければ 1 語 / 周期
