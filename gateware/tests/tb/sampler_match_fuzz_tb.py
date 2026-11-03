"""sampler_match のランダム試験: 乱数の mask / value / 番地 / 番地のマスクと、乱数の標本列（番地・値・ACK・間隔・番号）を入れ、
Python の基準モデルと fire の時刻・数・最後の一致・seen・fire_time・prev_time を突き合わせる。
途中で enable を落として入れ直す（数と状態が消える）ことも混ぜる。門（after_start と start）・最初の標本（first_sample）も
乱数で混ぜる（docs/superpowers/specs/2026-11-03-halt-watch-fpga-restart-design.md §3.1）。"""

import random

import cocotb
from cocotb.triggers import FallingEdge, RisingEdge

from tests.tb.sampler_match_tb import OK, WAIT, pulse_start, setup

ADDR = 0xE000EDF0


class Model:
    """状態（前の標本が一致したか・標本を見たか・その時刻）は、標本の番号（か所 × 16 + 語）ごと。"""

    def __init__(self, addr, mask, value, addr_mask, after_start=False, first_sample=False):
        self.addr, self.mask, self.value, self.addr_mask = addr, mask, value, addr_mask
        self.after_start, self.first_sample = after_start, first_sample
        self.clear()

    def clear(self):
        self.matched = False                             # 最後の標本が一致（番号によらない）
        self.any = False
        self.idx_matched, self.idx_seen, self.idx_time = {}, set(), {}
        self.seen = 0
        self.count = 0
        self.fire_time = 0
        self.prev_time = 0
        self.fire_index = 0
        self.fires = []
        self.unmatched = 0                               # 番号ごとに「有効にした後に不一致の標本を見た」（ビット = 番号）
        self.started, self.start_time, self.fire_first = False, 0, False

    def start(self, time):
        self.started, self.start_time = True, time

    def sample(self, t, addr, data, ack, index):
        if ack != OK or (addr ^ self.addr) & self.addr_mask or (self.after_start and not self.started):
            return
        hit = ((data ^ self.value) & self.mask) == 0
        if not hit:
            self.unmatched |= 1 << index
        seen = index in self.idx_seen
        if hit and ((seen and not self.idx_matched[index]) or (not seen and self.first_sample)):
            self.count += 1
            self.fire_time, self.fire_index, self.fire_first = t, index, not seen
            self.prev_time = self.idx_time[index] if seen else self.start_time
            self.fires.append(t + 1)                     # fire は標本を受けた次の周期（2 段: 比べる → 状態。2026-10-29）
        self.matched = hit
        self.idx_matched[index] = hit
        self.idx_seen.add(index)
        self.idx_time[index] = t
        self.any = True
        self.seen += 1


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
        addr_mask = rng.choice((0xFFFFFFFF, 0xFFFFFFFF & ~0x6000, 0xFFFFFF00))
        after_start, first_sample = rng.random() < 0.5, rng.random() < 0.5
        dut.enable.value = 0
        await RisingEdge(dut.clk)
        dut.match_addr.value, dut.match_mask.value, dut.match_value.value = addr, mask, value
        dut.match_addr_mask.value = addr_mask
        dut.after_start.value, dut.first_sample.value = int(after_start), int(first_sample)
        await RisingEdge(dut.clk)
        dut.enable.value = 1
        await RisingEdge(dut.clk)
        await RisingEdge(dut.clk)
        b.fires.clear()
        m = Model(addr, mask, value, addr_mask, after_start, first_sample)
        for _ in range(60):
            if rng.random() < 0.06:
                time = rng.getrandbits(48)
                await pulse_start(dut, time)                 # 門を開ける（after_start = 0 なら最初の標本の prev_time だけに効く）
                m.start(time)
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
            a = (addr ^ (rng.getrandbits(32) & ~addr_mask & 0xFFFFFFFF)) if rng.random() < 0.8 else rng.getrandbits(32)
            data = pick_value(rng, value, mask)
            ack = OK if rng.random() < 0.85 else rng.choice((WAIT, 0b100, 0b000, 0b111))
            index = rng.choice((0, 16, 32, 48, 5, 127))
            t = await b.sample(a, data, ack, gap=rng.choice((0, 1, 3)), index=index)
            m.sample(t, a, data, ack, index)
        await RisingEdge(dut.clk)                    # 最後の標本が 2 段目を通るまで（1 周期）
        await FallingEdge(dut.clk)
        assert b.fires == m.fires, f"case {case}: fire の時刻 {b.fires} != {m.fires}"
        assert int(dut.count.value) == m.count
        assert int(dut.seen.value) == m.seen
        assert int(dut.matched.value) == int(m.matched)
        assert int(dut.seen_any.value) == int(m.any)
        assert int(dut.fire_time.value) == m.fire_time
        assert int(dut.prev_time.value) == m.prev_time
        assert int(dut.fire_index.value) == m.fire_index
        assert int(dut.unmatched_seen.value) == m.unmatched
        assert int(dut.fire_first.value) == int(m.fire_first)
        assert int(dut.started.value) == int(m.started)
        dut.enable.value = 0
        dut.after_start.value, dut.first_sample.value = 0, 0
        await RisingEdge(dut.clk)
