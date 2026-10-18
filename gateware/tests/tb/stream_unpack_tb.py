"""stream_unpack（BYTES = 4）のテストベンチ。待ちを入れても語がバイト列として順に出ること。"""

import random

import cocotb
from cocotb.triggers import ReadOnly, RisingEdge

from tests.tb.common import start_clock_and_reset


@cocotb.test()
async def words_become_little_endian_bytes(dut):
    rng = random.Random(1)
    dut.in_valid.value = 0
    dut.out_ready.value = 0
    await start_clock_and_reset(dut)
    words = [rng.getrandbits(32) for _ in range(50)]
    expected = b"".join(w.to_bytes(4, "little") for w in words)
    got, pending = bytearray(), list(words)
    for _ in range(2000):
        dut.in_valid.value = int(bool(pending) and rng.random() > 0.3)
        dut.in_data.value = pending[0] if pending else 0
        dut.out_ready.value = int(rng.random() > 0.3)
        await ReadOnly()
        sent = dut.in_valid.value == 1 and dut.in_ready.value == 1
        if dut.out_valid.value == 1 and dut.out_ready.value == 1:
            got.append(int(dut.out_data.value))
        await RisingEdge(dut.clk)
        if sent:
            pending.pop(0)
        if len(got) == len(expected):
            break
    assert bytes(got) == expected
