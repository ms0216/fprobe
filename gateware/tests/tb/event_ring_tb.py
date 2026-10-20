"""event_ring: 外部メモリの無い構成のイベント記録（BRAM のリング）。la_deep_buffer のイベントと読み出しと同じ口・意味
（docs/superpowers/specs/2026-10-20-bram-event-ring-design.md）。DEPTH_BITS = 4（16 件）で試す。"""
import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge

DEPTH = 16


async def setup(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.rst.value = 1
    dut.ev_enable.value = 0
    dut.ev_valid.value = 0
    dut.ev_data.value = 0
    dut.read.value = 0
    dut.read_region.value = 1
    dut.read_first.value = 0
    dut.read_count.value = 0
    dut.out_ready.value = 1
    await ClockCycles(dut.clk, 3)
    dut.rst.value = 0
    await ClockCycles(dut.clk, 2)


async def push(dut, values):
    for v in values:
        dut.ev_valid.value = 1
        dut.ev_data.value = v
        await RisingEdge(dut.clk)
    dut.ev_valid.value = 0


async def read_events(dut, first, count, ready_rng=None, timeout=20000):
    dut.read_first.value = first
    dut.read_count.value = count
    dut.read.value = 1
    await RisingEdge(dut.clk)
    dut.read.value = 0
    data = bytearray()
    for _ in range(timeout):
        if ready_rng is not None:
            dut.out_ready.value = int(ready_rng.random() < 0.6)
        await RisingEdge(dut.clk)
        if dut.out_valid.value == 1 and dut.out_ready.value == 1:
            data.append(int(dut.out_data.value))
        if len(data) == 16 * count and dut.reading.value == 0:
            break
    dut.out_ready.value = 1
    return [int.from_bytes(data[i:i + 16], "little") for i in range(0, len(data), 16)]


@cocotb.test()
async def events_are_recorded_and_read_back_lsb_first(dut):
    await setup(dut)
    assert int(dut.ev_beats.value) == DEPTH
    dut.ev_enable.value = 1
    await RisingEdge(dut.clk)
    values = [random.Random(1).getrandbits(128) for _ in range(5)]
    await push(dut, values)
    await ClockCycles(dut.clk, 2)
    assert int(dut.ev_written.value) == 5 and int(dut.ev_lost.value) == 0
    assert await read_events(dut, 0, 5, ready_rng=random.Random(2)) == values


@cocotb.test()
async def events_are_ignored_while_disabled(dut):
    await setup(dut)
    await push(dut, [1, 2, 3])
    await ClockCycles(dut.clk, 2)
    assert int(dut.ev_written.value) == 0


@cocotb.test()
async def the_ring_wraps_and_keeps_the_newest(dut):
    await setup(dut)
    dut.ev_enable.value = 1
    await RisingEdge(dut.clk)
    values = list(range(100, 100 + DEPTH + 5))                  # 5 件あふれる
    await push(dut, values)
    await ClockCycles(dut.clk, 2)
    written = int(dut.ev_written.value)
    assert written == len(values)
    # PC（EventLog.read）と同じ: 最新の DEPTH 件 = リングの (written - DEPTH) % DEPTH から
    assert await read_events(dut, (written - DEPTH) % DEPTH, DEPTH) == values[-DEPTH:]


@cocotb.test()
async def enabling_again_restarts_from_the_head(dut):
    await setup(dut)
    dut.ev_enable.value = 1
    await RisingEdge(dut.clk)
    await push(dut, [7, 8, 9])
    dut.ev_enable.value = 0
    await ClockCycles(dut.clk, 2)
    dut.ev_enable.value = 1
    await RisingEdge(dut.clk)
    await push(dut, [42])
    await ClockCycles(dut.clk, 2)
    assert int(dut.ev_written.value) == 1
    assert await read_events(dut, 0, 1) == [42]


@cocotb.test()
async def events_keep_being_recorded_while_reading(dut):
    await setup(dut)
    dut.ev_enable.value = 1
    await RisingEdge(dut.clk)
    await push(dut, [1, 2, 3, 4])
    reader = cocotb.start_soon(read_events(dut, 0, 4, ready_rng=random.Random(3)))
    await ClockCycles(dut.clk, 3)
    await push(dut, [5, 6])
    assert await reader == [1, 2, 3, 4]
    assert int(dut.ev_written.value) == 6


@cocotb.test()
async def reading_the_la_region_returns_nothing(dut):
    # read_region = '0'（ロジアナのリング）は外部メモリの無い構成に無い: 0 バイトで終える（止まらない）
    await setup(dut)
    dut.read_region.value = 0
    assert await read_events(dut, 0, 4, timeout=50) == []
    await ClockCycles(dut.clk, 2)
    assert dut.reading.value == 0


@cocotb.test()
async def an_event_in_the_enabling_cycle_goes_to_the_head(dut):
    # 記録を始める周期に来たイベントは 0 番に書く（前の記録の書き位置に書かない。PC は 0 番から読む）
    await setup(dut)
    dut.ev_enable.value = 1
    await RisingEdge(dut.clk)
    await push(dut, [11, 12, 13])
    dut.ev_enable.value = 0
    await ClockCycles(dut.clk, 2)
    dut.ev_enable.value = 1                       # 立ち上がりの周期にイベント
    dut.ev_valid.value = 1
    dut.ev_data.value = 99
    await RisingEdge(dut.clk)
    dut.ev_valid.value = 0
    await ClockCycles(dut.clk, 2)
    assert int(dut.ev_written.value) == 1
    assert await read_events(dut, 0, 1) == [99]
