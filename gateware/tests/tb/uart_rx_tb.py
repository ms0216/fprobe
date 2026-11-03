"""uart_rx のテストベンチ（divisor = 8）。"""

import cocotb
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge

from tests.models.uart import uart_bits
from tests.tb.common import PulseCounter, StreamCollector, start_clock_and_reset

DIV = 8


async def setup(dut, *, ready=True):
    dut.divisor.value = DIV
    dut.rx.value = 1
    dut.out_ready.value = 1 if ready else 0
    await start_clock_and_reset(dut)
    await ClockCycles(dut.clk, 4)


async def drive(dut, levels):
    for level in levels:
        dut.rx.value = level
        await ClockCycles(dut.clk, DIV)


@cocotb.test()
async def receives_bytes_back_to_back(dut):
    await setup(dut)
    got = StreamCollector(dut, "out")
    for byte in (0x00, 0xFF, 0x3C):
        await drive(dut, uart_bits(byte))
    await ClockCycles(dut.clk, DIV * 2)
    assert got.items == [0x00, 0xFF, 0x3C]


@cocotb.test()
async def ignores_glitch_and_recovers_from_frame_error(dut):
    await setup(dut)
    got = StreamCollector(dut, "out")
    errors = PulseCounter(dut, dut.frame_error)
    # 半ビット未満のグリッチは開始ビットとみなさない
    dut.rx.value = 0
    await ClockCycles(dut.clk, DIV // 2 - 2)
    dut.rx.value = 1
    await ClockCycles(dut.clk, DIV * 12)
    # 停止ビットが 0 のフレーム → frame_error。線が low のまま続いても次へ進まない
    await drive(dut, uart_bits(0x55)[:-1] + [0, 0, 0])
    dut.rx.value = 1
    await ClockCycles(dut.clk, DIV * 3)
    # その後の正常なバイトは受信できる
    await drive(dut, uart_bits(0x81))
    await ClockCycles(dut.clk, DIV * 2)
    assert errors.count == 1
    assert got.items == [0x81]


@cocotb.test()
async def reports_overrun_and_keeps_latest(dut):
    await setup(dut, ready=False)
    overruns = PulseCounter(dut, dut.overrun)
    for byte in (0x11, 0x22):
        await drive(dut, uart_bits(byte))
    await ClockCycles(dut.clk, DIV)
    await ReadOnly()
    assert int(dut.out_valid.value) == 1
    assert int(dut.out_data.value) == 0x22
    assert overruns.count == 1


async def count_time(dut):
    """time を毎周期 1 進める（timebase と同じ: 立ち上がりの直後に変わる）。"""
    while True:
        await RisingEdge(dut.clk)
        dut.time.value = (int(dut.time.value) + 1) % (1 << 48)


@cocotb.test()
async def the_byte_time_is_the_start_bit_edge_whatever_the_divisor_and_the_wait(dut):
    """R3（docs/superpowers/specs/2026-11-01-time-accuracy-design.md）: バイトの時刻は開始ビットの辺を見つけた周期の time。
    時刻が T になる立ち上がりの直後に線を下げると、out_time = T + UART_RX_START_DELAY（fprobe_gw.timing）。divisor の偶数・奇数、
    受け手が取り出すまでの待ち（out_ready = 0 の間）に依らない。"""
    from fprobe_gw.timing import UART_RX_START_DELAY
    dut.time.value = 1000
    await setup(dut, ready=False)
    cocotb.start_soon(count_time(dut))
    for div, wait in ((8, 0), (7, 0), (9, 37), (8, 200)):
        dut.divisor.value = div
        await ClockCycles(dut.clk, 4 * div)
        for byte in (0xA5, 0x3C):
            await RisingEdge(dut.clk)
            await ReadOnly()
            t = int(dut.time.value)                       # この辺で count_time が書いた値（この周期の time）
            await RisingEdge(dut.clk)
            dut.rx.value = 0                              # 時刻が t + 1 になった辺の直後に開始ビット
            edge_t = t + 1
            for level in uart_bits(byte)[1:]:
                await ClockCycles(dut.clk, div)
                dut.rx.value = level
            await ClockCycles(dut.clk, div + wait)        # 停止ビットの後、取り出すまで wait 周期待つ
            await ReadOnly()
            assert int(dut.out_valid.value) == 1 and int(dut.out_data.value) == byte, (div, byte)
            assert int(dut.out_time.value) == edge_t + UART_RX_START_DELAY, (div, wait, int(dut.out_time.value), edge_t)
            await RisingEdge(dut.clk)
            dut.out_ready.value = 1
            await RisingEdge(dut.clk)
            dut.out_ready.value = 0


@cocotb.test()
async def the_host_window_contains_the_true_start_bit_edge(dut):
    """時刻の正しさ S1b（設計書 §10.4 の 2）: 周期の中の色々な位置（シミュレーションの ps）で開始ビットを下げ、ホストの式
    fprobe.timing.byte_edge_window が返す範囲 [t_lo, t_hi)（刻み）が、本当の辺の位置を含むこと（時刻 T の瞬間 = time が T に
    なる立ち上がり）。申告は時刻の表 fprobe_gw.timing の uart_rx（TLV TIMING と同じもの）。"""
    from types import SimpleNamespace

    from cocotb.triggers import Timer
    from cocotb.utils import get_sim_time
    from fprobe.caps import TimingPoint
    from fprobe.timing import byte_edge_window
    from fprobe_gw import timing as table
    from tests.tb.common import TimebaseClock

    p = next(p for p in table.timing_points(SimpleNamespace(la_channels=0, la_depth=0, mem_bytes=0, mem_test=False,
                                                             cap_ratio=1, la_lanes=1))
             if p.source == "uart_rx")
    caps = SimpleNamespace(timing={"uart_rx": TimingPoint("uart_rx", p.point, p.unit, p.delay, p.span, p.excludes)})
    await setup(dut, ready=True)
    tb = TimebaseClock(dut, width=48)
    await ClockCycles(dut.clk, 4)
    for offset_ps in (1, 3000, 5000, 9999):
        await RisingEdge(dut.clk)
        await Timer(offset_ps, unit="ps")
        dut.rx.value = 0
        t_ps = get_sim_time("ps")
        for level in uart_bits(0x5A)[1:]:
            await ClockCycles(dut.clk, DIV)
            dut.rx.value = level
        await ClockCycles(dut.clk, DIV)
        await ReadOnly()
        assert int(dut.out_data.value) == 0x5A
        w = byte_edge_window(caps, "uart_rx", int(dut.out_time.value))
        t_in = tb.tick_at(t_ps)
        assert w["t_lo"] <= t_in < w["t_hi"], (offset_ps, t_in, w)
        await ClockCycles(dut.clk, DIV * 2)
