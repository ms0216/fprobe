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
