"""la_probe_inputs のテストベンチ（R2: 内部の ch の段数をピンに揃える。docs/superpowers/specs/2026-11-01-time-accuracy-design.md）。

外の信号（ピン ch 0）と内部の信号（ch LA_PIN_COUNT）を、同じ立ち上がりの直後に変える（= 時刻 T の辺で変わった FF と、
その辺の直後に変わったピン）。内部の信号は、ここに来るまでに INTERNAL_AGE 段のレジスタを通ったものとして、テストベンチが
その段数だけ遅らせて入れる。どちらも probes に同じ周期で出る（ずれ 0 = LA_CH_TIMING の内部の ch の 0）。
フィルタのある構成では、外の信号が probes に出るまでの段数がフィルタの段数（fprobe_gw.timing.LA_FILTER_DELAY）であることも確かめる。
"""

import os

import cocotb
from cocotb.triggers import ReadOnly, RisingEdge

from tests.tb.common import start_clock_and_reset

PINS = 8
FILTER = os.environ.get("LA_PROBE_FILTER", "1") == "1"
AGE = int(os.environ.get("LA_PROBE_AGE", "0"))


async def setup(dut):
    dut.ext_raw.value = 0
    dut.internal.value = 0
    dut.filter_channels.value = 0
    dut.filter_cycles.value = 0
    await start_clock_and_reset(dut)
    for _ in range(8):
        await RisingEdge(dut.clk)


async def edge_cycles(dut, *, internal_bit: int, cycles: int = 20) -> tuple[int, int]:
    """辺 0 の直後にピン ch 0 を 1 にし、内部の FF もその辺で 1 になったものとする（上流の INTERNAL_AGE 段のレジスタを
    通ってここに届くのは辺 INTERNAL_AGE の直後）。probes の ch 0 と ch PINS + internal_bit が 1 になった辺の番号を返す
    （辺 k の直後の値を見る。k = 0 は変えた直後）。"""
    pin_at = int_at = None
    await RisingEdge(dut.clk)
    for k in range(cycles):
        if k == 0:
            dut.ext_raw.value = 1
        if k == AGE:
            dut.internal.value = 1 << internal_bit
        await ReadOnly()
        probes = int(dut.probes.value)
        if pin_at is None and probes & 1:
            pin_at = k
        if int_at is None and probes >> (PINS + internal_bit) & 1:
            int_at = k
        await RisingEdge(dut.clk)
    assert pin_at is not None and int_at is not None, (pin_at, int_at)
    return pin_at, int_at


@cocotb.test()
async def internal_channels_arrive_in_the_same_cycle_as_a_pin_changed_on_the_same_edge(dut):
    from fprobe_gw.timing import LA_FILTER_DELAY
    await setup(dut)
    for bit in (0, 1):
        dut.ext_raw.value = 0
        dut.internal.value = 0
        for _ in range(8):
            await RisingEdge(dut.clk)
        pin_at, int_at = await edge_cycles(dut, internal_bit=bit)
        dut._log.info("FILTER=%s AGE=%d bit %d: pin %d, internal %d", FILTER, AGE, bit, pin_at, int_at)
        assert int_at == pin_at, (bit, pin_at, int_at)
        # 辺 0 の直後に変えたピンが probes に出る辺の番号: フィルタの段数（無ければ 0 = そのまま通る）
        assert pin_at == (LA_FILTER_DELAY if FILTER else 0), pin_at


@cocotb.test()
async def the_channels_land_in_the_layout_order(dut):
    # ch 0〜7 = ピン、8・9 = 内部、残りは 0（専用の入力は無い構成）
    await setup(dut)
    dut.ext_raw.value = 0xA5
    dut.internal.value = 0b10
    for _ in range(AGE + 8):
        await RisingEdge(dut.clk)
    await ReadOnly()
    assert int(dut.probes.value) == 0xA5 | 0b10 << PINS
