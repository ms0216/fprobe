"""端子 → ISERDESE2 の模型 → la_lane_capture の端から端のテストベンチ（tests/rtl/la_lane_e2e_harness.vhd）。

細かいサンプルの辺 i（時刻 t0 + i × TC/2。clk の立ち上がり e と同時の辺が i = N × e）ごとに端子の値を置く:
ch 0 = 試験の信号（段・脈）、ch 1〜3 = 辺の番号 mod 8（欠け・重複・順の確認）。
タイムベース time は clk の立ち上がり e の後に e（関連クロックの記録と同じ数え方）。
確かめること（設計 2026-11-01-la-resolution-design.md §4.1）:
- ピンが辺 n の後に変わったとき、n = trigger_time × N + trigger_lane − latency（申告の latency と一致）。n を 2N + 1 通り動かし、
  レーンの境・周期の境をまたぐ。
- 記録の (trigger_index, trigger_lane) のサンプルが変化の後の最初のサンプル（辺 n + 1）で、記録は start_index から欠けも重複もない。
- 立ち下がり・値一致・外部トリガ（その次の語のレーン 0）・プリトリガ。"""

import os

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ReadOnly, RisingEdge, Timer

N = int(os.environ["LANE_N"])
WIDTH = 4
DEPTH = 32
TC_PS = 6000
TD_PS = TC_PS * N // 2
BENCH = None                    # いまの試験の Bench（check_capture が源の時刻を読む）


class Bench:
    def __init__(self, dut):
        self.dut = dut
        self.ch0 = lambda i: 0          # 辺 i のサンプルの ch 0
        self.e = -1                     # 最後の clk の立ち上がりの番号

    async def drive_pins(self):
        i = 0
        while True:
            self.dut.pins.value = (self.ch0(i) & 1) | (i % 8) << 1
            await Timer(TC_PS // 2, unit="ps")
            i += 1

    async def drive_time(self):
        while True:
            await RisingEdge(self.dut.clk)
            self.e += 1
            self.dut.time.value = self.e

    async def watch_triggered(self):
        """triggered が 0 → 1 になった周期の time（fprobe_core のクロストリガの源 bit0 のパルスの周期 S）。"""
        prev = 0
        while True:
            await RisingEdge(self.dut.clk)
            await ReadOnly()
            now = int(self.dut.triggered.value)
            if now and not prev:
                self.source_time = int(self.dut.time.value)
            prev = now


async def setup(dut):
    for name in ("run", "pretrigger", "trig_mask", "trig_value", "trig_rise", "trig_fall", "ext_trigger", "rd_addr",
                 "pins", "time"):
        getattr(dut, name).value = 0
    dut.rst.value = 1
    dut.clk.value = 0
    dut.clk_fast.value = 0
    global BENCH
    b = BENCH = Bench(dut)
    await Timer(TD_PS, unit="ps")
    # 辺 i の区間（i × TC/2 − TC/4 から）: 端子の駆動を TC/4 早く始め、クロックは t0 で両方立ち上げる
    cocotb.start_soon(b.drive_pins())
    await Timer(TC_PS // 4, unit="ps")
    cocotb.start_soon(b.drive_time())
    cocotb.start_soon(b.watch_triggered())
    cocotb.start_soon(Clock(dut.clk_fast, TC_PS, unit="ps").start())
    cocotb.start_soon(Clock(dut.clk, TD_PS, unit="ps").start())
    for _ in range(6):
        await RisingEdge(dut.clk)
    dut.rst.value = 0
    for _ in range(4):
        await RisingEdge(dut.clk)
    return b


async def capture(dut, b, *, step_after=None, ch0=None, pretrigger=4, mask=0, value=0, rise=0, fall=0, ext_after=None):
    """run を立て、トリガ待ちになってから ch 0 を変え（step_after = 辺の番号の、run の後の位置）、完了を待つ。"""
    dut.run.value = 0
    await RisingEdge(dut.clk)
    dut.pretrigger.value = pretrigger
    dut.trig_mask.value, dut.trig_value.value, dut.trig_rise.value, dut.trig_fall.value = mask, value, rise, fall
    await RisingEdge(dut.clk)
    dut.run.value = 1
    for _ in range(pretrigger + 8):
        await RisingEdge(dut.clk)
    await ReadOnly()
    assert int(dut.capturing.value) and not int(dut.triggered.value)
    await RisingEdge(dut.clk)
    n = None
    if step_after is not None:
        n = N * (b.e + 3) + step_after               # これからの辺: ピンは辺 n の後に変わる
        b.ch0 = ch0(n)
    if ext_after is not None:
        for _ in range(ext_after):
            await RisingEdge(dut.clk)
        await Timer(1, unit="ps")                    # drive_time がこの辺の後の time を置いた後
        b.ext_time = int(dut.time.value)             # 外部トリガが 1 の周期の time（その周期の頭 = 細かい辺 N × time）
        dut.ext_trigger.value = 1
        await RisingEdge(dut.clk)
        dut.ext_trigger.value = 0
    for _ in range(DEPTH * 2 + 20):
        await RisingEdge(dut.clk)
        await ReadOnly()
        if int(dut.done.value):
            break
    else:
        raise AssertionError("捕捉が終わらない")
    await RisingEdge(dut.clk)
    return n


async def read_samples(dut):
    """start_index から時間順に DEPTH × N 個のサンプル。"""
    start = int(dut.start_index.value)
    words = []
    for w in range(DEPTH):
        dut.rd_addr.value = (start + w) % DEPTH
        await RisingEdge(dut.clk)
        await RisingEdge(dut.clk)
        await ReadOnly()
        words.append(int(dut.rd_data.value))
        await RisingEdge(dut.clk)
    return [word >> (k * WIDTH) & (2 ** WIDTH - 1) for word in words for k in range(N)], start


async def check_capture(dut, n, before, after):
    latency = int(dut.latency.value)
    assert latency == N * 4 + (N - 3)            # 申告: LANES × (PIPE_CYCLES + 1) + iserdes_newest_age
    t, lane = int(dut.trigger_time.value), int(dut.trigger_lane.value)
    if n is not None:
        assert t * N + lane - latency == n, f"n={n}: time={t} lane={lane} latency={latency}"
        # クロストリガの源（時刻の表 la_lanes_xtrig。S1b の B7）: n は S × N − delay 以上 S × N − delay + span 未満
        from fprobe_gw.timing import LA_XTRIG_DELAY, la_lane_latency
        assert latency == la_lane_latency(N, N - 3)
        delay, span = la_lane_latency(N, N - 3) + LA_XTRIG_DELAY * N, N
        s_n = BENCH.source_time * N
        assert s_n - delay <= n < s_n - delay + span, (n, BENCH.source_time, delay)
    samples, start = await read_samples(dut)
    pos = ((int(dut.trigger_index.value) - start) % DEPTH) * N + lane
    counters = [s >> 1 for s in samples]
    for a, c in zip(counters, counters[1:]):
        assert c == (a + 1) % 8, "記録に欠けか重複がある"
    if n is not None:
        assert counters[pos] == (n + 1) % 8, "トリガのサンプルが辺 n + 1 でない"
    assert samples[pos] & 1 == after and samples[pos - 1] & 1 == before
    return pos


@cocotb.test()
async def rise_at_every_lane(dut):
    b = await setup(dut)
    for phase in range(2 * N + 1):
        n = await capture(dut, b, step_after=phase, ch0=lambda n: (lambda i: int(i > n)), rise=1)
        pos = await check_capture(dut, n, before=0, after=1)
        assert pos >= 4 * N                       # プリトリガ 4 語
        b.ch0 = lambda i: 0


@cocotb.test()
async def fall_and_value_match(dut):
    b = await setup(dut)
    for phase in (0, N - 1, N):
        b.ch0 = lambda i: 1
        n = await capture(dut, b, step_after=phase, ch0=lambda n: (lambda i: int(i <= n)), fall=1)
        await check_capture(dut, n, before=1, after=0)
    for phase in (1, N + 2):
        b.ch0 = lambda i: 0
        n = await capture(dut, b, step_after=phase, ch0=lambda n: (lambda i: int(i > n)), mask=1, value=1)
        await check_capture(dut, n, before=0, after=1)


@cocotb.test()
async def short_pulse_is_found_at_its_lane(dut):
    # 1 サンプルだけの脈（辺 n + 1 だけ 1）: 立ち上がりのトリガが、その 1 サンプルのレーンで成立する
    b = await setup(dut)
    for phase in (0, 3, N - 1):
        b.ch0 = lambda i: 0
        n = await capture(dut, b, step_after=phase, ch0=lambda n: (lambda i: int(i == n + 1)), rise=1)
        pos = await check_capture(dut, n, before=0, after=1)
        samples, _ = await read_samples(dut)
        assert samples[pos + 1] & 1 == 0


@cocotb.test()
async def external_trigger_lands_on_the_sample_after_its_cycle_start(dut):
    """時刻の正しさ S1b の N3: 外部トリガが 1 の sys の周期 X（頭 = 細かい辺 N × X）の直後のサンプルにトリガが付く:
    n = trigger_time × N + trigger_lane − latency = N × X（ピンの変化の n と同じ式・同じ意味）。レーンは IN_AGE mod N。
    外部トリガを入れる周期を N 通り以上ずらしても同じ。"""
    b = await setup(dut)
    for ext_after in range(3, 3 + N + 1):
        # パターンは決して成り立たない形（ch 0 が 1 かつ立ち下がり）、外部トリガだけで捕捉する
        await capture(dut, b, ext_after=ext_after, mask=1, value=1, fall=1)
        t, lane, latency = int(dut.trigger_time.value), int(dut.trigger_lane.value), int(dut.latency.value)
        assert t * N + lane - latency == N * b.ext_time, (ext_after, t, lane, latency, b.ext_time)
        assert lane == (N - 3) % N                   # IN_AGE = iserdes_newest_age = N − 3
        await check_capture(dut, None, before=0, after=0)
