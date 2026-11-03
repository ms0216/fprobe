"""aux_link（補助マイコンの計測値の受け口）のテストベンチ。CLK_HZ = 8 MHz、BAUD = 1 Mbaud（1 ビット = 8 周期）。"""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge

from tests.models.uart import uart_bits

DIVISOR = 8


def sample(ch: int, value: int) -> bytes:
    return bytes([0x80 | ch, (value >> 7) & 0x7F, value & 0x7F])


async def send(dut, data: bytes):
    for b in data:
        for level in uart_bits(b):
            dut.rx.value = level
            await ClockCycles(dut.clk, DIVISOR)


async def watch(dut, out: list):
    while True:
        await RisingEdge(dut.clk)
        await ReadOnly()
        if dut.sample_valid.value == 1:
            out.append((int(dut.sample_ch.value), int(dut.sample_value.value)))


@cocotb.test()
async def samples_are_decoded_and_resynchronised(dut):
    dut.rx.value = 1
    cocotb.start_soon(Clock(dut.clk, 125, unit="ns").start())
    dut.rst.value = 1
    await ClockCycles(dut.clk, 3)
    dut.rst.value = 0
    got = []
    cocotb.start_soon(watch(dut, got))
    # 途中から受け始め（値の 2 バイトだけ）→ 捨てて次の組で揃う。組の途中の先頭は誤りに数える
    await send(dut, sample(0, 3300)[1:] + sample(0, 3300) + sample(1, 0x3FFF) + sample(2, 5)[:2]
               + sample(3, 1234) + sample(9, 1))
    await ClockCycles(dut.clk, 40)
    assert got == [(0, 3300), (1, 0x3FFF), (3, 1234)]               # ch 9 は範囲外で捨てる
    assert int(dut.errors.value) == 1


async def count_time(dut):
    """time を毎周期 1 進める（timebase と同じ: 立ち上がりの直後に変わる）。"""
    while True:
        await RisingEdge(dut.clk)
        dut.time.value = (int(dut.time.value) + 1) % (1 << 48)


@cocotb.test()
async def the_sample_time_is_the_start_bit_of_the_first_byte(dut):
    """組の時刻（時刻の正しさの B2、段階 S3）: 時刻が T になる辺の直後に組の先頭のバイトの開始ビットを下げると、
    sample_time = T + UART_RX_START_DELAY（時刻の表 fprobe_gw.timing の aux）。受け取りの周期（3 バイト目の停止ビットの後）ではない。"""
    from fprobe_gw.timing import UART_RX_START_DELAY
    dut.rx.value = 1
    dut.time.value = 70000
    cocotb.start_soon(Clock(dut.clk, 125, unit="ns").start())
    dut.rst.value = 1
    await ClockCycles(dut.clk, 3)
    dut.rst.value = 0
    cocotb.start_soon(count_time(dut))
    await ClockCycles(dut.clk, 4)
    for ch, value in ((0, 3300), (2, 17)):
        await RisingEdge(dut.clk)
        await ReadOnly()
        t = int(dut.time.value) + 1                                 # 次の辺の後の time
        await RisingEdge(dut.clk)
        seen = []

        async def first_valid():
            while not seen:
                await RisingEdge(dut.clk)
                await ReadOnly()
                if dut.sample_valid.value == 1:
                    seen.append((int(dut.sample_ch.value), int(dut.sample_value.value), int(dut.sample_time.value)))
        waiter = cocotb.start_soon(first_valid())
        await send(dut, sample(ch, value))                          # 辺の直後に開始ビット
        await ClockCycles(dut.clk, 2 * DIVISOR)
        assert seen, "組が来ない"
        waiter.cancel()
        got = seen[0]
        assert got == (ch, value, t + UART_RX_START_DELAY), (got, t)
        await ClockCycles(dut.clk, 2 * DIVISOR)
