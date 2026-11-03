"""la_testgen（外部ピン入力の限界を測る試験用の方形波。cap_clk の領域。周期 = cap_clk の周期の数、high = 1 の周期の数）のテストベンチ。
sys（clk）と cap_clk は 1 : 3（clk 12 ns、cap_clk 4 ns）。"""

import cocotb
from cocotb.triggers import ClockCycles, Edge, ReadOnly, RisingEdge, Timer
from cocotb.utils import get_sim_time

from tests.tb.common import start_clock_and_reset

CAP_PS = 4000


async def setup(dut):
    for name in ("enable", "edge_sel", "period", "high"):
        getattr(dut, name).value = 0
    await start_clock_and_reset(dut, period_ns=12, cap_ratio=3)


async def configure(dut, *, enable, period, high, edge_sel=0):
    await RisingEdge(dut.clk)                            # 直前の読み（ReadOnly の相）から出てから書く
    dut.enable.value, dut.period.value, dut.high.value, dut.edge_sel.value = int(enable), period, high, int(edge_sel)
    await ClockCycles(dut.clk, 40)                       # sys から cap_clk への受け渡しと、丸めの段を待つ


async def sample(dut, n):
    """cap_clk の立ち上がりごとの out_q を n 個。"""
    out = []
    for _ in range(n):
        await RisingEdge(dut.cap_clk)
        await ReadOnly()
        out.append(int(dut.out_q.value))
    return out


def check_pattern(bits, period, high):
    """周期 period・1 が high 個の連続（巡回して連続）。位相は問わない。"""
    assert len(bits) >= 4 * period
    assert all(bits[i] == bits[i + period] for i in range(len(bits) - period)), bits[:3 * period]
    one_period = bits[:period]
    assert sum(one_period) == high, one_period
    changes = sum(one_period[i] != one_period[(i + 1) % period] for i in range(period))
    assert changes == 2, one_period                          # 1 の連続が 1 つ


@cocotb.test()
async def square_wave_has_the_period_and_the_high_time(dut):
    await setup(dut)
    for period, high in ((2, 1), (3, 1), (3, 2), (7, 3), (30, 15), (300, 1), (300, 299)):
        await configure(dut, enable=True, period=period, high=high)
        await ReadOnly()
        assert dut.out_oe.value == 1
        bits = await sample(dut, 4 * period + 2)
        check_pattern(bits, period, high)


@cocotb.test()
async def out_of_range_values_are_clamped_not_stuck(dut):
    # period < 2 は 2、4095 超は 4095（12 ビット）、high = 0 は 1、high >= period は period - 1（出力が固まらない）
    await setup(dut)
    for period, high, want_period, want_high in ((0, 0, 2, 1), (1, 1, 2, 1), (5, 0, 5, 1), (5, 5, 5, 4), (5, 9, 5, 4),
                                                  (5000, 1, 4095, 1), (65535, 4000, 4095, 4000)):
        await configure(dut, enable=True, period=period, high=high)
        check_pattern(await sample(dut, 4 * want_period + 2), want_period, want_high)


@cocotb.test()
async def disabled_output_is_low_and_not_driven(dut):
    await setup(dut)
    await configure(dut, enable=True, period=3, high=1)
    await configure(dut, enable=False, period=3, high=1)
    await ReadOnly()
    assert dut.out_oe.value == 0 and dut.out_q.value == 0                 # 出さない（Hi-Z にするのは oe）
    await RisingEdge(dut.clk)


async def edge_residues(dut, cycles):
    """out_q が変わった時刻の、cap_clk の周期（4 ns）での余り（立ち上がりからの位相）の集合。"""
    seen = set()

    async def watch():
        while True:
            await Edge(dut.out_q)
            seen.add(get_sim_time("ps") % CAP_PS)
    task = cocotb.start_soon(watch())
    await ClockCycles(dut.cap_clk, cycles)
    task.cancel()
    return seen


@cocotb.test()
async def falling_edge_launch_is_half_a_cap_cycle_later(dut):
    # 位相が固定なので、立ち上がり・立ち下がりの 2 通りで測れるように、edge_sel = 1 は半周期（2 ns）遅れて変わる
    await setup(dut)
    await configure(dut, enable=True, period=7, high=3, edge_sel=0)
    rising = await edge_residues(dut, 70)
    await configure(dut, enable=True, period=7, high=3, edge_sel=1)
    falling = await edge_residues(dut, 70)
    assert len(rising) == 1 and len(falling) == 1, (rising, falling)
    assert (next(iter(falling)) - next(iter(rising))) % CAP_PS == CAP_PS // 2
    check_pattern(await sample(dut, 30), 7, 3)                            # 値の並びは同じ


@cocotb.test()
async def reset_stops_driving_the_pin_and_the_pattern_comes_back_after_it(dut):
    # CLAUDE.md の規約: リセットは同期・アクティブハイの rst。リセット中は、ピンを駆動しない（oe = 0）・出力は 0。解除後は、設定のまま再開する
    await setup(dut)
    await configure(dut, enable=True, period=7, high=3)
    await ReadOnly()
    assert dut.out_oe.value == 1
    await RisingEdge(dut.clk)
    dut.rst.value = 1
    await ClockCycles(dut.clk, 4)
    await ReadOnly()
    assert dut.out_oe.value == 0 and dut.out_q.value == 0
    await RisingEdge(dut.clk)
    dut.rst.value = 0
    await ClockCycles(dut.clk, 8)
    await ReadOnly()
    assert dut.out_oe.value == 1
    check_pattern(await sample(dut, 30), 7, 3)
