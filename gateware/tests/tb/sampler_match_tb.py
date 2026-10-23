"""sampler_match のテストベンチ: サンプラの標本（番地・値・ACK）を入れ、一致に入った瞬間だけ fire が 1 周期出ること、
数・時刻（発火した標本と、その直前の標本）を確かめる。仕様 docs/superpowers/specs/2026-10-23-sampler-match-trigger-design.md"""

import cocotb
from cocotb.triggers import FallingEdge, ReadOnly, RisingEdge

from tests.tb.common import start_clock_and_reset

DHCSR, S_HALT = 0xE000EDF0, 1 << 17
OK, WAIT = 0b001, 0b010


class Bench:
    def __init__(self, dut):
        self.dut = dut
        self.t = 0
        self.fires = []                                  # fire が出た周期の time_count

    async def clock(self):
        d = self.dut
        while True:
            await FallingEdge(d.clk)
            d.time_count.value = self.t
            await RisingEdge(d.clk)
            await ReadOnly()
            if d.fire.value == 1:
                self.fires.append(self.t)
            self.t += 1

    async def sample(self, addr, data, ack=OK, gap=3):
        """標本を 1 つ入れ、回路がそれを受けた周期の time_count を返す。"""
        d = self.dut
        await RisingEdge(d.clk)
        d.sample_valid.value, d.sample_addr.value, d.sample_data.value, d.sample_ack.value = 1, addr, data, ack
        await RisingEdge(d.clk)
        taken = int(d.time_count.value)                  # この立ち上がりで回路が取り込んだ時刻
        d.sample_valid.value = 0
        for _ in range(gap):
            await RisingEdge(d.clk)
        return taken


async def setup(dut, mask=S_HALT, value=S_HALT, addr=DHCSR):
    dut.enable.value = 0
    dut.sample_valid.value = 0
    dut.sample_addr.value, dut.sample_data.value, dut.sample_ack.value = 0, 0, 0
    dut.match_addr.value, dut.match_mask.value, dut.match_value.value = addr, mask, value
    dut.time_count.value = 0
    await start_clock_and_reset(dut)
    b = Bench(dut)
    cocotb.start_soon(b.clock())
    await RisingEdge(dut.clk)
    dut.enable.value = 1
    await RisingEdge(dut.clk)
    return b


@cocotb.test()
async def fires_only_when_entering(dut):
    b = await setup(dut)
    await b.sample(DHCSR, 0x00030000 & ~S_HALT)          # 走っている
    await b.sample(DHCSR, 0x0)
    assert b.fires == [] and dut.count.value == 0
    t_prev = await b.sample(DHCSR, 0x0)
    t_fire = await b.sample(DHCSR, S_HALT | 1)            # 止まった → 入った瞬間
    await b.sample(DHCSR, S_HALT)                         # 止まったまま: 出さない
    assert len(b.fires) == 1 and dut.count.value == 1 and dut.matched.value == 1
    fire_t, prev_t = int(dut.fire_time.value), int(dut.prev_time.value)
    assert (fire_t, prev_t) == (t_fire, t_prev)           # 発火した標本と、その直前の（不一致の）標本の時刻
    assert dut.seen.value == 5
    await b.sample(DHCSR, 0)                              # 走り出した
    await b.sample(DHCSR, S_HALT)                         # もう一度止まった
    assert len(b.fires) == 2 and dut.count.value == 2


@cocotb.test()
async def first_sample_does_not_fire(dut):
    b = await setup(dut)
    await b.sample(DHCSR, S_HALT)                         # 有効にした最初の標本がすでに一致: 入った瞬間は見ていない
    await b.sample(DHCSR, S_HALT)
    assert b.fires == [] and dut.matched.value == 1 and dut.seen_any.value == 1 and dut.count.value == 0


@cocotb.test()
async def other_addresses_and_bad_acks_are_ignored(dut):
    b = await setup(dut)
    await b.sample(DHCSR, 0)
    await b.sample(DHCSR + 4, S_HALT)                     # 違う番地
    await b.sample(DHCSR, S_HALT, ack=WAIT)               # ACK が OK でない
    assert b.fires == [] and dut.seen.value == 1
    await b.sample(DHCSR, S_HALT)
    assert len(b.fires) == 1


@cocotb.test()
async def enable_rising_clears(dut):
    b = await setup(dut)
    await b.sample(DHCSR, 0)
    await b.sample(DHCSR, S_HALT)
    assert dut.count.value == 1
    dut.enable.value = 0
    await b.sample(DHCSR, 0)                              # 無効の間は数えない
    assert dut.seen.value == 2
    await RisingEdge(dut.clk)
    dut.enable.value = 1
    await RisingEdge(dut.clk)
    await RisingEdge(dut.clk)
    await FallingEdge(dut.clk)
    assert dut.count.value == 0 and dut.seen.value == 0 and dut.seen_any.value == 0
    await b.sample(DHCSR, S_HALT)                         # 消した後の最初の標本: 出さない
    assert len(b.fires) == 1
