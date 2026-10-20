"""axi_mem_port: mem_port（128 ビットの拍）⇄ AXI3 の 64 ビットのマスタ（PYNQ の S_AXI_HP0）。
相手は AXI3 のスレーブのメモリの模型（READY・VALID をランダムに遅らせる）。"""
import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge

from tests.tb.axi3_slave_mem import Axi3SlaveMem

BASE = 0x1E00_0000


async def setup(dut, base=BASE, seed=1):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.rst.value = 1
    dut.base.value = base
    dut.cmd_valid.value = 0
    dut.cmd_write.value = 0
    dut.cmd_addr.value = 0
    dut.cmd_data.value = 0
    mem = Axi3SlaveMem(dut, dut.clk, seed=seed)
    got = []

    async def collect():
        while True:
            await RisingEdge(dut.clk)
            if dut.rst.value == 0 and dut.rd_valid.value == 1:
                got.append(int(dut.rd_data.value))
    cocotb.start_soon(collect())
    await ClockCycles(dut.clk, 3)
    dut.rst.value = 0
    await ClockCycles(dut.clk, 2)
    return mem, got


async def command(dut, write, addr, data=0, timeout=1000):
    dut.cmd_valid.value = 1
    dut.cmd_write.value = int(write)
    dut.cmd_addr.value = addr
    dut.cmd_data.value = data
    for _ in range(timeout):
        await RisingEdge(dut.clk)
        if dut.cmd_ready.value == 1:
            dut.cmd_valid.value = 0
            return
    raise TimeoutError("cmd_ready が立たない")


async def wait_reads(dut, got, n, timeout=5000):
    for _ in range(timeout):
        if len(got) >= n:
            return
        await RisingEdge(dut.clk)
    raise TimeoutError(f"読みの結果が {len(got)}/{n}")


@cocotb.test()
async def writes_then_reads_return_the_data_in_order(dut):
    mem, got = await setup(dut)
    rng = random.Random(2)
    values = {a: rng.getrandbits(128) for a in rng.sample(range(1 << 12), 40)}
    for a, v in values.items():
        await command(dut, True, a, v)
    for a in values:
        await command(dut, False, a)
    await wait_reads(dut, got, len(values))
    assert got == list(values.values())
    # 物理番地 = base + 拍の番号 × 16、下位 64 ビットが先の語
    a, v = next(iter(values.items()))
    assert mem.mem[(BASE + a * 16) >> 3] == v & (2**64 - 1)
    assert mem.mem[((BASE + a * 16) >> 3) + 1] == v >> 64


@cocotb.test()
async def reads_are_pipelined_and_never_stall(dut):
    mem, got = await setup(dut, seed=3)
    for a in range(64):
        mem.mem[(BASE >> 3) + 2 * a] = a
        mem.mem[(BASE >> 3) + 2 * a + 1] = 1000 + a
    start = cocotb.utils.get_sim_time("ns")
    for a in range(64):
        await command(dut, False, a)
    await wait_reads(dut, got, 64)
    assert got == [(1000 + a) << 64 | a for a in range(64)]
    # 出しっぱなしにできるので、1 つずつ待つより十分速い（模型の遅れ込みで 1 拍あたり 10 周期未満）
    assert cocotb.utils.get_sim_time("ns") - start < 64 * 10 * 10


@cocotb.test()
async def write_then_read_of_the_same_beat_returns_the_new_value(dut):
    mem, got = await setup(dut, seed=4)
    for k in range(20):
        await command(dut, True, 7, k)
        await command(dut, False, 7)
    await wait_reads(dut, got, 20)
    assert got == list(range(20))


@cocotb.test()
async def nothing_is_issued_while_base_is_zero(dut):
    mem, got = await setup(dut, base=0)
    assert dut.ready.value == 0
    dut.cmd_valid.value = 1
    dut.cmd_write.value = 1
    await ClockCycles(dut.clk, 20)
    assert dut.cmd_ready.value == 0 and mem.requests == 0
    dut.cmd_valid.value = 0
    dut.base.value = BASE
    await ClockCycles(dut.clk, 2)
    assert dut.ready.value == 1


@cocotb.test()
async def error_responses_are_counted(dut):
    mem, got = await setup(dut, seed=5)
    mem.error_addresses = {BASE + 3 * 16}
    await command(dut, True, 3, 0xAB)
    await command(dut, False, 3)
    await command(dut, True, 4, 0xCD)
    await command(dut, False, 4)
    await wait_reads(dut, got, 2)
    assert got[1] == 0xCD
    await ClockCycles(dut.clk, 20)
    assert int(dut.errors.value) == 2           # 書きの B と読みの R の 2 つ


@cocotb.test()
async def a_read_error_on_any_word_of_the_beat_is_counted(dut):
    mem, got = await setup(dut, seed=6)
    mem.first_beat_error_addresses = {BASE + 9 * 16}
    await command(dut, False, 9)
    await wait_reads(dut, got, 1)
    await ClockCycles(dut.clk, 5)
    assert int(dut.errors.value) == 1
