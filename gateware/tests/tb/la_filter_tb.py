"""la_filter（WIDTH=4, LEN_BITS=8）のテストベンチ: チャンネルごとのグリッチフィルタ（U21）。

d は非同期でよい（2 段で同期化）。フィルタする ch は、変化が cycles 周期続いたときだけ q に通す。
"""

import cocotb
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge

from tests.tb.common import start_clock_and_reset


class Trace:
    """毎周期の q を記録する。"""

    def __init__(self, dut):
        self.values: list[int] = []
        cocotb.start_soon(self._run(dut))

    async def _run(self, dut):
        while True:
            await RisingEdge(dut.clk)
            await ReadOnly()
            self.values.append(int(dut.q.value))

    def channel(self, ch: int) -> list[int]:
        return [v >> ch & 1 for v in self.values]


async def setup(dut, *, mask=0, cycles=0):
    dut.d.value = 0
    dut.mask.value = mask
    dut.cycles.value = cycles
    await start_clock_and_reset(dut)
    await ClockCycles(dut.clk, 5)


async def pulse(dut, ch: int, width: int):
    """ch に width 周期の 1 を入れる。"""
    await RisingEdge(dut.clk)
    dut.d.value = int(dut.d.value) | 1 << ch
    await ClockCycles(dut.clk, width)
    dut.d.value = int(dut.d.value) & ~(1 << ch)


def pulses(levels: list[int]) -> list[int]:
    """1 の続く長さの並び。"""
    out, run = [], 0
    for v in levels:
        if v:
            run += 1
        elif run:
            out.append(run)
            run = 0
    return out


@cocotb.test()
async def unfiltered_channels_pass_every_pulse(dut):
    await setup(dut, mask=0b0000, cycles=10)
    trace = Trace(dut)
    for w in (1, 2, 5):
        await pulse(dut, 0, w)
        await ClockCycles(dut.clk, 5)
    await ClockCycles(dut.clk, 5)
    assert pulses(trace.channel(0)) == [1, 2, 5]


@cocotb.test()
async def filtered_channel_drops_pulses_shorter_than_cycles(dut):
    await setup(dut, mask=0b0010, cycles=4)
    trace = Trace(dut)
    for w in (1, 3, 4, 7):
        await pulse(dut, 1, w)
        await ClockCycles(dut.clk, 8)
    await ClockCycles(dut.clk, 10)
    # 4 周期以上の脈だけが通り、幅は保たれる（両端とも cycles - 1 周期遅れる）
    assert pulses(trace.channel(1)) == [4, 7]


@cocotb.test()
async def filtered_edge_is_delayed_by_cycles_minus_one(dut):
    await setup(dut, mask=0b0100, cycles=6)
    trace = Trace(dut)
    await RisingEdge(dut.clk)
    dut.d.value = 0b0101                      # ch0（フィルタなし）と ch2（フィルタあり）を同時に上げる
    await ClockCycles(dut.clk, 20)
    first = {ch: trace.channel(ch).index(1) for ch in (0, 2)}
    assert first[2] - first[0] == 5


@cocotb.test()
async def glitch_inside_a_level_is_removed(dut):
    await setup(dut, mask=0b1000, cycles=3)
    trace = Trace(dut)
    await RisingEdge(dut.clk)
    dut.d.value = 0b1000
    await ClockCycles(dut.clk, 10)
    dut.d.value = 0                           # 2 周期の 0 のひげ
    await ClockCycles(dut.clk, 2)
    dut.d.value = 0b1000
    await ClockCycles(dut.clk, 10)
    dut.d.value = 0
    await ClockCycles(dut.clk, 10)
    assert pulses(trace.channel(3)) == [22]


async def check_no_filter(dut, cycles: int):
    await setup(dut, mask=0b0001, cycles=cycles)
    trace = Trace(dut)
    await pulse(dut, 0, 1)
    await ClockCycles(dut.clk, 6)
    assert pulses(trace.channel(0)) == [1]


@cocotb.test()
async def cycles_zero_means_no_filter(dut):
    await check_no_filter(dut, 0)


@cocotb.test()
async def cycles_one_means_no_filter(dut):
    await check_no_filter(dut, 1)


@cocotb.test()
async def an_unfiltered_change_reaches_q_after_la_filter_delay(dut):
    # 辺の直後に変わった入力が q に出るまでの辺の数（ケイパビリティ LA_DELAY の元。fprobe_gw.caps.LA_FILTER_DELAY）
    from cocotb.triggers import Timer

    from fprobe_gw.caps import LA_FILTER_DELAY
    await setup(dut)
    await RisingEdge(dut.clk)
    await Timer(1, "ns")
    dut.d.value = 0b0001
    edges = 0
    while True:
        await RisingEdge(dut.clk)
        edges += 1
        await ReadOnly()
        if int(dut.q.value) & 1:
            break
    assert edges == LA_FILTER_DELAY
