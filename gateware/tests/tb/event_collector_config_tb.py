"""event_collector（CONFIG_EVENTS = true）: PHY の設定（cfg_word）が変わった周期と、記録を始めた周期（events_enable の
0 → 1）に種類 16（[119:48] = 設定、[47:0] = 受けた周期の時刻）を出す。ほかの源と同じ保持・優先順（いちばん後）・lost の
数え方で、基準モデル（event_collector_tb の Model）と毎周期突き合わせ、保存則も確かめる
（docs/superpowers/specs/2026-11-01-time-accuracy-design.md R7）。"""
import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, ReadOnly, RisingEdge

from tests.tb.event_collector_tb import Model, drive, rand_inputs, reset, run


@cocotb.test()
async def config_changes_and_enable_give_kind_16(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    for seed, density, toggle, change in ((1, 0.05, False, 0.01), (2, 0.3, True, 0.05), (3, 0.8, True, 0.2)):
        rng = random.Random(seed)
        model = Model(config_events=True)
        await reset(dut, model, rng)
        await run(dut, model, rng, 3000, density, toggle, f"seed {seed}", cfg_change=change)
        if density <= 0.3:                               # 込んだ入力では、いちばん後の優先順の件は出られず欠けに数える（モデルどおり）
            assert model.out["cfg"] > 0, "種類 16 が 1 件も出ていない"


@cocotb.test()
async def recording_start_reports_the_current_config(dut):
    """記録を始めた周期に、変わっていなくても今の設定を出す（記録だけで転送の時刻の式が計算できる）。"""
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    rng = random.Random(7)
    i = rand_inputs(rng, 0.0, 1000, 0, 0)
    i["cfg"] = 0x00_0000_1234_5678_9ABC
    for _ in range(5):                                   # 記録していない間に設定が決まっている
        await FallingEdge(dut.clk)
        drive(dut, i)
    got = []
    for n in range(4):
        i = dict(i, en=1, time=2000 + n)
        await FallingEdge(dut.clk)
        drive(dut, i)
        await RisingEdge(dut.clk)
        await ReadOnly()
        if dut.event_valid.value == 1:
            got.append(int(dut.event_data.value))
    assert got == [(0x10 << 120) | (0x1234_5678_9ABC << 48) | 2000], [hex(g) for g in got]
