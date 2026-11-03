"""端子 → ISERDESE2 の模型 → la_deep_buffer（LANES = 8、PS が DDR のリングを読む垂れ流し）の端から端のテストベンチ
（tests/rtl/la_deep_lanes_harness.vhd。設計 docs/superpowers/specs/2026-11-01-la-deep-fast-design.md §8）。

細かいサンプルの辺 i（clk の立ち上がり e と同時の辺が i = 8e）ごとに端子の値を置く: ch 0 = 試験の信号（乱数の長さで反転）、
ch 1〜3 = 辺の番号 mod 8（欠け・重複・順の確認。RLE の試験では 0）。タイムベース time は clk の立ち上がり e の後に e。
PS の役（このテストが演じる）が、公開された書き位置までメモリのモデルのリングから読み、読み終えた位置を返す。

確かめること: ホストの解読（fprobe.la）で戻した流れの細かいサンプル s が、辺 n + 1 のサンプルであること
（n = start_time × 8 + s × 2^step × (rate + 1) − latency。la_lane_encoder の式）。欠けも重複も無い（ch 1〜3 の数え）。
RAW の ch の詰め（16・4・1 ch）・サンプルの間隔、RLE（まばらな変化・毎サンプルの変化で溢れ）、リングの使用量の申告。"""

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge, Timer

from fprobe.la import _expand_words, decode_beats

N = 8
TC_PS = 2500                     # 細かいサンプルの辺の 2 つ分（cap_clk の周期）
TD_PS = TC_PS * N // 2           # clk の周期
LA_RING = 1024 - 64              # ADDR_BITS = 10 のロジアナのリング


class Pattern:
    """ch 0 の値: 辺の番号 i → 0/1。乱数の長さ（lo〜hi 辺）ごとに反転する。"""

    def __init__(self, seed, lo, hi, limit=1 << 22):
        rng = random.Random(seed)
        self.bits = bytearray(limit)
        i, v = 0, 0
        while i < limit:
            run = rng.randint(lo, hi)
            self.bits[i:i + run] = bytes([v]) * min(run, limit - i)
            i += run
            v ^= 1

    def __call__(self, i):
        return self.bits[i] if i >= 0 else 0


class Bench:
    def __init__(self, dut):
        self.dut = dut
        self.ch0 = lambda i: 0
        self.counter = True
        self.e = -1

    async def drive_pins(self):
        i = 0
        while True:
            self.dut.pins.value = (self.ch0(i) & 1) | ((i % 8) << 1 if self.counter else 0)
            await Timer(TC_PS // 2, unit="ps")
            i += 1

    async def drive_time(self):
        while True:
            await RisingEdge(self.dut.clk)
            self.e += 1
            self.dut.time.value = self.e


class MemAndPs:
    """メモリ（mem_port: 書き込みだけ使う。ときどき待たせる）と PS の役（every 周期ごとに書き位置まで読む。stall が真の間は読まない）。"""

    def __init__(self, dut, *, every=64, stall=lambda cycle: False):
        self.dut, self.every, self.stall = dut, every, stall
        self.memory = {}
        self.la = bytearray()
        self.paused = False
        self.rng = random.Random(5)
        cocotb.start_soon(self._mem())
        self.task = cocotb.start_soon(self._ps())

    async def _mem(self):
        dut = self.dut
        while True:
            dut.cmd_ready.value = int(self.rng.random() >= 0.2)
            await ReadOnly()
            if int(dut.cmd_ready.value) and int(dut.cmd_valid.value) and int(dut.cmd_write.value):
                self.memory[int(dut.cmd_addr.value)] = int(dut.cmd_data.value)
            await RisingEdge(dut.clk)

    async def _ps(self):
        dut = self.dut
        cycle = 0
        while True:
            await ClockCycles(dut.clk, self.every)
            cycle += self.every
            if self.stall(cycle) or self.paused:
                continue
            await ReadOnly()
            written = int(dut.ps_la_written.value)
            await RisingEdge(dut.clk)
            have = len(self.la) // 16
            if written > have:
                self.la += b"".join(self.memory.get((have + k) % LA_RING, 0).to_bytes(16, "little")
                                    for k in range(written - have))
                dut.ps_la_consumed.value = written


async def setup(dut):
    for name in ("run", "rle", "step", "pack", "first_ch", "rate", "ps_la_consumed", "pins", "time", "rd_valid", "rd_data", "cmd_ready"):
        getattr(dut, name).value = 0
    dut.rst.value = 1
    dut.clk.value = 0
    dut.clk_fast.value = 0
    b = Bench(dut)
    await Timer(TD_PS, unit="ps")
    cocotb.start_soon(b.drive_pins())
    await Timer(TC_PS // 4, unit="ps")
    cocotb.start_soon(b.drive_time())
    cocotb.start_soon(Clock(dut.clk_fast, TC_PS, unit="ps").start())
    cocotb.start_soon(Clock(dut.clk, TD_PS, unit="ps").start())
    for _ in range(6):
        await RisingEdge(dut.clk)
    dut.rst.value = 0
    for _ in range(4):
        await RisingEdge(dut.clk)
    return b


async def stream(dut, ps, *, cycles, rle=0, step=0, pack=0, rate=0, first=0):
    """垂れ流しを cycles 周期流して止め、PS が全部読み終えるまで待つ。読んだバイト列を返す。
    PS の役は、新しい流れの書き位置が公開されるまで（16 周期ごと）読まない（前の流れの総数で読まない。中継の世代の代わり）。"""
    ps.paused = True
    dut.ps_la_consumed.value = 0
    dut.rle.value, dut.step.value, dut.pack.value, dut.rate.value, dut.first_ch.value = rle, step, pack, rate, first
    await ClockCycles(dut.clk, 4)                  # 端子の値を替えた後、模型の中の古い辺が出てから始める
    dut.run.value = 1
    await ClockCycles(dut.clk, 40)
    ps.la = bytearray()
    ps.paused = False
    await ClockCycles(dut.clk, cycles - 40)
    dut.run.value = 0
    for _ in range(20_000):
        await RisingEdge(dut.clk)
        await ReadOnly()
        if int(dut.reading.value) == 0:
            break
    else:
        raise AssertionError(f"reading stays 1: ps read {len(ps.la) // 16} of {int(dut.ps_la_written.value)}")
    await RisingEdge(dut.clk)
    return bytes(ps.la)


def edge_of(dut, s, d):
    """流れの細かいサンプル s（2^step × (rate + 1) = d ごと）が取った辺（= n + 1）。"""
    return int(dut.start_time.value) * N + s * d - int(dut.latency.value) + 1


def check_raw(dut, b, data, *, pack, d, first=0):
    samples = list(_expand_words(decode_beats(data, False), pack))
    assert len(samples) > 1000
    assert int(dut.start_valid.value) == 1 and int(dut.latency.value) == N + (N - 3)
    # 時刻の表の申告（TLV TIMING の la_deep_lanes。時刻の正しさ S1b の A9）が回路の latency と同じ
    from fprobe_gw.designs import pynq_z2_iserdes_newest_age
    from fprobe_gw.timing import la_deep_lane_latency
    assert int(dut.latency.value) == la_deep_lane_latency(N, pynq_z2_iserdes_newest_age(N))
    width = 16 >> pack
    for s, v in enumerate(samples):
        i = edge_of(dut, s, d)
        want = b.ch0(i) | (i % 8) << 1
        assert v == want >> first & ((1 << width) - 1), f"s={s} i={i} v={v:#x} want={want:#x} n={len(samples)} t0={int(dut.start_time.value)}"
    return samples


@cocotb.test()
async def raw_one_channel_at_800_msps_keeps_every_sample_on_the_fine_grid(dut):
    b = await setup(dut)
    ps = MemAndPs(dut)
    b.ch0 = Pattern(1, 1, 40)
    data = await stream(dut, ps, cycles=6000, pack=4)              # 1 ch: 1 拍 = 128 サンプル = 16 周期
    assert int(dut.overrun.value) == 0
    samples = check_raw(dut, b, data, pack=4, d=1)
    assert len(samples) >= (6000 - 40) * N


@cocotb.test()
async def raw_one_channel_from_another_first_channel(dut):
    # ch 1（辺の番号 mod 8 の最下位）だけを詰める: 毎サンプル反転 = 欠けも重複も無い 800 MS/s の 1 ch
    b = await setup(dut)
    ps = MemAndPs(dut)
    data = await stream(dut, ps, cycles=3000, pack=4, first=1)
    assert int(dut.overrun.value) == 0
    samples = check_raw(dut, b, data, pack=4, d=1, first=1)
    assert all(a != c for a, c in zip(samples, samples[1:]))


@cocotb.test()
async def raw_four_channels_and_sixteen_channels(dut):
    b = await setup(dut)
    ps = MemAndPs(dut, every=16)
    b.ch0 = Pattern(2, 1, 9)
    data = await stream(dut, ps, cycles=3000, pack=2)              # 4 ch: 1 拍 = 32 サンプル = 4 周期
    assert int(dut.overrun.value) == 0
    check_raw(dut, b, data, pack=2, d=1)
    # 16 ch・800 MS/s は 1 周期に 1 拍（1.6 GB/s）: 書き込みが追いつかず溢れる（事実として立つ）。出た分は先頭から正しい
    data = await stream(dut, ps, cycles=3000, pack=0)
    assert int(dut.overrun.value) == 1
    check_raw(dut, b, data, pack=0, d=1)


@cocotb.test()
async def raw_sample_interval_steps_and_word_rate(dut):
    b = await setup(dut)
    ps = MemAndPs(dut)
    b.ch0 = Pattern(3, 2, 60)
    for step, rate in ((1, 0), (2, 0), (3, 0), (3, 2)):
        data = await stream(dut, ps, cycles=4000 * (rate + 1), step=step, pack=4, rate=rate)
        assert int(dut.overrun.value) == 0, (step, rate)
        check_raw(dut, b, data, pack=4, d=(1 << step) * (rate + 1))


@cocotb.test()
async def rle_keeps_every_change_on_the_fine_grid(dut):
    b = await setup(dut)
    b.counter = False
    ps = MemAndPs(dut)
    b.ch0 = Pattern(4, 1, 300)                                    # 平均 150 辺に 1 回の変化（1 細かいサンプルの脈も含む）
    data = await stream(dut, ps, cycles=8000, rle=1)
    assert int(dut.overrun.value) == 0
    samples = list(decode_beats(data, True))
    assert len(samples) > 8000 * N - 2000
    for s, v in enumerate(samples):
        i = edge_of(dut, s, 1)
        assert v == b.ch0(i), f"s={s} i={i} n={len(samples)}"


@cocotb.test()
async def rle_of_a_change_at_every_sample_overflows_and_keeps_a_correct_prefix(dut):
    b = await setup(dut)
    b.counter = False
    ps = MemAndPs(dut)
    b.ch0 = lambda i: i & 1                                       # 400 MHz の方形波 = 毎サンプルの変化（1 周期に 8 レコード）
    data = await stream(dut, ps, cycles=2000, rle=1)
    assert int(dut.overrun.value) == 1
    samples = list(decode_beats(data, True))
    for s, v in enumerate(samples):
        assert v == b.ch0(edge_of(dut, s, 1))


@cocotb.test()
async def the_ring_use_is_reported(dut):
    # PS が読まない間、未読（書いた − 読んだ）が増え、最大も記録される。読むと未読は減るが最大は残る
    b = await setup(dut)
    ps = MemAndPs(dut, stall=lambda c: c < 4000)
    b.ch0 = Pattern(6, 1, 30)
    ps.la = bytearray()
    dut.ps_la_consumed.value = 0
    dut.rle.value, dut.step.value, dut.pack.value, dut.rate.value = 0, 0, 3, 0     # 2 ch: 1 拍 = 64 サンプル = 8 周期
    await RisingEdge(dut.clk)
    dut.run.value = 1
    await ClockCycles(dut.clk, 3000)
    await ReadOnly()
    unread, peak, written = int(dut.stat_unread.value), int(dut.stat_peak.value), int(dut.stat_written.value)
    assert 300 < unread <= written and peak >= unread - 4
    await ClockCycles(dut.clk, 3000)
    await ReadOnly()
    assert int(dut.stat_unread.value) < 64
    assert int(dut.stat_peak.value) >= peak
    assert int(dut.overrun.value) == 0
    await RisingEdge(dut.clk)
    dut.run.value = 0
