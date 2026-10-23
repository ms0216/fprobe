"""la_capture（WIDTH=4, DEPTH=32）のテストベンチ。プローブには 1 周期ごとに増えるカウンタを入れる。"""

import cocotb
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge

from tests.tb.common import start_clock_and_reset

DEPTH = 32


async def counter(dut):
    value = 0
    while True:
        await RisingEdge(dut.clk)
        value = (value + 1) & 0xF
        dut.probes.value = value


async def setup(dut):
    for name in ("run", "rate", "pretrigger", "trig_mask", "trig_value", "trig_rise", "trig_fall",
                 "ext_trigger", "rd_addr", "probes", "pw_enable", "pw_event"):
        getattr(dut, name).value = 0
    dut.time.value = 0
    await start_clock_and_reset(dut)
    cocotb.start_soon(counter(dut))

    async def clock_time():
        while True:
            await RisingEdge(dut.clk)
            dut.time.value = int(dut.time.value) + 1
    cocotb.start_soon(clock_time())


async def capture(dut, *, rate=0, pretrigger=8, mask=0, value=0, rise=0, fall=0, timeout=5000):
    dut.rate.value = rate
    dut.pretrigger.value = pretrigger
    dut.trig_mask.value = mask
    dut.trig_value.value = value
    dut.trig_rise.value = rise
    dut.trig_fall.value = fall
    dut.run.value = 1
    for _ in range(timeout):
        await RisingEdge(dut.clk)
        await ReadOnly()
        if int(dut.done.value):
            break
    else:
        raise AssertionError("捕捉が終わらない")
    await RisingEdge(dut.clk)
    dut.run.value = 0
    start = int(dut.start_index.value)
    samples = []
    for i in range(DEPTH):
        dut.rd_addr.value = (start + i) % DEPTH
        await RisingEdge(dut.clk)
        await RisingEdge(dut.clk)
        await ReadOnly()
        samples.append(int(dut.rd_data.value))
        await RisingEdge(dut.clk)
    trigger = (int(dut.trigger_index.value) - start) % DEPTH
    return samples, trigger


@cocotb.test()
async def value_trigger_with_pretrigger(dut):
    await setup(dut)
    samples, trigger = await capture(dut, pretrigger=8, mask=0xF, value=0xA)
    assert trigger == 8 and samples[8] == 0xA
    assert all((b - a) % 16 == 1 for a, b in zip(samples, samples[1:]))     # 抜けも重複もない


@cocotb.test()
async def edge_trigger_and_slow_rate(dut):
    await setup(dut)
    samples, trigger = await capture(dut, rate=2, pretrigger=4, rise=0b1000)  # 3 周期ごと、bit3 の立ち上がり
    assert trigger == 4
    assert samples[3] & 0b1000 == 0 and samples[4] & 0b1000
    assert all((b - a) % 16 == 3 for a, b in zip(samples, samples[1:]))


@cocotb.test()
async def immediate_trigger_and_abort(dut):
    await setup(dut)
    samples, trigger = await capture(dut, pretrigger=0)                      # 条件なし: すぐトリガ
    assert trigger == 0 and len(samples) == DEPTH
    dut.trig_mask.value = 0xF
    dut.trig_value.value = 0xF
    dut.trig_rise.value = 0
    dut.trig_fall.value = 0b0001                                             # 値 0xF（bit0 = 1）と bit0 の立ち下がりは両立しない
    dut.run.value = 1
    await ClockCycles(dut.clk, 200)
    await ReadOnly()
    assert int(dut.capturing.value) == 1 and int(dut.done.value) == 0
    await RisingEdge(dut.clk)
    dut.run.value = 0
    await ClockCycles(dut.clk, 2)
    await ReadOnly()
    assert int(dut.capturing.value) == 0


@cocotb.test()
async def one_cycle_external_trigger_between_samples(dut):
    await setup(dut)

    async def pulse():
        await ClockCycles(dut.clk, 301)                   # プリトリガが溜まった後の、サンプルの合間
        dut.ext_trigger.value = 1
        await RisingEdge(dut.clk)
        dut.ext_trigger.value = 0

    cocotb.start_soon(pulse())
    # 値 0xF と bit0 の立ち下がりは両立しない: 外部トリガでしか成立しない
    samples, trigger = await capture(dut, rate=6, pretrigger=4, mask=0xF, value=0xF, fall=0b0001)
    assert trigger == 4
    assert all((b - a) % 16 == 7 for a, b in zip(samples, samples[1:]))


@cocotb.test()
async def pulse_width_event_is_anded_with_the_pattern(dut):
    """U21: pw_enable のとき、トリガ = パターンの条件 かつ 前のサンプルからの間に pw_event（la_pulse）があった。"""
    await setup(dut)
    dut.pw_enable.value = 1
    events = []

    async def pulse():
        await ClockCycles(dut.clk, 403)                   # プリトリガが溜まった後の、サンプルの合間
        events.append(int(dut.time.value))
        dut.pw_event.value = 1
        await RisingEdge(dut.clk)
        dut.pw_event.value = 0

    cocotb.start_soon(pulse())
    samples, trigger = await capture(dut, rate=6, pretrigger=4)            # パターンは常に成立: pw_event だけで決まる
    assert trigger == 4
    assert int(dut.trigger_time.value) - events[0] in range(0, 12)
    assert all((b - a) % 16 == 7 for a, b in zip(samples, samples[1:]))


@cocotb.test()
async def trigger_time_is_la_internal_delay_after_the_trigger_sample(dut):
    # プローブ（カウンタ）と time は同じ辺で進む。トリガのサンプル（値 5）の time と、報告の trigger_time の差
    # = 内蔵ロジアナの入り口から時刻の付くまでの遅れ（fprobe_gw.caps.LA_INTERNAL_DELAY）
    from fprobe_gw.caps import LA_INTERNAL_DELAY
    await setup(dut)
    p0, t0 = int(dut.probes.value), int(dut.time.value)
    await capture(dut, mask=0xF, value=5)
    assert (int(dut.trigger_time.value) - 5 - (t0 - p0)) % 16 == LA_INTERNAL_DELAY
