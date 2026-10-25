"""外部メモリ経路の不具合の再現（検出専用。docs/reports/bugs/rtl-deep-mem.md）。
各テストは、現状の RTL では失敗する（test_deepmem_bugs.py が strict xfail で包む）。"""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge

from tests.models.mem_port import MemPortModel
from tests.tb.axi3_slave_mem import Axi3SlaveMem
from tests.tb.common import start_clock_and_reset

CAP = 1024
EV_BEATS = CAP // 16
LA_BEATS = CAP - EV_BEATS


# ------------------------------------------------------------------ mem_reader（mem_ring_harness）
async def ring_setup(dut):
    for name in ("use_reader", "w_start", "w_stop", "post_beats", "in_valid", "in_data", "in_kind", "ev_enable",
                 "in_trig", "in_slot", "r_start", "region_base", "region_size", "first", "count"):
        getattr(dut, name).value = 0
    dut.out_ready.value = 1
    mem = MemPortModel(dut, size_beats=CAP, stall=0.3)
    await start_clock_and_reset(dut)
    cocotb.start_soon(mem.run())
    return mem


async def r_pulse(dut):
    dut.r_start.value = 1
    await RisingEdge(dut.clk)
    dut.r_start.value = 0


@cocotb.test()
async def bug_mem_1_reader_restart_takes_stale_beats(dut):
    """BUG-RTL-MEM-1: 読み出し中に新しい start を出すと、前の読みの未着の結果が新しい読みの出力に混ざる。"""
    mem = await ring_setup(dut)
    for i in range(CAP):
        mem.memory[i] = 0x1000 + i
    dut.use_reader.value = 1
    dut.region_base.value, dut.region_size.value = 0, LA_BEATS
    dut.first.value, dut.count.value = 0, 200
    dut.out_ready.value = 0                      # 受け皿を満たして発行済みの読みを残す
    await r_pulse(dut)
    await ClockCycles(dut.clk, 30)
    dut.first.value, dut.count.value = 500, 20
    dut.out_ready.value = 1
    await r_pulse(dut)
    out = []
    for _ in range(3000):
        await ReadOnly()
        if dut.out_valid.value == 1 and dut.out_ready.value == 1:
            out.append(int(dut.out_data.value))
        await RisingEdge(dut.clk)
        if len(out) >= 20:
            break
    assert out[:20] == [0x1000 + 500 + i for i in range(20)], [hex(x) for x in out[:20]]


@cocotb.test()
async def bug_mem_2_reader_first_beyond_region_leaves_region(dut):
    """BUG-RTL-MEM-2: first が領域の大きさ以上だと折り返さず、領域の外（イベントのリング等）を読む。"""
    mem = await ring_setup(dut)
    seen = []

    async def watch():
        while True:
            await ReadOnly()
            if dut.cmd_valid.value == 1 and dut.cmd_ready.value == 1 and dut.cmd_write.value == 0:
                seen.append(int(dut.cmd_addr.value))
            await RisingEdge(dut.clk)
    cocotb.start_soon(watch())
    dut.use_reader.value = 1
    dut.region_base.value, dut.region_size.value = 0, LA_BEATS
    dut.first.value, dut.count.value = LA_BEATS + 5, 4          # 領域の外の位置
    await r_pulse(dut)
    await ClockCycles(dut.clk, 300)
    assert seen and all(0 <= a < LA_BEATS for a in seen), seen


