"""cdc_value（WIDTH=32）のテストベンチ。送り 60 MHz 相当・受け 100 MHz 相当（と、その逆・同じクロック）。

約束（rtl/common/cdc_value.vhd）: 届く値は、どれも送り側が src_load した値そのもの（ビットが混ざらない）で、送った順。
最後に src_load した値は必ず届く。前の値が届く前の src_load は最新の 1 つだけが後で送られる（握手）。"""

import os
import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge

from tests.tb.common import PulseCounter

SAME_CLOCK = os.environ.get("CDC_SAME_CLOCK") == "1"


def clocks(dut, src_ns=16, dst_ns=10):
    if SAME_CLOCK:
        cocotb.start_soon(Clock(dut.src_clk, 10, unit="ns").start())
        cocotb.start_soon(Clock(dut.dst_clk, 10, unit="ns").start())   # 同じ時刻の辺（同じクロックの模型）
    else:
        cocotb.start_soon(Clock(dut.src_clk, src_ns, unit="ns").start())
        cocotb.start_soon(Clock(dut.dst_clk, dst_ns, unit="ns").start())


class Received:
    """受け側の dst_changed の周期の dst_value を順に集める。"""

    def __init__(self, dut):
        self.dut, self.values = dut, []
        cocotb.start_soon(self._run())

    async def _run(self):
        while True:
            await RisingEdge(self.dut.dst_clk)
            await ReadOnly()
            if self.dut.dst_changed.value == 1:
                self.values.append(int(self.dut.dst_value.value))


@cocotb.test()
async def values_arrive_with_one_pulse_each(dut):
    dut.src_load.value = 0
    dut.src_value.value = 0
    clocks(dut)
    pulses = PulseCounter(dut, dut.dst_changed, clock=dut.dst_clk)
    for value in (115_200, 9_600, 0xDEADBEEF):
        await RisingEdge(dut.src_clk)
        dut.src_value.value = value
        dut.src_load.value = 1
        await RisingEdge(dut.src_clk)
        dut.src_load.value = 0
        dut.src_value.value = 0                       # 保持されるので後で変えてよい
        await ClockCycles(dut.dst_clk, 10)
        await ReadOnly()
        assert int(dut.dst_value.value) == value
    assert pulses.count == 3


async def rapid_loads(dut, src_ns, dst_ns):
    dut.src_load.value = 0
    dut.src_value.value = 0
    clocks(dut, src_ns, dst_ns)
    got = Received(dut)
    await ClockCycles(dut.src_clk, 5)
    rnd = random.Random(src_ns * 100 + dst_ns)
    sent = []
    for _ in range(600):
        await RisingEdge(dut.src_clk)
        if rnd.random() < 0.6:                        # 受け渡しの速さより速く、不規則に送る
            v = rnd.randrange(1 << 32)
            sent.append(v)
            dut.src_value.value = v
            dut.src_load.value = 1
        else:
            dut.src_load.value = 0
            dut.src_value.value = rnd.randrange(1 << 32)   # 送らない周期の値は届かない
    await RisingEdge(dut.src_clk)
    dut.src_load.value = 0
    await ClockCycles(dut.dst_clk, 40)
    await ReadOnly()
    assert got.values, "何も届かない"
    assert got.values[-1] == sent[-1], "最後に送った値が届かない"
    index = {v: i for i, v in enumerate(sent)}
    order = [index.get(v) for v in got.values]
    assert None not in order, "送っていない値（混ざった値）が届いた"
    assert order == sorted(order) and len(set(order)) == len(order), "順番が崩れた"
    if not SAME_CLOCK:
        assert len(got.values) < len(sent), "間の値を飛ばしていない（試験の前提: 受け渡しより速く送った）"
    assert int(dut.src_ready.value) == 1


@cocotb.test()
async def rapid_loads_deliver_whole_values_in_order_and_always_the_last_one(dut):
    await rapid_loads(dut, 16, 10)


@cocotb.test()
async def rapid_loads_to_a_slower_receiver_also_keep_the_values_whole(dut):
    await rapid_loads(dut, 10, 37)


@cocotb.test()
async def a_steady_load_streams_the_latest_value(dut):
    # src_load = '1' のまま: 返事が戻るたびに、その周期の値が送られる。届く値は送り側の値の並びの中の値で、増えるだけ
    dut.src_load.value = 1
    dut.src_value.value = 0
    clocks(dut)
    await ClockCycles(dut.src_clk, 20)                # 前の試験の残り（送りかけの値）が届き終わってから見る
    got = Received(dut)
    for n in range(401):
        await RisingEdge(dut.src_clk)
        dut.src_value.value = n
    await ClockCycles(dut.dst_clk, 40)
    await ReadOnly()
    assert got.values[-1] == 400
    assert got.values == sorted(got.values)
    values = sorted(set(got.values))
    gaps = [b - a for a, b in zip(values, values[1:])]
    if SAME_CLOCK:
        assert max(gaps) == 1                          # 同じクロック: 毎周期
    else:
        assert max(gaps) <= 8, max(gaps)              # 握手の一巡（受け側の同期化 + 返事の同期化）ごと
