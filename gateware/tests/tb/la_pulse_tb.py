"""la_pulse（WIDTH=4, COUNT_BITS=16）のテストベンチ: パルス幅のトリガ（U21）。

mode 1: 選んだ ch の level の脈が終わったとき、幅（周期）が min〜max なら event を 1 周期。
mode 2: level が min 周期を超えて続いたら（脈の途中で）event を 1 周期（一定時間変化しない）。
probes は同期済み（la_filter の出力）。
"""

import cocotb
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge

from tests.tb.common import start_clock_and_reset

OFF, RANGE, TIMEOUT = 0, 1, 2
UNBOUNDED = 0xFFFF


class Events:
    def __init__(self, dut):
        self.cycles: list[int] = []
        self.now = 0
        cocotb.start_soon(self._run(dut))

    async def _run(self, dut):
        while True:
            await RisingEdge(dut.clk)
            await ReadOnly()
            self.now += 1
            if dut.event.value == 1:
                self.cycles.append(self.now)


async def setup(dut, *, mode, channel=1, level=1, min_cycles=0, max_cycles=UNBOUNDED):
    dut.probes.value = 0 if level else 0b1111          # 脈の外の値
    dut.mode.value = mode
    dut.channel.value = channel
    dut.level.value = level
    dut.min_cycles.value = min_cycles
    dut.max_cycles.value = max_cycles
    await start_clock_and_reset(dut)
    await ClockCycles(dut.clk, 3)
    return Events(dut)


async def pulse(dut, ch: int, width: int, level: int = 1, gap: int = 10):
    await RisingEdge(dut.clk)
    rest = int(dut.probes.value)
    dut.probes.value = (rest | 1 << ch) if level else (rest & ~(1 << ch))
    await ClockCycles(dut.clk, width)
    dut.probes.value = rest
    await ClockCycles(dut.clk, gap)


@cocotb.test()
async def range_fires_only_for_widths_inside(dut):
    events = await setup(dut, mode=RANGE, min_cycles=5, max_cycles=8)
    fired = []
    for w in (3, 4, 5, 6, 8, 9, 20):
        before = len(events.cycles)
        await pulse(dut, 1, w)
        fired.append(len(events.cycles) - before)
    assert fired == [0, 0, 1, 1, 1, 0, 0]


@cocotb.test()
async def longer_than_and_shorter_than(dut):
    events = await setup(dut, mode=RANGE, min_cycles=10, max_cycles=UNBOUNDED)
    fired = []
    for w in (9, 10, 300):
        before = len(events.cycles)
        await pulse(dut, 1, w)
        fired.append(len(events.cycles) - before)
    assert fired == [0, 1, 1]


@cocotb.test()
async def shorter_than(dut):
    events = await setup(dut, mode=RANGE, min_cycles=0, max_cycles=3)
    fired = []
    for w in (1, 3, 4):
        before = len(events.cycles)
        await pulse(dut, 1, w)
        fired.append(len(events.cycles) - before)
    assert fired == [1, 1, 0]


@cocotb.test()
async def low_pulses_on_the_selected_channel_only(dut):
    events = await setup(dut, mode=RANGE, channel=2, level=0, min_cycles=4, max_cycles=6)
    await pulse(dut, 1, 5, level=0)            # 別の ch
    assert events.cycles == []
    await pulse(dut, 2, 5, level=0)
    assert len(events.cycles) == 1


@cocotb.test()
async def event_comes_at_the_end_of_the_pulse(dut):
    events = await setup(dut, mode=RANGE, min_cycles=2, max_cycles=UNBOUNDED)
    await RisingEdge(dut.clk)
    dut.probes.value = 0b0010
    await ClockCycles(dut.clk, 50)
    assert events.cycles == []                 # まだ終わっていない
    dut.probes.value = 0
    await ClockCycles(dut.clk, 5)
    assert len(events.cycles) == 1


@cocotb.test()
async def timeout_fires_while_the_level_lasts(dut):
    events = await setup(dut, mode=TIMEOUT, min_cycles=20)
    await pulse(dut, 1, 15)
    assert events.cycles == []
    await RisingEdge(dut.clk)
    start = events.now
    dut.probes.value = 0b0010
    await ClockCycles(dut.clk, 60)
    assert len(events.cycles) == 1             # 1 回だけ（続いている間に繰り返さない）
    assert 20 <= events.cycles[0] - start <= 24
    dut.probes.value = 0
    await ClockCycles(dut.clk, 5)
    assert len(events.cycles) == 1


@cocotb.test()
async def timeout_counts_a_level_already_held_when_enabled(dut):
    dut.probes.value = 0b0010
    events = await setup(dut, mode=OFF, min_cycles=10)
    dut.probes.value = 0b0010
    await ClockCycles(dut.clk, 30)
    dut.mode.value = TIMEOUT
    await ClockCycles(dut.clk, 30)
    assert len(events.cycles) == 1


@cocotb.test()
async def range_ignores_a_pulse_already_running_when_enabled(dut):
    events = await setup(dut, mode=OFF, min_cycles=0, max_cycles=UNBOUNDED)
    dut.probes.value = 0b0010
    await ClockCycles(dut.clk, 10)
    dut.mode.value = RANGE
    await ClockCycles(dut.clk, 10)
    dut.probes.value = 0                       # 始まりを見ていない脈は幅が分からない
    await ClockCycles(dut.clk, 10)
    assert events.cycles == []
    await pulse(dut, 1, 5)
    assert len(events.cycles) == 1


@cocotb.test()
async def off_never_fires(dut):
    events = await setup(dut, mode=OFF, min_cycles=0, max_cycles=UNBOUNDED)
    for w in (1, 5, 50):
        await pulse(dut, 1, w)
    assert events.cycles == []
