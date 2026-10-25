"""外部メモリ経路の不具合の再現（検出専用。docs/reports/bugs/rtl-deep-mem.md）。
各テストは、現状の RTL では失敗する（test_deepmem_bugs.py が strict xfail で包む）。"""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge

from tests.models.mem_port import MemPortModel
from tests.tb.axi3_slave_mem import Axi3SlaveMem
from tests.tb.common import start_clock_and_reset

# ------------------------------------------------------------------ axi_mem_port
async def axi_setup(dut, base=0x1E00_0000, stall=0.3):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.rst.value = 1
    dut.base.value = base
    dut.cmd_valid.value = 0
    dut.cmd_write.value = 0
    dut.cmd_addr.value = 0
    dut.cmd_data.value = 0
    mem = Axi3SlaveMem(dut, dut.clk, seed=3, stall=stall)
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


async def axi_cmd(dut, write, addr, data=0, timeout=2000):
    dut.cmd_valid.value = 1
    dut.cmd_write.value = int(write)
    dut.cmd_addr.value = addr
    dut.cmd_data.value = data
    for _ in range(timeout):
        await RisingEdge(dut.clk)
        if dut.cmd_ready.value == 1:
            dut.cmd_valid.value = 0
            return
    raise TimeoutError("cmd_ready")


@cocotb.test()
async def bug_axi_1_reset_with_outstanding_transactions(dut):
    """BUG-RTL-AXI-1: 途中で rst を入れると、AXI の相手に残った書き・読みの応答で数が負になり、
    また AW だけ・W の一部だけ渡した書きが相手に残って次の書きの W と組になる。"""
    base = 0x1E00_0000
    mem, got = await axi_setup(dut, base=base, stall=0.5)
    for delay in range(0, 6):
        await axi_cmd(dut, True, 100 + delay, 0xAAAA0000 + delay)
        await ClockCycles(dut.clk, delay)
        dut.rst.value = 1
        await ClockCycles(dut.clk, 2)
        dut.rst.value = 0
        await ClockCycles(dut.clk, 40)                      # 相手の応答が出きる
        n = len(got)
        await axi_cmd(dut, True, 200 + delay, 0xBBBB0000 + delay)
        await axi_cmd(dut, False, 200 + delay)
        for _ in range(500):
            if len(got) > n:
                break
            await RisingEdge(dut.clk)
        assert len(got) > n and got[-1] == 0xBBBB0000 + delay, (delay, got[n:])


@cocotb.test()
async def bug_axi_2_unaligned_base_crosses_4kb(dut):
    """BUG-RTL-AXI-2: base が 16 の倍数でないと、16 バイトのバーストが 4 KB の境界をまたぐ（AXI 違反）。
    base の下位 4 ビットは捨てるか、準備ができない（ready = 0）べき。"""
    mem, got = await axi_setup(dut, base=0x1E00_0FF8, stall=0.0)
    seen = []

    async def watch():
        while True:
            await ReadOnly()
            if dut.m_axi_awvalid.value == 1 and dut.m_axi_awready.value == 1:
                seen.append(int(dut.m_axi_awaddr.value))
            await RisingEdge(dut.clk)
    cocotb.start_soon(watch())
    if dut.ready.value == 1:
        await axi_cmd(dut, True, 0, 1)
        await ClockCycles(dut.clk, 40)
        assert seen and all((a & 0xFFF) + 16 <= 0x1000 for a in seen), [hex(a) for a in seen]
