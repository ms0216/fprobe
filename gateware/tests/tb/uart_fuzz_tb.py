"""uart_loopback（tx -> rx 直結）のランダム試験。基準モデルは「送ったバイト列がそのまま順に出る」。

* 分周比 2〜17 とランダムなバイト・ランダムな間隔（連続送信を含む）で往復する。
* 送受信の途中で分周比を小さくしても、固まらず（2^16 周期止まらず）後続のバイトを受けられる。
"""

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge

from tests.tb.common import CLK_PERIOD_NS, StreamCollector, stream_send


async def setup(dut, divisor, *, clock=False):
    """リセットし直す（1 つの試験の中で何度か始め直すため）。clock = True の最初の 1 回だけクロックを起こす。"""
    dut.divisor.value = divisor
    dut.in_valid.value = 0
    dut.in_data.value = 0
    dut.out_ready.value = 1
    if clock:
        cocotb.start_soon(Clock(dut.clk, CLK_PERIOD_NS, unit="ns").start())
    dut.rst.value = 1
    await ClockCycles(dut.clk, 3)
    dut.rst.value = 0
    await RisingEdge(dut.clk)


async def send_byte(dut, byte):
    await stream_send(dut, "in", [byte])


@cocotb.test()
async def random_roundtrip_at_many_divisors(dut):
    rng = random.Random(1234)
    for n, divisor in enumerate((2, 3, 4, 5, 7, 8, 13, 16, 17)):
        await setup(dut, divisor, clock=n == 0)
        got = StreamCollector(dut, "out")
        sent = []
        for _ in range(24):
            byte = rng.randrange(256)
            sent.append(byte)
            await send_byte(dut, byte)
            gap = rng.choice((0, 0, 1, divisor, 3 * divisor + 1, 10 * divisor))
            if gap:
                await ClockCycles(dut.clk, gap)
        await ClockCycles(dut.clk, divisor * 14)
        assert got.items == sent, f"divisor={divisor}: {got.items} != {sent}"


@cocotb.test()
async def shrinking_divisor_mid_frame_does_not_stall(dut):
    # 送受信の途中で分周比を小さくしても、timer が新しい divisor - 1 を過ぎたまま一致を待って止まらない
    # （一致比較だと次の一致まで 2^16 周期止まり、その間のバイトを取りこぼす）
    rng = random.Random(77)
    for n, phase in enumerate((20, 60, 150, 370, 700)):   # 古い分周比 100 でフレームのどこか（開始ビット〜停止ビット）
        await setup(dut, 100, clock=n == 0)
        got = StreamCollector(dut, "out")
        await send_byte(dut, rng.randrange(256))
        await ClockCycles(dut.clk, phase)
        dut.divisor.value = 8
        await ClockCycles(dut.clk, 100 * 12)             # 古い分周比のフレームがあれば終わる長さ（2^16 より十分短い）
        tail = [rng.randrange(256) for _ in range(4)]
        before = len(got.items)
        for byte in tail:
            await send_byte(dut, byte)
        await ClockCycles(dut.clk, 8 * 14)
        assert got.items[before:] == tail, f"phase={phase}: {got.items[before:]} != {tail}"
