"""obs_match: 基準モデルとランダム入力（数千周期）で突き合わせる。"""

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, RisingEdge


class Model:
    def __init__(self):
        self.reset()
        self.on_prev = 0

    def reset(self):
        self.last = 0
        self.hit_last = 0
        self.seen = 0
        self.count = 0
        self.match = 0

    def step(self, restart, valid, v, mask, value, change, on):
        self.match = 0
        rising = on and not self.on_prev
        if rising:
            self.count = 0
            self.seen = 0                                    # 最初の値は覚えるだけ
        prev_on = self.on_prev
        self.on_prev = on
        if valid:
            hit = ((v ^ value) & mask) == 0
            fire = (hit and not self.hit_last) if change == 0 else (hit and ((v ^ self.last) & change) != 0)
            if fire and self.seen and on and prev_on:
                self.match = 1
                self.count = (self.count + 1) & 0xFFFFFFFF
            self.last = v
            self.hit_last = int(hit)
            self.seen = 1
        if restart:
            self.seen = 0


@cocotb.test()
async def random_values_match_the_model(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    rng = random.Random(99)
    for name in ("restart", "obs_valid", "obs_v", "match_mask", "match_value", "match_change", "match_on"):
        getattr(dut, name).value = 0
    dut.rst.value = 1
    await RisingEdge(dut.clk)
    await RisingEdge(dut.clk)
    dut.rst.value = 0
    m = Model()
    mask = value = change = 0
    on = 0
    fired = 0
    for cycle in range(8000):
        await FallingEdge(dut.clk)
        assert int(dut.match.value) == m.match, f"match @ {cycle}"
        assert int(dut.match_count.value) == m.count, f"count @ {cycle}"
        fired += m.match
        if cycle % 200 == 0:                       # 設定を時々変える。値は小さな範囲で何度も当たるように
            mask = rng.choice([0x0000, 0x000F, 0x00FF, 0x0003, 0xFFFF])
            value = rng.getrandbits(16) & mask
            change = rng.choice([0x0000, 0x0000, 0x0001, 0x000F, 0x0100])
        if rng.random() < 0.01:
            on ^= 1
        restart = rng.random() < 0.01
        valid = rng.random() < 0.4
        v = rng.getrandbits(4) | (rng.getrandbits(16) if rng.random() < 0.1 else 0)
        rst = rng.random() < 0.001
        dut.restart.value = int(restart)
        dut.obs_valid.value = int(valid)
        dut.obs_v.value = v
        dut.match_mask.value = mask
        dut.match_value.value = value
        dut.match_change.value = change
        dut.match_on.value = on
        dut.rst.value = int(rst)
        await RisingEdge(dut.clk)
        if rst:
            m.reset()
            m.on_prev = on
        else:
            m.step(restart, valid, v, mask, value, change, on)
    assert fired > 50, f"試験が一致をあまり通っていない ({fired})"
