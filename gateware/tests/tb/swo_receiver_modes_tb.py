"""swo_receiver のテストベンチ（clk 100 MHz）: UART（NRZ）方式と Manchester 方式の切り替え、異常の申告、NRZ の最高速度。

HAS_MANCHESTER = false の構成（tests/test_swo.py の test_swo_receiver_without_manchester）では、manchester を 1 にしても
UART 方式で受ける（Manchester の回路が無い）。構成はシミュレータから読めないので、dut.manchester_g の有無で見分ける。
"""

import random

import cocotb
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge, Timer

from tests.models.swo import manchester_halves, segments
from tests.models.uart import uart_bits
from tests.tb.common import CLK_PERIOD_NS, start_clock_and_reset

FLUSH = 32                    # swo_receiver の FLUSH_CYCLES（既定）


class ByteCollector:
    def __init__(self, dut):
        self.items: list[int] = []
        cocotb.start_soon(self._run(dut))

    async def _run(self, dut):
        while True:
            await RisingEdge(dut.clk)
            await ReadOnly()
            if dut.out_valid.value == 1 and dut.out_ready.value == 1:
                self.items.append(int(dut.out_data.value))


def has_manchester(dut) -> bool:
    try:
        dut.manchester_g
    except AttributeError:
        return False
    return True


async def setup(dut, divisor: int, *, manchester: bool, idle: int):
    dut.divisor.value = divisor
    dut.manchester.value = int(manchester)
    dut.run.value = 0
    dut.out_ready.value = 1
    dut.swo.value = idle
    await start_clock_and_reset(dut)
    await ClockCycles(dut.clk, 4)
    dut.run.value = 1
    await ClockCycles(dut.clk, FLUSH + 4)                   # 開始の窓（B10）の間は出さない


async def send_uart(dut, data: bytes, bit_ns: float, phase_ns: float = 0.0):
    if phase_ns:
        await Timer(round(phase_ns * 1000), unit="ps")
    for byte in data:
        for bit in uart_bits(byte):
            dut.swo.value = bit
            await Timer(round(bit_ns * 1000), unit="ps")


async def send_manchester(dut, data: bytes, bit_ns: float):
    for level, length in segments(manchester_halves(data), bit_ns / 2):
        dut.swo.value = level
        await Timer(round(length * 1000), unit="ps")


@cocotb.test()
async def uart_mode(dut):
    await setup(dut, 8, manchester=False, idle=1)
    got = ByteCollector(dut)
    await send_uart(dut, b"\x00\xffSWO\x55", 8 * CLK_PERIOD_NS)
    await ClockCycles(dut.clk, 20)
    assert bytes(got.items) == b"\x00\xffSWO\x55"
    assert dut.error.value == 0 and dut.overrun.value == 0


@cocotb.test()
async def manchester_mode_and_its_errors(dut):
    await setup(dut, 8, manchester=True, idle=0)
    got = ByteCollector(dut)
    bit_ns = 8 * CLK_PERIOD_NS
    dut._log.info(f"has_manchester={has_manchester(dut)}")
    if not has_manchester(dut):
        # 回路が無い構成: manchester を無視して UART 方式で受ける
        dut.swo.value = 1
        await ClockCycles(dut.clk, 20 * 8)                  # 休止（Low）を開始ビットと取った分が終わるまで
        got.items.clear()
        await send_uart(dut, b"\x12\x34", bit_ns)
        await ClockCycles(dut.clk, 20)
        assert bytes(got.items) == b"\x12\x34"
        return
    await send_manchester(dut, b"\x01\x80\xa5", bit_ns)
    await ClockCycles(dut.clk, 40)
    assert bytes(got.items) == b"\x01\x80\xa5"
    assert dut.error.value == 0
    # 端数のビットで終わったフレーム → error（受信を始め直すまで残る）
    halves = manchester_halves(b"\x0f")
    for level, length in segments(halves[:2 + 6] + [0] * 4, bit_ns / 2):
        dut.swo.value = level
        await Timer(round(length * 1000), unit="ps")
    await ClockCycles(dut.clk, 40)
    assert dut.error.value == 1
    dut.run.value = 0
    await ClockCycles(dut.clk, 2)
    dut.run.value = 1
    await ClockCycles(dut.clk, FLUSH + 4)
    assert dut.error.value == 0
    # 下流が受けられなければ捨てて overrun
    dut.out_ready.value = 0
    await send_manchester(dut, b"\x77", bit_ns)
    await ClockCycles(dut.clk, 40)
    assert dut.overrun.value == 1


@cocotb.test()
async def stopped_receiver_drops_bytes(dut):
    await setup(dut, 8, manchester=False, idle=1)
    got = ByteCollector(dut)
    dut.run.value = 0
    await send_uart(dut, b"\x42", 8 * CLK_PERIOD_NS)
    await ClockCycles(dut.clk, 20)
    assert got.items == []


async def uart_trial(dut, got, divisor: int, rate_error: float, rng: random.Random) -> bool:
    data = bytes(rng.randrange(256) for _ in range(16))
    got.items.clear()
    await send_uart(dut, data, divisor * CLK_PERIOD_NS * (1 + rate_error), phase_ns=rng.uniform(0, CLK_PERIOD_NS))
    dut.swo.value = 1
    await ClockCycles(dut.clk, 4 * divisor)
    return bytes(got.items) == data


@cocotb.test()
async def uart_speed_and_tolerance(dut):
    """UART 方式の最高速度。開始ビットごとに位相を合わせ、以後は divisor ごとに数える（8N1、10 ビット）。
    16 バイトを隙間なく（停止ビット 1 つ）、位相を乱数にして 8 回ずつ受ける。

    2026-10-19 に速さのずれ -6〜+6%（1% 刻み。正 = 送り手が遅い）を掃いた結果（すべて正しく受けた範囲）:
      divisor=4（25 Mbaud）: -1〜+5%、5: -3〜+4%、6・8・16: -3〜+5%
    窓が遅い側に寄るのは、uart_rx が開始ビットの立ち下がりを同期化（2 段）の後に見て、そこから半ビット数えるため
    （標本点がビットの中央より 2〜3 周期遅い）。divisor = 4 では標本点の量子化（±1 周期 = ±25%）も効く。
    ここではその内側を確かめる。"""
    rng = random.Random(24)
    await setup(dut, 4, manchester=False, idle=1)
    got = ByteCollector(dut)
    for divisor, rate_error in ((4, 0.0), (4, 0.03), (4, -0.005), (8, 0.04), (8, -0.025), (16, 0.04), (16, -0.025)):
        dut.divisor.value = divisor
        await ClockCycles(dut.clk, 4 * divisor)
        for _ in range(8):
            ok = await uart_trial(dut, got, divisor, rate_error, rng)
            assert ok, f"divisor={divisor} rate_error={rate_error}: {got.items}"
