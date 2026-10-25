"""sampler_match のランダム試験: 乱数の mask / value / 番地と、乱数の標本列（番地・値・ACK・間隔）を入れ、
Python の基準モデルと fire の時刻・数・最後の一致・seen・fire_time・prev_time を突き合わせる。
途中で enable を落として入れ直す（数と状態が消える）ことも混ぜる。"""

import random

import cocotb
from cocotb.triggers import FallingEdge, RisingEdge

from tests.tb.sampler_match_tb import OK, WAIT, setup

ADDR = 0xE000EDF0


class Model:
    def __init__(self, addr, mask, value):
        self.addr, self.mask, self.value = addr, mask, value
        self.clear()

    def clear(self):
        self.matched = False
        self.any = False
        self.seen = 0
        self.count = 0
        self.last_time = 0
        self.fire_time = 0
        self.prev_time = 0
        self.fires = []

    def sample(self, t, addr, data, ack):
        if ack != OK or addr != self.addr:
            return
        hit = ((data ^ self.value) & self.mask) == 0
        if hit and self.any and not self.matched:
            self.count += 1
            self.fire_time, self.prev_time = t, self.last_time
            self.fires.append(t)
        self.matched = hit
        self.any = True
        self.seen += 1
        self.last_time = t


def pick_value(rng, value, mask):
    """一致する値と一致しない値を半々に混ぜる。"""
    if rng.random() < 0.5:
        return (value & mask) | (rng.getrandbits(32) & ~mask & 0xFFFFFFFF)
    return rng.getrandbits(32)


@cocotb.test()
async def random_streams_match_the_model(dut):
    rng = random.Random(2026)
    b = await setup(dut)
    for case in range(12):
        mask = rng.choice((0xFFFFFFFF, 1 << 17, 0xFF, rng.getrandbits(32), 0))
        value = rng.getrandbits(32)
        addr = rng.choice((ADDR, rng.getrandbits(32)))
        dut.enable.value = 0
        await RisingEdge(dut.clk)
        dut.match_addr.value, dut.match_mask.value, dut.match_value.value = addr, mask, value
        await RisingEdge(dut.clk)
        dut.enable.value = 1
        await RisingEdge(dut.clk)
        await RisingEdge(dut.clk)
        b.fires.clear()
        m = Model(addr, mask, value)
        for _ in range(60):
            if rng.random() < 0.04:
                # enable を落として入れ直す: 数と状態が消える。入れ直した周期の標本は比べない（setup と同じ手順で間を空ける）
                dut.enable.value = 0
                await b.sample(addr, rng.getrandbits(32))
                await RisingEdge(dut.clk)
                dut.enable.value = 1
                await RisingEdge(dut.clk)
                await RisingEdge(dut.clk)
                m.clear()
                m.fires.clear()
                b.fires.clear()
            a = addr if rng.random() < 0.8 else rng.getrandbits(32)
            data = pick_value(rng, value, mask)
            ack = OK if rng.random() < 0.85 else rng.choice((WAIT, 0b100, 0b000, 0b111))
            t = await b.sample(a, data, ack, gap=rng.choice((0, 1, 3)))
            m.sample(t, a, data, ack)
        await FallingEdge(dut.clk)
        assert b.fires == m.fires, f"case {case}: fire の時刻 {b.fires} != {m.fires}"
        assert int(dut.count.value) == m.count
        assert int(dut.seen.value) == m.seen
        assert int(dut.matched.value) == int(m.matched)
        assert int(dut.seen_any.value) == int(m.any)
        assert int(dut.fire_time.value) == m.fire_time
        assert int(dut.prev_time.value) == m.prev_time
        dut.enable.value = 0
        await RisingEdge(dut.clk)
