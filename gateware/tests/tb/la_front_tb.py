"""la_front（ロジアナの前段）のテストベンチ。トリガ判定の状態が捕捉をまたいで残らないことなど。"""

import cocotb
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge

from tests.tb.common import start_clock_and_reset


async def setup(dut):
    for name in ("run", "rate", "trig_mask", "trig_value", "trig_rise", "trig_fall", "ext_trigger", "probes",
                 "pw_enable", "pw_event"):
        getattr(dut, name).value = 0
    dut.arm.value = 1
    await start_clock_and_reset(dut)


async def hits_while_running(dut, cycles):
    """run = 1 にして cycles 周期のあいだ sample_hit が立った回数と、サンプルの数を返す。"""
    dut.run.value = 1
    hits = samples = 0
    for _ in range(cycles):
        await RisingEdge(dut.clk)
        await ReadOnly()
        samples += int(dut.sample_valid.value)
        hits += int(dut.sample_hit.value)
    await RisingEdge(dut.clk)                               # ReadOnly の相を抜ける
    return hits, samples


@cocotb.test()
async def rising_edge_is_not_judged_against_the_previous_capture(dut):
    """前の捕捉の最後のサンプルが 0 で、今回の開始時に既に 1 のとき、開始直後のサンプルを立ち上がりとしない
    （この捕捉の中で変化していない）。"""
    await setup(dut)
    dut.trig_rise.value = 1
    dut.probes.value = 0
    hits, samples = await hits_while_running(dut, 20)
    assert samples > 10 and hits == 0                      # 0 のまま: 立ち上がりなし
    dut.run.value = 0
    await ClockCycles(dut.clk, 10)
    dut.probes.value = 1                                    # 止めている間に 1 になる
    await ClockCycles(dut.clk, 10)
    hits, samples = await hits_while_running(dut, 20)
    assert samples > 10 and hits == 0                      # 1 のまま: 立ち上がりなし


@cocotb.test()
async def rising_edge_inside_a_capture_hits_once(dut):
    await setup(dut)
    dut.trig_rise.value = 1
    dut.probes.value = 0
    dut.run.value = 1
    await ClockCycles(dut.clk, 10)
    dut.probes.value = 1
    hits, _ = await hits_while_running(dut, 20)
    assert hits == 1


@cocotb.test()
async def falling_edge_is_not_judged_against_the_previous_capture(dut):
    await setup(dut)
    dut.trig_fall.value = 1
    dut.probes.value = 1
    hits, _ = await hits_while_running(dut, 20)
    assert hits == 0
    dut.run.value = 0
    await ClockCycles(dut.clk, 10)
    dut.probes.value = 0
    await ClockCycles(dut.clk, 10)
    hits, samples = await hits_while_running(dut, 20)
    assert samples > 10 and hits == 0
