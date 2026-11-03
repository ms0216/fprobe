"""la_lane_inputs のテストベンチ（時刻の正しさ S1b の N2: docs/superpowers/specs/2026-11-01-time-accuracy-design.md §9.5）。

レーンの並びの約束（la_lane_capture・la_lane_encoder の時刻の式の前提）: 立ち上がり w の後に出た語のレーン k は、細かい辺
e = N × w − (N − 1 − k) − IN_AGE のサンプル（sys の辺 T は細かい辺 N × T）。ある ch が「辺 n の後に変わった」なら、変化を最初に
見せるレーンは e = n + 1。
- 内部の ch（sys の FF。辺 T の直後に変わる = 細かい辺 N × T の直後）: 最初に見せるレーンの e − 1 = N × T（ずれ 0）。
- sys で取るピン（FINE_PINS = false。周期 [T, T + 1) の中で変わる）: e − 1 = N × (T + 1)（ずれ −1 刻み・位相の幅 1 刻み）。
- 細かい ch（専用の入力と FINE_PINS のピン）は入力のレーンをそのまま通す。
"""

import os

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ReadOnly, RisingEdge, Timer

N = int(os.environ["LANE_N"])
AGE = int(os.environ["LANE_AGE"])
FINE = os.environ.get("LANE_FINE", "0") == "1"
WIDTH, PINS, INPUTS = 16, 8, 2


class Bench:
    def __init__(self, dut):
        self.dut = dut
        self.w = 0                                     # 最後の立ち上がりの番号

    async def count(self):
        while True:
            await RisingEdge(self.dut.clk)
            self.w += 1


async def setup(dut):
    dut.pins.value = 0
    dut.pin_lanes.value = 0
    dut.internal.value = 0
    dut.input_lanes.value = 0
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    b = Bench(dut)
    cocotb.start_soon(b.count())
    for _ in range(8):
        await RisingEdge(dut.clk)
    return b


def lane_edge(w: int, k: int) -> int:
    return N * w - (N - 1 - k) - AGE


async def first_lane_showing(dut, b, ch: int, cycles: int = 12) -> int:
    """ch が 1 になった最初のレーンの細かい辺 e（立ち上がりの直後の lane_data を読む）。"""
    for _ in range(cycles):
        await ReadOnly()
        data = int(dut.lane_data.value)
        for k in range(N):
            if data >> (k * WIDTH + ch) & 1:
                return lane_edge(b.w, k)
        await RisingEdge(dut.clk)
    raise AssertionError(f"ch {ch} が出ない")


@cocotb.test()
async def internal_channels_land_on_the_fine_axis(dut):
    b = await setup(dut)
    for bit in (0, 1):
        dut.internal.value = 0
        for _ in range(6):
            await RisingEdge(dut.clk)
        await RisingEdge(dut.clk)
        await Timer(1, unit="ps")
        t = b.w                                        # 内部の FF が変わった辺（辺 t の直後）
        dut.internal.value = 1 << bit
        e = await first_lane_showing(dut, b, PINS + bit)
        assert e - 1 == N * t, (N, AGE, bit, t, e)
        await RisingEdge(dut.clk)


@cocotb.test()
async def sys_sampled_pins_land_one_tick_later(dut):
    if FINE:
        return
    b = await setup(dut)
    for offset_ps in (1, 5000, 9999):
        dut.pins.value = 0
        for _ in range(8):
            await RisingEdge(dut.clk)
        await RisingEdge(dut.clk)
        await Timer(offset_ps, unit="ps")
        t = b.w                                        # ピンが変わる周期 [t, t + 1)（count がこの辺を数えた後に読む）
        dut.pins.value = 1
        e = await first_lane_showing(dut, b, 0)
        assert e - 1 == N * (t + 1), (N, AGE, offset_ps, t, e)
        await RisingEdge(dut.clk)


@cocotb.test()
async def fine_channels_pass_through(dut):
    b = await setup(dut)
    for w in range(3):
        word = 0
        for k in range(N):
            word |= ((k + w) % 4) << (k * INPUTS)
        dut.input_lanes.value = word
        pin_word = 0
        for k in range(N):
            pin_word |= ((k * 3 + w) & 0xFF) << (k * PINS)
        dut.pin_lanes.value = pin_word
        await Timer(1, unit="ps")
        data = int(dut.lane_data.value)
        for k in range(N):
            assert data >> (k * WIDTH + PINS + 2) & 3 == (k + w) % 4
            if FINE:
                assert data >> (k * WIDTH) & 0xFF == (k * 3 + w) & 0xFF
        await RisingEdge(dut.clk)
