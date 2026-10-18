"""swo_manchester_rx のテストベンチ（clk 100 MHz）。線は tests.models.swo の Manchester（Arm の TPIU の形）。

速さの限界（100 MHz で divisor = 4 → 25 Mbit/s）と、送り手の速さのずれ・線のひずみへの耐性も確かめる。
"""

import random

import cocotb
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge, Timer

from tests.models.swo import manchester_halves, segments
from tests.tb.common import CLK_PERIOD_NS, PulseCounter, start_clock_and_reset


class ByteCollector:
    def __init__(self, dut):
        self.items: list[int] = []
        cocotb.start_soon(self._run(dut))

    async def _run(self, dut):
        while True:
            await RisingEdge(dut.clk)
            await ReadOnly()
            if dut.out_valid.value == 1:
                self.items.append(int(dut.out_data.value))


async def setup(dut, divisor: int):
    dut.divisor.value = divisor
    dut.rx.value = 0
    await start_clock_and_reset(dut)
    await ClockCycles(dut.clk, 4)


async def drive(dut, halves: list[int], bit_ns: float, skew_ns: float = 0.0):
    for level, length in segments(halves, bit_ns / 2, skew_ns):
        dut.rx.value = level
        await Timer(round(length * 1000), unit="ps")


async def send(dut, data: bytes, bit_ns: float, *, skew_ns: float = 0.0, idle_bits: int = 1, phase_ns: float = 0.0):
    if phase_ns:
        await Timer(round(phase_ns * 1000), unit="ps")
    await drive(dut, manchester_halves(data, idle_bits), bit_ns, skew_ns)


@cocotb.test()
async def receives_frames_of_any_length(dut):
    divisor = 8
    await setup(dut, divisor)
    got = ByteCollector(dut)
    errors = PulseCounter(dut, dut.frame_error)
    bit_ns = divisor * CLK_PERIOD_NS
    for frame in (b"\x00", b"\xff", b"\x55\xaa", bytes([0x01, 0x80, 0x3c, 0x00, 0xff])):
        await send(dut, frame, bit_ns)                      # 休止 1 ビット（最短）で続ける
    await ClockCycles(dut.clk, 4 * divisor)
    assert bytes(got.items) == b"\x00\xff\x55\xaa\x01\x80\x3c\x00\xff"
    assert errors.count == 0


@cocotb.test()
async def partial_byte_is_a_frame_error_and_recovers(dut):
    divisor = 8
    await setup(dut, divisor)
    got = ByteCollector(dut)
    errors = PulseCounter(dut, dut.frame_error)
    bit_ns = divisor * CLK_PERIOD_NS
    halves = manchester_halves(b"\x5a")
    await drive(dut, halves[:2 + 2 * 5] + [0] * 4, bit_ns)   # 開始ビットと 5 ビットで終わる
    await send(dut, b"\xa5", bit_ns)
    await ClockCycles(dut.clk, 4 * divisor)
    assert errors.count == 1
    assert got.items == [0xa5]


@cocotb.test()
async def line_held_high_is_not_a_start(dut):
    """つないでいない SWO（プルアップで High）や NRZ の休止（High）を Manchester の開始と取らない。"""
    divisor = 8
    await setup(dut, divisor)
    got = ByteCollector(dut)
    errors = PulseCounter(dut, dut.frame_error)
    dut.rx.value = 1
    await ClockCycles(dut.clk, 20 * divisor)
    dut.rx.value = 0
    await ClockCycles(dut.clk, 2 * divisor)
    await send(dut, b"\x42", divisor * CLK_PERIOD_NS)
    await ClockCycles(dut.clk, 4 * divisor)
    assert got.items == [0x42]
    assert errors.count == 0


async def _trial(dut, got, divisor: int, rate_error: float, skew_ns: float, rng: random.Random) -> bool:
    data = bytes(rng.randrange(256) for _ in range(16))
    got.items.clear()
    await send(dut, data, divisor * CLK_PERIOD_NS * (1 + rate_error), skew_ns=skew_ns,
               phase_ns=rng.uniform(0, CLK_PERIOD_NS))
    await ClockCycles(dut.clk, 4 * divisor)
    return bytes(got.items) == data


@cocotb.test()
async def speed_and_tolerance(dut):
    """divisor（1 ビットの周期数 T）ごとに、送り手の速さのずれ（rate_error。正 = 遅い）と線のひずみ（skew: High が長い /
    短い）を変えて、位相を乱数にした 16 バイトのフレームを 8 回ずつ受ける。すべて正しく受けること。

    中央の変化は T(1+e) ± 1 周期、境目の変化は T(1+e)/2 ± 1 周期に検出され、閾値 T - floor(T/4) で分ける。
    2026-10-19 にずれ -30〜+30%（5% 刻み）・ひずみ 0〜±10 ns を掃いた結果（6 フレームすべて正しく受けた範囲）:
      T=4（25 Mbit/s）  ひずみ 0: -25〜0%。遅い側の余裕が無い（境目が閾値 3 に届く）。±2 ns で既に欠ける
      T=5（20 Mbit/s）  ひずみ 0: ±20%、±2 ns: -15〜+10%、±5 ns: -10〜0%
      T=6（16.7 Mbit/s）ひずみ 0: ±15%、±2 ns: ±10%、±5 ns: ±5%、±10 ns: 0% のみ
      T=8（12.5 Mbit/s）ひずみ 0: ±25%、±2 ns: ±20%、±5 ns: -15〜+10%、±10 ns: -10〜0%
      T=16（6.3 Mbit/s）ひずみ 0: ±25%、±5 ns: ±20%、±10 ns: ±15%
    ここではその内側を確かめる。"""
    rng = random.Random(51)
    cases = [
        (4, 0.0, 0.0), (4, -0.10, 0.0),
        (5, 0.10, 2.0), (5, -0.10, -2.0),
        (6, 0.10, 2.0), (6, -0.10, -2.0), (6, 0.05, 5.0), (6, -0.05, -5.0),
        (8, 0.15, 2.0), (8, -0.15, -2.0), (8, 0.10, 5.0), (8, -0.10, -5.0),
        (16, 0.20, 5.0), (16, -0.20, -5.0), (16, 0.15, 10.0), (16, -0.15, -10.0),
    ]
    await setup(dut, 4)
    got = ByteCollector(dut)
    for divisor, rate_error, skew in cases:
        dut.divisor.value = divisor
        await ClockCycles(dut.clk, 4 * divisor)
        for _ in range(8):
            ok = await _trial(dut, got, divisor, rate_error, skew, rng)
            assert ok, f"divisor={divisor} rate_error={rate_error} skew={skew} ns: {got.items}"
