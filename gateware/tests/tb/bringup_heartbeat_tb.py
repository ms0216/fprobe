"""bringup_core の心拍（SYS_CLK_HZ=10_000 → 5000 クロックごとに反転）。"""

import cocotb
from cocotb.triggers import ClockCycles

from tests.tb.common import LevelRecorder, start_clock_and_reset


@cocotb.test()
async def heartbeat_toggles_every_half_second(dut):
    dut.rx.value = 1
    await start_clock_and_reset(dut)
    rec = LevelRecorder(dut, dut.heartbeat)
    await ClockCycles(dut.clk, 16_000)
    lv = rec.levels
    toggles = [i for i in range(1, len(lv)) if lv[i] != lv[i - 1]]
    assert len(toggles) >= 3, toggles
    assert [b - a for a, b in zip(toggles, toggles[1:])] == [5000] * (len(toggles) - 1)
