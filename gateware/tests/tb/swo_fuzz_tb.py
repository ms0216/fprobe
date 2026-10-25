"""swo_receiver のランダム試験。基準モデルは「線へ出したバイト列がそのまま順に出る（error・overrun なし）」。

方式（UART / Manchester）・分周比 4〜24・バイト・フレームの長さ・休止の長さ・位相を乱数にし、受信を始め直す
（run を落として上げる）ことも混ぜる。線の速さは分周比どおり（ずれ・ひずみなし。許容は swo_*_tb が掃いている）。
"""

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge, Timer

from tests.models.swo import manchester_halves, segments
from tests.models.uart import uart_bits
from tests.tb.common import CLK_PERIOD_NS

FLUSH = 32


class Collector:
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


async def start_run(dut):
    dut.run.value = 0
    await ClockCycles(dut.clk, 2)
    dut.run.value = 1
    await ClockCycles(dut.clk, FLUSH + 4)


async def send_uart(dut, data: bytes, bit_ns: float, rng: random.Random):
    for byte in data:
        for bit in uart_bits(byte):
            dut.swo.value = bit
            await Timer(round(bit_ns * 1000), unit="ps")
        extra = rng.choice((0, 0, 1, 2, 5))
        if extra:
            dut.swo.value = 1
            await Timer(round(extra * bit_ns * 1000), unit="ps")


async def send_manchester(dut, data: bytes, bit_ns: float, idle_bits: int):
    for level, length in segments(manchester_halves(data, idle_bits), bit_ns / 2):
        dut.swo.value = level
        await Timer(round(length * 1000), unit="ps")


@cocotb.test()
async def random_frames_both_modes(dut):
    rng = random.Random(4242)
    dut.divisor.value = 8
    dut.manchester.value = 0
    dut.run.value = 0
    dut.out_ready.value = 1
    dut.swo.value = 1
    cocotb.start_soon(Clock(dut.clk, CLK_PERIOD_NS, unit="ns").start())
    dut.rst.value = 1
    await ClockCycles(dut.clk, 3)
    dut.rst.value = 0
    await ClockCycles(dut.clk, 4)
    got = Collector(dut)
    manchester_ok = has_manchester(dut)
    for trial in range(60):
        man = manchester_ok and rng.random() < 0.5
        divisor = rng.choice((4, 5, 6, 7, 8, 9, 11, 16, 24))
        dut.run.value = 0
        await ClockCycles(dut.clk, 2)
        dut.divisor.value = divisor
        dut.manchester.value = int(man)
        dut.swo.value = 0 if man else 1                      # 休止: Manchester は Low、UART は High
        await ClockCycles(dut.clk, 2 * divisor)
        await start_run(dut)
        got.items.clear()
        sent = bytearray()
        bit_ns = divisor * CLK_PERIOD_NS
        await Timer(round(rng.uniform(0, CLK_PERIOD_NS) * 1000), unit="ps")
        for _ in range(rng.randrange(1, 4)):
            data = bytes(rng.randrange(256) for _ in range(rng.randrange(1, 7)))
            sent += data
            if man:
                await send_manchester(dut, data, bit_ns, rng.randrange(1, 9))
            else:
                await send_uart(dut, data, bit_ns, rng)
        dut.swo.value = 0 if man else 1
        await ClockCycles(dut.clk, 6 * divisor)
        assert bytes(got.items) == bytes(sent), f"trial={trial} man={man} divisor={divisor}: {got.items} != {list(sent)}"
        assert int(dut.error.value) == 0, f"trial={trial} man={man} divisor={divisor}: error"
        assert int(dut.overrun.value) == 0, f"trial={trial} man={man} divisor={divisor}: overrun"
