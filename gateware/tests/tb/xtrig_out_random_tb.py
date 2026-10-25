"""xtrig_out: ランダムな設定・入力（端子・源・enable・rst・time）を、仕様（xtrig_out.vhd 先頭の説明）から書いた模型と毎周期
突き合わせる（oe と o）。COUNT = 3、WIDTH = 8。time は途中で飛ぶことも含めて 32 ビットでランダムに進める。"""
import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, ReadOnly, RisingEdge

COUNT, WIDTH = 3, 8
FULL = (1 << WIDTH) - 1


class Model:
    def __init__(self):
        self.p1 = self.s = 0
        self.tprev = [0] * 8                 # 1..7 を使う
        self.cond = [0] * COUNT
        self.cond_prev = [0] * COUNT
        self.held = [0] * COUNT
        self.drive = [0] * COUNT
        self.left = [0] * COUNT

    def step(self, cfg, pins, sources, time, rst):
        tbits = [0] + [(time >> (4 * u)) & 1 for u in range(1, 8)]
        tick = [1] + [tbits[u] ^ self.tprev[u] for u in range(1, 8)]
        new = {k: [] for k in ("cond", "cond_prev", "held", "drive", "left")}
        for k in range(COUNT):
            c = cfg[k]
            hit = ~(self.s ^ c["value"]) & c["mask"] & FULL
            if c["any"]:
                pattern = hit != 0
            else:
                pattern = c["mask"] != 0 and hit == c["mask"]
            cond_new = int(pattern or (sources & c["src"]) != 0)
            rise = self.cond[k] == 1 and self.cond_prev[k] == 0
            n, h = 0, 0
            if c["enable"] and not rst:
                if c["mode"] == 0:
                    if rise:
                        n = c["length"]
                    elif self.left[k] != 0 and tick[c["unit"]]:
                        n = self.left[k] - 1
                    else:
                        n = self.left[k]
                elif c["mode"] == 1:
                    h = 1 if rise else self.held[k]
            if c["enable"] and not rst and c["mode"] >= 2:
                d = self.cond[k]
            else:
                d = int(n != 0 or h == 1)
            new["cond"].append(cond_new)
            new["cond_prev"].append(self.cond[k])
            new["held"].append(h)
            new["drive"].append(d)
            new["left"].append(n)
        self.cond, self.cond_prev, self.held, self.drive, self.left = (new[x] for x in ("cond", "cond_prev", "held", "drive", "left"))
        self.p1, self.s = pins, self.p1
        self.tprev = [0] + tbits[1:]


def rand_cfg(rng):
    return [dict(enable=int(rng.random() < 0.8), mode=rng.choice([0, 0, 1, 2, 3]), level=rng.getrandbits(1),
                 any=rng.getrandbits(1), mask=rng.choice([0, FULL, 0x0F, 0x80, rng.getrandbits(WIDTH)]),
                 value=rng.getrandbits(WIDTH), src=rng.choice([0, 0, 1, 2, 4, 8, 16, 32, 63, rng.getrandbits(6)]),
                 length=rng.choice([0, 1, 2, 3, 10, 255]), unit=rng.choice([0, 0, 0, 1, 1, 2, 7, rng.randrange(8)]))
            for _ in range(COUNT)]


def pack(cfg, name, bits):
    return sum((c[name] & (1 << bits) - 1) << bits * k for k, c in enumerate(cfg))


def apply(dut, cfg):
    dut.enable.value = pack(cfg, "enable", 1)
    dut.mode.value = pack(cfg, "mode", 2)
    dut.level.value = pack(cfg, "level", 1)
    dut.any.value = pack(cfg, "any", 1)
    dut.mask.value = pack(cfg, "mask", WIDTH)
    dut.value.value = pack(cfg, "value", WIDTH)
    dut.src.value = pack(cfg, "src", 6)
    dut.length.value = pack(cfg, "length", 8)
    dut.unit.value = pack(cfg, "unit", 3)


@cocotb.test()
async def random_runs_match_the_reference_model(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    rng = random.Random(31)
    pulses = 0
    for run in range(60):
        cfg = rand_cfg(rng)
        # 状態を揃える: 全部無効・入力 0・time = 0 で数周期流してから、設定を入れて始める
        await FallingEdge(dut.clk)
        for k in range(COUNT):
            cfg_off = [dict(c, enable=0) for c in cfg]
        apply(dut, cfg_off)
        dut.pins.value = 0
        dut.sources.value = 0
        dut.time.value = 0
        dut.rst.value = 1
        for _ in range(4):
            await RisingEdge(dut.clk)
            await FallingEdge(dut.clk)
        dut.rst.value = 0
        # 揃えた状態: p1 = s = 0、cond は pins = 0 の判定のまま。模型にも同じ入力を流して揃える
        model = Model()
        for _ in range(4):
            model.step(cfg_off, 0, 0, 0, 1)
        apply(dut, cfg)
        time = rng.getrandbits(32)
        pins = 0
        for n in range(500):
            if rng.random() < 0.2:
                pins = rng.getrandbits(WIDTH) if rng.random() < 0.5 else pins ^ (1 << rng.randrange(WIDTH))
            sources = rng.getrandbits(6) if rng.random() < 0.05 else 0
            rst = int(rng.random() < 0.003)
            if rng.random() < 0.01:
                cfg[rng.randrange(COUNT)]["enable"] ^= 1
                apply(dut, cfg)
            time = (time + rng.choice([1, 1, 1, 1, 1, 5, 16, 255])) & 0xFFFFFFFF
            dut.pins.value, dut.sources.value, dut.time.value, dut.rst.value = pins, sources, time, rst
            model.step(cfg, pins, sources, time, rst)
            await RisingEdge(dut.clk)
            await ReadOnly()
            exp_oe = sum(d << k for k, d in enumerate(model.drive))
            assert int(dut.oe.value) == exp_oe, f"run {run} cycle {n}: oe {int(dut.oe.value):b} != {exp_oe:b} {cfg}"
            exp_o = sum(c["level"] << k for k, c in enumerate(cfg))
            assert int(dut.o.value) == exp_o
            pulses += exp_oe != 0
            await FallingEdge(dut.clk)
    assert pulses > 500, "出力がほとんど動かない試験は意味が薄い"
