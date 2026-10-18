"""mem_test（メモリ試験）のテストベンチ。相手は mem_port の振る舞いモデル。"""

import cocotb
from cocotb.triggers import ReadOnly, RisingEdge

from tests.models.mem_port import MemPortModel
from tests.tb.common import start_clock_and_reset


async def setup(dut, **model):
    dut.start.value = 0
    dut.base.value = dut.count.value = dut.seed.value = 0
    mem = MemPortModel(dut, **model)
    await start_clock_and_reset(dut)
    cocotb.start_soon(mem.run())
    return mem


async def run_test(dut, base, count, seed=0x12345678, timeout=100_000):
    dut.base.value, dut.count.value, dut.seed.value = base, count, seed
    dut.start.value = 1
    await RisingEdge(dut.clk)
    dut.start.value = 0
    for _ in range(timeout):
        await RisingEdge(dut.clk)
        await ReadOnly()
        if dut.done.value == 1:
            result = (int(dut.errors.value), int(dut.first_error.value), int(dut.write_cycles.value),
                      int(dut.read_cycles.value))
            await RisingEdge(dut.clk)
            return result
    raise TimeoutError


@cocotb.test()
async def good_memory_has_no_errors(dut):
    mem = await setup(dut)
    errors, _, wcycles, rcycles = await run_test(dut, base=100, count=500)
    assert errors == 0 and mem.writes == 500 and mem.reads == 500
    assert 500 <= wcycles < 1000 and 500 <= rcycles < 1100                 # 待ちの分だけ多い
    errors, _, _, _ = await run_test(dut, base=0, count=1, seed=0)          # 1 拍
    assert errors == 0


@cocotb.test()
async def stuck_bit_is_found(dut):
    await setup(dut, stuck_bits=1 << 77)
    errors, first, _, _ = await run_test(dut, base=10, count=64)
    assert 0 < errors <= 64 and first >= 10


@cocotb.test()
async def wrapping_address_lines_are_found(dut):
    # 容量 256 拍のメモリに 0..511 を書くと、後半が前半を上書きする → 前半の読み値が合わない
    await setup(dut, size_beats=256)
    errors, first, _, _ = await run_test(dut, base=0, count=512)
    assert errors >= 256 and first == 0
