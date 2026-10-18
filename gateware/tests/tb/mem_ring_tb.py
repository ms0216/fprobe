"""la_mem_writer（2 つのリングへの書き込み）と mem_reader（領域の読み出し）のテストベンチ。
mem_ring_harness、容量 1024 拍 = ロジアナのリング 960 拍 + イベントのリング 64 拍。"""

import random

import cocotb
from cocotb.triggers import ReadOnly, RisingEdge

from tests.models.mem_port import MemPortModel
from tests.tb.common import start_clock_and_reset

CAP = 1024
EV_BEATS = CAP // 16
LA_BEATS = CAP - EV_BEATS


async def setup(dut):
    for name in ("use_reader", "w_start", "w_stop", "post_beats", "in_valid", "in_data", "in_kind", "ev_enable",
                 "in_trig", "in_slot", "r_start", "region_base", "region_size", "first", "count"):
        getattr(dut, name).value = 0
    dut.out_ready.value = 1
    mem = MemPortModel(dut, size_beats=CAP, stall=0.3)
    await start_clock_and_reset(dut)
    cocotb.start_soon(mem.run())
    return mem


async def pulse(dut, name):
    getattr(dut, name).value = 1
    await RisingEdge(dut.clk)
    getattr(dut, name).value = 0


async def write_beats(dut, beats, rng=None):
    """beats: (データ, 種類, トリガ) の列を流す。"""
    rng = rng or random.Random(1)
    for data, kind, trig in beats:
        while True:
            dut.in_valid.value = int(rng.random() > 0.2)
            dut.in_data.value, dut.in_kind.value, dut.in_trig.value, dut.in_slot.value = data, kind, int(trig), 5
            await ReadOnly()
            accepted = dut.in_valid.value == 1 and dut.in_ready.value == 1
            await RisingEdge(dut.clk)
            if accepted:
                break
    dut.in_valid.value = 0


async def read_range(dut, base, size, first, count, stall=0.3, rng=None):
    rng = rng or random.Random(2)
    dut.use_reader.value = 1
    dut.region_base.value, dut.region_size.value = base, size
    dut.first.value, dut.count.value = first, count
    await pulse(dut, "r_start")
    out = []
    while len(out) < count:
        dut.out_ready.value = int(rng.random() >= stall)
        await ReadOnly()
        if dut.out_valid.value == 1 and dut.out_ready.value == 1:
            out.append(int(dut.out_data.value))
        await RisingEdge(dut.clk)
    dut.out_ready.value = 1
    for _ in range(3):
        await RisingEdge(dut.clk)
    assert dut.r_busy.value == 0
    dut.use_reader.value = 0
    return out


@cocotb.test()
async def ring_keeps_history_before_the_trigger(dut):
    await setup(dut)
    dut.post_beats.value = 100
    await pulse(dut, "w_start")
    await write_beats(dut, [(0x1000 + i, 0, i == 2500) for i in range(2601)])   # リングを 2 周以上回る
    await ReadOnly()
    assert dut.done.value == 1 and dut.triggered.value == 1
    trig_beat, written = int(dut.trigger_beat.value), int(dut.written.value)
    assert trig_beat == 2500 % LA_BEATS and written == LA_BEATS and int(dut.trigger_slot.value) == 5
    await RisingEdge(dut.clk)
    await write_beats(dut, [(0, 0, False)] * 10)                                 # 止まった後は読み捨てる
    pre = LA_BEATS - 101
    first = (trig_beat - pre) % LA_BEATS
    data = await read_range(dut, 0, LA_BEATS, first, LA_BEATS)
    assert data == [0x1000 + i for i in range(2500 - pre, 2601)]


@cocotb.test()
async def events_go_to_their_own_ring_alongside_the_analyzer(dut):
    await setup(dut)
    dut.post_beats.value = 0
    dut.ev_enable.value = 1
    await RisingEdge(dut.clk)
    await pulse(dut, "w_start")
    beats = []
    for i in range(100):                                    # ロジアナの拍とイベントを混ぜる（イベントは 100 件 > 64）
        beats.append((0x1000 + i, 0, False))
        beats.append((0xE000 + i, 1, False))
    await write_beats(dut, beats + [(0x1999, 0, True)])
    await ReadOnly()
    assert int(dut.ev_written.value) == 100 and dut.done.value == 1
    await RisingEdge(dut.clk)
    # イベントのリング: 最新の 64 件（36..99）は 100 % 64 = 36 番目から
    events = await read_range(dut, LA_BEATS, EV_BEATS, 100 % EV_BEATS, EV_BEATS)
    assert events == [0xE000 + i for i in range(36, 100)]
    # ロジアナのリングはイベントに乱されない
    assert await read_range(dut, 0, LA_BEATS, 0, 101) == [0x1000 + i for i in range(100)] + [0x1999]
    # イベントを止めると読み捨てる
    dut.ev_enable.value = 0
    await write_beats(dut, [(0xF000, 1, False)] * 5)
    assert int(dut.ev_written.value) == 100
