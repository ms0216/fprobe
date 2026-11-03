"""la_cap_domain のテストベンチ（2 クロック）: sys は cap の RATIO 倍の周期、同じ時刻の立ち上がりから始める（同じ MMCM の整数比）。
プローブ（外の 8 ピン）には、cap の周期ごとに増えるカウンタ（連続性の確認）か、cap の指定の周期にだけ立つ 1 本のパルス（位相の確認）を入れる。
仕様 docs/superpowers/specs/2026-10-30-la-capture-clock-design.md §4・§5"""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ReadOnly, RisingEdge, Timer

RATIO = int(__import__("os").environ.get("LA_CAP_RATIO", "3"))
CAP_NS = 4
DEPTH = 32


class Bench:
    def __init__(self, dut):
        self.dut = dut
        self.cap_n = 0                       # 立ち上がった cap のエッジの数（cap の周期の番号）
        self.counter_mode = True
        self.pulse_at = None                 # この cap の周期から pulse_len の間だけ ext_raw[0] を立てる
        self.pulse_len = 1
        self.static = None                   # None でなければ、この値を入れ続ける（内部の ch との揃えの確認）

    async def drive_pins(self):
        d = self.dut
        while True:
            await RisingEdge(d.cap_clk)
            self.cap_n += 1
            if self.static is not None:
                d.ext_raw.value = self.static
            elif self.counter_mode:
                d.ext_raw.value = self.cap_n & 0xFF
            else:
                inside = self.pulse_at is not None and self.pulse_at <= self.cap_n < self.pulse_at + self.pulse_len
                d.ext_raw.value = 1 if inside else 0

    async def drive_time(self):
        d = self.dut
        while True:
            await RisingEdge(d.clk)
            d.time.value = int(d.time.value) + 1


async def setup(dut):
    for name in ("run", "rate", "pretrigger", "trig_mask", "trig_value", "trig_rise", "trig_fall", "ext_trigger",
                 "rd_addr", "sys_bit_a", "sys_bit_b", "filter_channels", "filter_cycles", "pulse_mode", "pulse_level",
                 "pulse_channel", "pulse_min", "pulse_max", "ext_raw"):
        getattr(dut, name).value = 0
    dut.time.value = 0
    dut.rst.value = 1
    cocotb.start_soon(Clock(dut.cap_clk, CAP_NS, unit="ns").start())
    cocotb.start_soon(Clock(dut.clk, CAP_NS * RATIO, unit="ns").start())
    for _ in range(4):
        await RisingEdge(dut.clk)
    dut.rst.value = 0
    await RisingEdge(dut.clk)
    b = Bench(dut)
    cocotb.start_soon(b.drive_pins())
    cocotb.start_soon(b.drive_time())
    for _ in range(40):                      # ピンのカウンタが動いてから始める（ピンから取り込みまで数周期の遅れがある）
        await RisingEdge(dut.cap_clk)
    return b


async def start_run(dut):
    """run を立て、前の結果の done が下がるのを待つ（捕捉が終わった状態は run = 0 でも残る仕様なので、古い結果を読まない）。"""
    await RisingEdge(dut.clk)
    dut.run.value = 1
    for _ in range(20):
        await RisingEdge(dut.clk)
        await ReadOnly()
        if not int(dut.done.value):
            return
    raise AssertionError("run を立てても done が下がらない")


async def wait_done(dut, timeout=4000):
    for _ in range(timeout):
        await RisingEdge(dut.clk)
        await ReadOnly()
        if int(dut.done.value):
            break
    else:
        raise AssertionError("捕捉が終わらない")
    await RisingEdge(dut.clk)


async def capture(dut, *, rate=0, pretrigger=0, mask=0, value=0, rise=0, fall=0, timeout=4000):
    dut.rate.value = rate
    dut.pretrigger.value = pretrigger
    dut.trig_mask.value = mask
    dut.trig_value.value = value
    dut.trig_rise.value = rise
    dut.trig_fall.value = fall
    await start_run(dut)
    await wait_done(dut, timeout)


async def read_buffer(dut, mask=0xFF):
    """la_mem_csr と同じ時間で読む: 番地を出して、2 周期後（sys）の rd_data（mask のビット）。"""
    out = []
    for a in range(DEPTH):
        dut.rd_addr.value = a
        await RisingEdge(dut.clk)
        await RisingEdge(dut.clk)
        await RisingEdge(dut.clk)
        await ReadOnly()
        out.append(int(dut.rd_data.value) & mask)
        await Timer(1, unit="ns")
    return out


async def stop(dut):
    dut.run.value = 0
    for _ in range(4):
        await RisingEdge(dut.clk)


@cocotb.test()
async def every_cap_cycle_is_sampled_with_no_gap(dut):
    b = await setup(dut)
    await capture(dut, rate=0, pretrigger=4, mask=0xFF, value=0x80)
    data = await read_buffer(dut)
    start = int(dut.start_index.value)
    ordered = [data[(start + i) % DEPTH] for i in range(DEPTH)]
    steps = [(ordered[i + 1] - ordered[i]) & 0xFF for i in range(DEPTH - 1)]
    assert steps == [1] * (DEPTH - 1), (RATIO, steps, ordered)         # cap の周期ごとに 1 サンプル、欠けない
    trig = int(dut.trigger_index.value)
    assert data[trig] == 0x80                                           # トリガのサンプルがトリガの値
    assert (trig - start) % DEPTH == 4                                  # プリトリガ 4 サンプル
    assert int(dut.triggered.value) == 1 and int(dut.done.value) == 1
    await stop(dut)


@cocotb.test()
async def the_sample_interval_is_counted_in_cap_cycles(dut):
    b = await setup(dut)
    await capture(dut, rate=2, pretrigger=2, mask=0, value=0)
    data = await read_buffer(dut)
    start = int(dut.start_index.value)
    ordered = [data[(start + i) % DEPTH] for i in range(DEPTH)]
    steps = [(ordered[i + 1] - ordered[i]) & 0xFF for i in range(DEPTH - 1)]
    dut._log.info("RATIO %d interval test: start %d trigger %d data %s", RATIO, start, int(dut.trigger_index.value),
                  " ".join("%02x" % x for x in data))
    assert steps == [3] * (DEPTH - 1), (RATIO, " ".join(map(str, steps)))   # rate = 2 → 3 cap 周期ごと
    await stop(dut)


@cocotb.test()
async def the_trigger_time_and_phase_follow_the_cap_cycle(dut):
    """ピンの 1 周期のパルスを cap の周期 n に入れる。トリガの時刻（sys の数）と位相から求めた (time × RATIO + phase) は、
    n と一定の差で並ぶ（n を RATIO × 2 通りずらして、すべての位相で確かめる）。差の値は固定の遅れ（段数）。"""
    b = await setup(dut)
    b.counter_mode = False
    offsets = []
    base = None
    for j in range(RATIO * 2):
        await stop(dut)
        await RisingEdge(dut.cap_clk)
        n_target = b.cap_n + RATIO * 12 + j                              # 余裕を持って先の周期
        b.pulse_at = n_target
        dut.rate.value = 0
        dut.pretrigger.value = 0
        dut.trig_mask.value = 0
        dut.trig_value.value = 0
        dut.trig_rise.value = 1                                          # ch0 の立ち上がり
        dut.trig_fall.value = 0
        await start_run(dut)
        await wait_done(dut, 2000)
        t = int(dut.trigger_time.value)
        ph = int(dut.trigger_phase.value)
        assert 0 <= ph < RATIO, ph
        offsets.append(t * RATIO + ph - n_target)
    assert len(set(offsets)) == 1, (RATIO, offsets)                      # 位相がずれると、n との差が動く
    dut._log.info("RATIO %d: (time x RATIO + phase) - cap cycle of the pin pulse = %d cap cycles (fixed)", RATIO, offsets[0])
    assert offsets[0] == int(dut.latency.value), (offsets[0], int(dut.latency.value))   # 固定の遅れ（同期化・判定の段 + 受け渡し）の申告


@cocotb.test()
async def run_can_be_restarted_and_stopped(dut):
    b = await setup(dut)
    await capture(dut, rate=0, pretrigger=2, mask=0xFF, value=0x40)
    first = int(dut.trigger_index.value)
    await stop(dut)
    assert int(dut.done.value) == 1 and int(dut.capturing.value) == 0      # 終わった結果は run = 0 でも残る
    await capture(dut, rate=0, pretrigger=2, mask=0xFF, value=0x41)
    data = await read_buffer(dut)
    assert data[int(dut.trigger_index.value)] == 0x41
    await stop(dut)


@cocotb.test()
async def the_pin_glitch_filter_runs_in_the_cap_domain(dut):
    """フィルタの周期は cap の周期（filter_cycles）。ch0 の 1 周期のひげは 3 周期のフィルタで落ち、5 周期の脈は通る。"""
    b = await setup(dut)
    b.counter_mode = False
    dut.filter_channels.value = 1
    dut.filter_cycles.value = 3
    dut.rate.value = 0
    dut.pretrigger.value = 0
    dut.trig_mask.value = 0
    dut.trig_value.value = 0
    dut.trig_rise.value = 1
    dut.trig_fall.value = 0
    for _ in range(10):                                                  # フィルタの中が落ち着く
        await RisingEdge(dut.clk)
    await start_run(dut)
    await RisingEdge(dut.cap_clk)
    b.pulse_at, b.pulse_len = b.cap_n + RATIO * 12, 1                    # 1 周期のひげ
    for _ in range(RATIO * 40):
        await RisingEdge(dut.clk)
        await ReadOnly()
        assert int(dut.triggered.value) == 0, "ひげがフィルタを通った"
    await Timer(1, unit="ns")
    await stop(dut)
    await RisingEdge(dut.cap_clk)
    b.pulse_at, b.pulse_len = b.cap_n + RATIO * 12, 5                    # 5 周期の脈は通る
    await start_run(dut)
    await wait_done(dut)
    assert int(dut.triggered.value) == 1
    await stop(dut)


@cocotb.test()
async def internal_channels_land_on_the_same_sample_as_a_pin_changed_on_the_same_edge(dut):
    """R2（docs/superpowers/specs/2026-11-01-time-accuracy-design.md）: sys の FF（sys_bit_a = ch 8、sys_bit_b = ch 9）が変わる辺と、
    その辺の直後に変わったピン（ch 0）は、記録の上で同じサンプルに来る（内部の ch のずれ 0 = LA_CH_TIMING の 0）。
    sys の辺は cap の辺でもある（同じ MMCM の整数比）ので、cap の周期で揃う。"""
    b = await setup(dut)
    b.counter_mode = False
    for bit in (0, 1):
        await stop(dut)
        b.static = 0
        dut.sys_bit_a.value = 0
        dut.sys_bit_b.value = 0
        dut.rate.value = 0
        dut.pretrigger.value = 8
        dut.trig_mask.value = 0
        dut.trig_value.value = 0
        dut.trig_rise.value = 1                                          # ch0 の立ち上がり
        dut.trig_fall.value = 0
        for _ in range(10):
            await RisingEdge(dut.clk)
        await start_run(dut)
        for _ in range(12):                                              # プリトリガの 8 サンプルが溜まる
            await RisingEdge(dut.clk)
        await RisingEdge(dut.clk)                                        # sys の辺（= cap の辺）の直後に両方を変える
        b.static = 1
        dut.ext_raw.value = 1
        if bit == 0:
            dut.sys_bit_a.value = 1
        else:
            dut.sys_bit_b.value = 1
        await wait_done(dut)
        data = await read_buffer(dut, mask=0xFFFF)
        start = int(dut.start_index.value)
        ordered = [data[(start + i) % DEPTH] for i in range(DEPTH)]
        pin_at = next(i for i, v in enumerate(ordered) if v & 1)
        int_at = next(i for i, v in enumerate(ordered) if v >> (8 + bit) & 1)
        dut._log.info("RATIO %d bit %d: pin sample %d, internal sample %d", RATIO, bit, pin_at, int_at)
        assert int_at == pin_at, (RATIO, bit, pin_at, int_at, " ".join("%04x" % v for v in ordered))
        b.static = None


async def watch_triggered(dut, out: dict):
    """triggered（sys。fprobe_core のクロストリガの源 bit0）が 0 → 1 になった sys の周期の time を out["at"] に。"""
    prev = 0
    while True:
        await RisingEdge(dut.clk)
        await ReadOnly()
        now = int(dut.triggered.value)
        if now and not prev:
            out["at"] = int(dut.time.value)
        prev = now


@cocotb.test()
async def the_cap_cycle_number_is_ratio_times_the_timebase(dut):
    """辺と時刻の約束（時刻の正しさ S1b の §10.4 の 2）: sys の辺（time が T になる辺）は cap の辺 T × RATIO と同じ瞬間。
    ピンの cap の周期の番号 n（辺 n の後に変わった = n 以上 n + 1 未満）とタイムベースが同じ時間軸に載ることの前提。"""
    b = await setup(dut)
    for _ in range(5):
        await RisingEdge(dut.clk)
        await Timer(1, unit="ps")                     # drive_pins・drive_time がこの辺の数を数えた後
        assert b.cap_n == RATIO * int(dut.time.value), (RATIO, b.cap_n, int(dut.time.value))


@cocotb.test()
async def an_external_trigger_lands_on_the_sample_after_its_cycle_start(dut):
    """N3: 外部トリガが 1 の sys の周期 X（頭 = cap の辺 X × RATIO）→ トリガのサンプルの n = time × RATIO + phase − latency が
    X × RATIO（rate = 0。ピンが辺 X × RATIO の後に変わったときと同じ n）。rate > 0 なら X × RATIO 以後の最初のサンプル
    （X × RATIO は n − rate 以上 n 以下）。sys の周期の中の位相を全部通る。"""
    b = await setup(dut)
    b.counter_mode = False
    for rate in (0, 2):
        for wait in range(RATIO + rate + 1):
            await stop(dut)
            dut.rate.value, dut.pretrigger.value = rate, 2
            dut.trig_mask.value, dut.trig_value.value, dut.trig_rise.value, dut.trig_fall.value = 1, 1, 0, 1   # 成り立たない
            await start_run(dut)
            for _ in range(10 + wait):
                await RisingEdge(dut.clk)
            await Timer(1, unit="ps")
            x = int(dut.time.value)
            dut.ext_trigger.value = 1
            await RisingEdge(dut.clk)
            dut.ext_trigger.value = 0
            await wait_done(dut)
            n = int(dut.trigger_time.value) * RATIO + int(dut.trigger_phase.value) - int(dut.latency.value)
            assert n - rate <= x * RATIO <= n, (RATIO, rate, wait, x, n)
            if rate == 0:
                assert n == x * RATIO, (RATIO, x, n)
    dut.trig_fall.value = 0


@cocotb.test()
async def the_cross_trigger_source_is_declared_by_the_table(dut):
    """B7: ロジアナのトリガをクロストリガの源にしたとき、源のパルス（triggered の立ち上がりの sys の周期 S）とピンの cap の周期 n は
    時刻の表 la_cap_xtrig の範囲（n は S × RATIO − delay 以上 + span 未満）。ピンを cap の全部の位相で入れて、範囲の両端に来る。"""
    from fprobe_gw.timing import la_cap_xtrig
    b = await setup(dut)
    b.counter_mode = False
    seen: dict = {}
    cocotb.start_soon(watch_triggered(dut, seen))
    delay, span = la_cap_xtrig(RATIO)
    offsets = set()
    for j in range(RATIO * 2):
        await stop(dut)
        await RisingEdge(dut.cap_clk)
        n_target = b.cap_n + RATIO * 12 + j
        b.pulse_at = n_target
        dut.rate.value, dut.pretrigger.value = 0, 0
        dut.trig_mask.value, dut.trig_value.value, dut.trig_rise.value, dut.trig_fall.value = 0, 0, 1, 0
        await start_run(dut)
        await wait_done(dut, 2000)
        s = seen["at"]
        assert s * RATIO - delay <= n_target < s * RATIO - delay + span, (RATIO, j, s, n_target, delay, span)
        offsets.add(s * RATIO - n_target)
    assert offsets == set(range(delay - span + 1, delay + 1)), (RATIO, sorted(offsets), delay, span)
