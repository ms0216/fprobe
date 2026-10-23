"""deep_axi_harness: ロジアナの深いバッファ（la_deep_buffer）→ axi_mem_port → AXI3 のメモリの模型（READY・VALID を
ランダムに遅らせる）。PYNQ の P3（fprobe_pynq_deep）のつなぎを、再起動して実機で試す前に端から端まで確かめる。
取り込み・読み戻しの手順と確かめ方は la_deep_buffer_tb（DDR3 の mem_port の模型）と同じ。"""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge

from fprobe.la import decode_beats, trigger_sample
from tests.tb.axi3_slave_mem import Axi3SlaveMem
from tests.tb.la_deep_buffer_tb import CAP, capture, read_bytes, stream_until

BASE = 0x1E00_0000


async def setup(dut, stall=0.3):
    for name in ("rst", "probes", "ext_trigger", "time", "rate", "trig_mask", "trig_value", "trig_rise",
                 "trig_fall", "pw_enable", "pw_event", "run", "rle", "stream", "post_beats", "pretrigger", "read",
                 "read_first", "read_count", "ev_enable", "ev_valid", "ev_data", "read_region"):
        getattr(dut, name).value = 0
    dut.out_ready.value = 1
    dut.base.value = BASE
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    mem = Axi3SlaveMem(dut, dut.clk, stall=stall)
    dut.rst.value = 1
    await ClockCycles(dut.clk, 5)
    dut.rst.value = 0

    async def counter():
        value = 0
        while True:
            await RisingEdge(dut.clk)
            value = (value + 1) & 0xFFFF
            dut.probes.value = value
            dut.time.value = int(dut.time.value) + 1
    cocotb.start_soon(counter())
    return mem


@cocotb.test()
async def raw_capture_through_axi_wraps_and_reads_back_in_time_order(dut):
    mem = await setup(dut)
    beat, slot, written = await capture(dut, rle=False, rate=0, post=100, value=0x3000)
    assert written == CAP
    pre = CAP - 101
    first = (beat - pre) % CAP
    data = await read_bytes(dut, first, CAP)
    samples = decode_beats(data, rle=False)
    assert samples[trigger_sample(data, False, pre, slot)] == 0x3000
    assert all((b - a) & 0xFFFF == 1 for a, b in zip(samples, samples[1:]))   # 抜けも重複もない
    assert int(dut.mem_errors.value) == 0
    # 書いた先は base からの番地（DDR の予約した領域の中だけ）
    words = sorted(mem.mem)
    assert words[0] * 8 >= BASE and words[-1] * 8 < BASE + 2 ** 10 * 16


@cocotb.test()
async def rle_capture_through_axi(dut):
    await setup(dut, stall=0.5)
    beat, slot, written = await capture(dut, rle=True, rate=3, post=10, value=0x2000, mask=0xFF00)
    first = (beat - min(written - 11, 50)) % CAP
    count = (beat - first) % CAP + 11
    data = await read_bytes(dut, first, count)
    samples = decode_beats(data, rle=True)
    assert all((b - a) & 0xFFFF == 4 for a, b in zip(samples, samples[1:]))    # 4 周期ごとのサンプル
    assert int(dut.mem_errors.value) == 0


@cocotb.test()
async def stream_through_axi_survives_host_stalls_without_gaps(dut):
    # 流し続けの間は書き（取り込み）と読み（送り出し）が交互に AXI を使う。読みは書きの B を待つので書きも待たされる
    # （axi_mem_port の説明）。8 周期ごとのサンプルで、受け手が時々止まっても欠け・重複・溢れが無いこと
    await setup(dut, stall=0.3)
    dut.rle.value, dut.rate.value, dut.stream.value = 0, 7, 1
    dut.trig_mask.value, dut.trig_value.value = 0, 0
    dut.run.value = 1
    data = await stream_until(dut, beats=2200, stall=lambda n: (n // 37) % 5 == 0 or 60_000 <= n < 80_000)
    samples = decode_beats(data, False)
    assert len(samples) >= 2200 * 8
    gaps = [(i, a, b) for i, (a, b) in enumerate(zip(samples[1:], samples[2:])) if (b - a) & 0xFFFF != 8]
    assert not gaps, (len(samples), gaps[:5])
    assert dut.overrun.value == 0 and int(dut.mem_errors.value) == 0


@cocotb.test()
async def trigger_time_is_la_deep_delay_after_the_trigger_sample(dut):
    # setup のカウンタ: probes と time を同じ辺で進める。トリガのサンプル（値 0x3000）の time と、報告の trigger_time の差
    # = 深いバッファの入り口から時刻の付くまでの遅れ（fprobe_gw.caps.LA_DEEP_DELAY）
    from fprobe_gw.caps import LA_DEEP_DELAY
    await setup(dut, stall=0.0)
    await ClockCycles(dut.clk, 3)
    p0, t0 = int(dut.probes.value), int(dut.time.value)
    await capture(dut, rle=False, rate=0, post=100, value=0x3000)
    tt = int(dut.trigger_time.value)
    t_true = t0 + ((0x3000 - p0) % 0x10000)
    t_true += (tt - t_true) // 0x10000 * 0x10000
    assert tt - t_true == LA_DEEP_DELAY
