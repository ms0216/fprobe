"""xtrig_out（クロストリガの動作: 出力の端子）のテストベンチ。COUNT = 4、WIDTH = 8、clk 10 ns。

time は 32 ビットのタイムベース（テストベンチが毎周期 1 ずつ進める）。
"""

import cocotb
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge

from tests.tb.common import start_clock_and_reset

PULSE, HOLD, WHILE = 0, 1, 2
LA, MATCH, HOST, SEQ, OBS, SMP = 1, 2, 4, 8, 16, 32
COUNT, WIDTH = 4, 8


class Out:
    def __init__(self, dut):
        self.dut = dut
        self.cycle = 0
        self.history: list[int] = []        # 周期ごとの oe（立ち上がり直後の値）
        self.cfg = [dict(enable=0, mode=0, level=1, any=0, mask=0, value=0, src=0, length=0, unit=0)
                    for _ in range(COUNT)]
        self.apply()

    async def run(self):
        time = 0
        while True:
            await RisingEdge(self.dut.clk)
            time = (time + 1) & 0xFFFFFFFF
            self.dut.time.value = time
            await ReadOnly()
            self.cycle += 1
            self.history.append(int(self.dut.oe.value))

    def apply(self):
        d = self.dut

        def pack(name, bits):
            return sum((c[name] & (1 << bits) - 1) << bits * k for k, c in enumerate(self.cfg))
        d.enable.value = pack("enable", 1)
        d.mode.value = pack("mode", 2)
        d.level.value = pack("level", 1)
        d.any.value = pack("any", 1)
        d.mask.value = pack("mask", WIDTH)
        d.value.value = pack("value", WIDTH)
        d.src.value = pack("src", 6)
        d.length.value = pack("length", 8)
        d.unit.value = pack("unit", 3)

    async def set(self, k, **cfg):
        self.cfg[k].update(cfg)
        self.apply()
        await RisingEdge(self.dut.clk)

    async def pins(self, value, hold=1):
        self.dut.pins.value = value
        await ClockCycles(self.dut.clk, hold)

    async def pulse_source(self, bits):
        self.dut.sources.value = bits
        await RisingEdge(self.dut.clk)
        self.dut.sources.value = 0

    def oe(self, k) -> int:
        return int(self.dut.oe.value) >> k & 1

    def high_cycles(self, k, since: int) -> int:
        return sum(v >> k & 1 for v in self.history[since:])


async def setup(dut):
    dut.pins.value = 0
    dut.sources.value = 0
    dut.time.value = 0
    out = Out(dut)
    await start_clock_and_reset(dut)
    cocotb.start_soon(out.run())
    await ClockCycles(dut.clk, 2)
    return out


async def cycles_until(out, k, level, limit=50):
    for n in range(1, limit + 1):
        await RisingEdge(out.dut.clk)
        await ReadOnly()
        if out.oe(k) == level:
            return n
    raise AssertionError(f"oe({k}) が {limit} 周期で {level} にならない")


@cocotb.test()
async def while_mode_follows_the_pattern_within_four_cycles(dut):
    out = await setup(dut)
    await out.set(1, enable=1, mode=WHILE, mask=0b10, value=0b10, level=1)
    assert out.oe(1) == 0
    dut.pins.value = 0b10
    assert await cycles_until(out, 1, 1) == 4          # 同期化 2 + 条件 1 + 駆動 1
    await RisingEdge(dut.clk)
    assert int(dut.o.value) >> 1 & 1 == 1
    dut.pins.value = 0
    assert await cycles_until(out, 1, 0) == 4
    assert out.high_cycles(0, 0) == 0 and out.high_cycles(2, 0) == 0   # ほかの出力は動かない


@cocotb.test()
async def any_matches_one_of_the_masked_pins(dut):
    out = await setup(dut)
    await out.set(0, enable=1, mode=WHILE, mask=0b0110, value=0b0110, any=1)
    for pins, expected in ((0b0000, 0), (0b0010, 1), (0b0100, 1), (0b0110, 1), (0b1001, 0)):
        await out.pins(pins, hold=6)
        await ReadOnly()
        assert out.oe(0) == expected, bin(pins)
        await RisingEdge(dut.clk)
    # all（any = 0）: 両方が一致したときだけ
    await out.set(0, any=0)
    for pins, expected in ((0b0010, 0), (0b0110, 1), (0b1110, 1)):
        await out.pins(pins, hold=6)
        await ReadOnly()
        assert out.oe(0) == expected, bin(pins)
        await RisingEdge(dut.clk)


@cocotb.test()
async def empty_mask_never_matches(dut):
    out = await setup(dut)
    await out.set(0, enable=1, mode=WHILE, mask=0, value=0)
    await out.pins(0xFF, hold=10)
    await out.pins(0x00, hold=10)
    assert out.high_cycles(0, 0) == 0


@cocotb.test()
async def pulse_from_a_source_has_the_exact_length_in_cycles(dut):
    out = await setup(dut)
    await out.set(2, enable=1, mode=PULSE, src=HOST, length=37, unit=0)
    start = out.cycle
    await out.pulse_source(HOST)
    await ClockCycles(dut.clk, 60)
    assert out.high_cycles(2, start) == 37
    # 別の源（ロジアナのトリガ）は選んでいないので動かない
    start = out.cycle
    await out.pulse_source(LA)
    await ClockCycles(dut.clk, 60)
    assert out.high_cycles(2, start) == 0


@cocotb.test()
async def sampler_match_source_is_bit5(dut):
    out = await setup(dut)
    await out.set(3, enable=1, mode=PULSE, src=SMP, length=9, unit=0)
    start = out.cycle
    await out.pulse_source(OBS)                                         # 隣の源（bit4）では動かない
    await ClockCycles(dut.clk, 20)
    assert out.high_cycles(3, start) == 0
    start = out.cycle
    await out.pulse_source(SMP)
    await ClockCycles(dut.clk, 20)
    assert out.high_cycles(3, start) == 9


@cocotb.test()
async def pulse_in_ticks_is_within_one_tick(dut):
    out = await setup(dut)
    await out.set(0, enable=1, mode=PULSE, src=SEQ, length=5, unit=1)     # 刻み 16 周期 × 5
    start = out.cycle
    await out.pulse_source(SEQ)
    await ClockCycles(dut.clk, 120)
    assert 4 * 16 < out.high_cycles(0, start) <= 5 * 16 + 1


@cocotb.test()
async def retrigger_during_a_pulse_reloads_the_length(dut):
    out = await setup(dut)
    await out.set(0, enable=1, mode=PULSE, src=MATCH, length=20)
    start = out.cycle
    await out.pulse_source(MATCH)
    await ClockCycles(dut.clk, 10)
    await out.pulse_source(MATCH)
    await ClockCycles(dut.clk, 40)
    assert out.high_cycles(0, start) == 10 + 1 + 20


@cocotb.test()
async def pulse_on_the_rising_edge_of_a_pattern(dut):
    out = await setup(dut)
    await out.set(3, enable=1, mode=PULSE, mask=1, value=1, length=8)
    start = out.cycle
    await out.pins(1, hold=40)                           # 成り立ったまま: 脈は 1 回だけ
    assert out.high_cycles(3, start) == 8
    await out.pins(0, hold=5)
    await out.pins(1, hold=20)
    assert out.high_cycles(3, start) == 16


@cocotb.test()
async def hold_stays_until_disabled(dut):
    out = await setup(dut)
    await out.set(1, enable=1, mode=HOLD, src=HOST, level=0)
    await out.pulse_source(HOST)
    await ClockCycles(dut.clk, 3)
    await ReadOnly()
    assert out.oe(1) == 1 and int(dut.o.value) >> 1 & 1 == 0   # level = 0: Low を駆動
    await ClockCycles(dut.clk, 100)
    await ReadOnly()
    assert out.oe(1) == 1
    await RisingEdge(dut.clk)
    await out.set(1, enable=0)
    await ClockCycles(dut.clk, 2)
    await ReadOnly()
    assert out.oe(1) == 0
    await RisingEdge(dut.clk)
    await out.set(1, enable=1)                           # 有効に戻しても、次の発火まで動かない
    await ClockCycles(dut.clk, 10)
    await ReadOnly()
    assert out.oe(1) == 0


@cocotb.test()
async def condition_already_true_at_enable_is_not_an_edge(dut):
    out = await setup(dut)
    await out.set(0, enable=0, mode=HOLD, mask=1, value=1)   # 条件は無効の間も追う
    await out.pins(1, hold=5)
    await out.set(0, enable=1)
    await ClockCycles(dut.clk, 10)
    await ReadOnly()
    assert out.oe(0) == 0
    await RisingEdge(dut.clk)
    await out.pins(0, hold=5)
    await out.pins(1, hold=6)
    await ReadOnly()
    assert out.oe(0) == 1


@cocotb.test()
async def disabled_outputs_never_drive(dut):
    out = await setup(dut)
    for k in range(COUNT):
        await out.set(k, enable=0, mode=WHILE, mask=1, value=1, src=HOST)
    start = out.cycle
    await out.pins(1, hold=10)
    await out.pulse_source(HOST)
    await ClockCycles(dut.clk, 10)
    assert all(v == 0 for v in out.history[start:])


@cocotb.test()
async def the_output_times_match_the_timing_table(dut):
    """時刻の表（fprobe_gw.timing の XTRIG_OUT_*、TLV TIMING の xtrig_out・xtrig_out_pins。時刻の正しさ S3 の D2）と、ホストの
    fprobe.timing.xtrig_out_window の式が回路と一致する: 源のパルスの周期 S → 出力の FF が S + 2 で変わり、脈の長さは
    刻み 16^unit の最初の刻みの位置で決まる（源の時刻から 1 つに決まる）。端子の並びの辺 T の直後の変化 → T + 4。
    （ボードの出力の段はこの部品の外。ここでは 0 段）"""
    from types import SimpleNamespace
    from fprobe.caps import TimingPoint
    from fprobe.timing import xtrig_out_window
    from fprobe_gw.timing import XTRIG_OUT_PIN_DELAY, XTRIG_OUT_SOURCE_DELAY
    caps = SimpleNamespace(dap_output_ticks=0, timing={
        "xtrig_out": TimingPoint("xtrig_out", "output_edge", "tick", -XTRIG_OUT_SOURCE_DELAY, 0, ("output_delay",))})
    out = await setup(dut)
    seen: list[tuple[int, int]] = []                       # (その周期の time, oe(0))

    async def monitor():
        while True:
            await RisingEdge(dut.clk)
            await ReadOnly()
            seen.append((int(dut.time.value), out.oe(0)))
    cocotb.start_soon(monitor())
    for unit in (0, 1, 2):
        for length in (1, 2, 3):
            for phase in (0, 1, 5, 14, 15):
                if unit == 0 and phase:                    # unit 0 は毎周期が刻み: 位置は 1 通り
                    continue
                await out.set(0, enable=1, mode=PULSE, src=HOST, length=length, unit=unit)
                tick = 16 ** unit
                # 源のパルスの周期 S を、出力の FF が変わる周期 S + 2 の刻みの中の位置が phase になるように選ぶ
                while True:
                    await RisingEdge(dut.clk)
                    await ReadOnly()
                    s = int(dut.time.value) + 1                # 次の周期の time
                    if unit == 0 or (s + XTRIG_OUT_SOURCE_DELAY) % tick == phase:
                        break
                await RisingEdge(dut.clk)
                dut.sources.value = HOST                        # 周期 s の間だけ 1
                await RisingEdge(dut.clk)
                dut.sources.value = 0
                start = len(seen)
                await ClockCycles(dut.clk, length * tick + 8)
                high = [t for t, v in seen[start - 2:] if v]
                want = xtrig_out_window(caps, s, mode=PULSE, length=length, unit=unit)
                assert high and high[0] == want["t_on"] == s + XTRIG_OUT_SOURCE_DELAY, (unit, length, phase, high[:2], s)
                assert high[-1] + 1 == want["t_off"] and len(high) == want["pulse_cycles"], (unit, length, phase, len(high))
                assert (length - 1) * tick + 1 <= want["pulse_cycles"] <= length * tick
                await out.set(0, enable=0)
                await ClockCycles(dut.clk, 2)
    # 端子の並びの条件（条件の間）: 時刻が T になる辺の直後に端子を変える → oe は T + 4 の周期から
    await out.set(1, enable=1, mode=WHILE, mask=0b1, value=0b1, level=1)
    await RisingEdge(dut.clk)
    await ReadOnly()
    t = int(dut.time.value) + 1
    await RisingEdge(dut.clk)
    dut.pins.value = 0b1
    for _ in range(10):
        await RisingEdge(dut.clk)
        await ReadOnly()
        if out.oe(1):
            assert int(dut.time.value) == t + XTRIG_OUT_PIN_DELAY, (int(dut.time.value), t)
            break
    else:
        raise AssertionError("oe(1) が立たない")
