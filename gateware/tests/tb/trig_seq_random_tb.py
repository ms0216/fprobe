"""trig_seq: ランダムな設定とランダムな入力（ピン・match・host・enable・rst）を、仕様（trig_seq.vhd 先頭の説明）から
書いた基準モデルと毎周期突き合わせる（fire・fired・stage）。WIDTH = 16。

仕様の時間の数え方: 段 k > 0 に入った周期の次の周期から数えて within(k) 周期の間の事象は受け付け、その次の周期で段 0 に戻る。
1 周期に進むのは高々 1 段。回数 0 は 1 と同じ。stages 0 は 1 と同じ（5 以上は仕様外なので使わない）。"""
import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, ReadOnly, RisingEdge

W = 16
FULL = (1 << W) - 1


def hit(v, p, mask, value, rise, fall):
    edges = True
    if rise | fall:
        edges = bool((rise & v & ~p & FULL) | (fall & p & ~v & FULL))
    return edges and (v & mask) == (value & mask)


class Model:
    def __init__(self, cfg):
        self.c = cfg
        self.p1 = self.s = self.prev = 0
        self.ev = 0
        self.stage = 0
        self.left = 0
        self.tleft = 0
        self.tlimit = False
        self.fire = 0
        self.fired = 0

    def enter(self, k):
        c = self.c
        self.stage = k
        self.left = c["count"][k]
        within = 0 if k == 0 else c["within"][k - 1]
        self.tleft = within
        self.tlimit = within != 0

    def step(self, probes, match, host, enable, rst):
        c = self.c
        # このエッジで使う値（更新前）
        s, prev, ev = self.s, self.prev, self.ev
        new_ev = (host << 3) | (match << 2) \
            | (int(hit(s, prev, c["b_mask"], c["b_value"], c["b_rise"], c["b_fall"])) << 1) \
            | int(hit(s, prev, c["a_mask"], c["a_value"], c["a_rise"], c["a_fall"]))
        last = max(c["stages"] - 1, 0)
        fire = 0
        if rst or not enable:
            self.enter(0)
            self.fired = 0
        elif not self.fired:
            timed_out = self.tlimit and self.tleft == 0
            if self.tlimit:
                self.tleft -= 1
            if timed_out:
                self.enter(0)
            elif ev & c["cond"][self.stage]:
                if self.left <= 1:
                    if self.stage == last:
                        fire = 1
                        self.fired = 1
                        self.enter(0)
                    else:
                        self.enter(self.stage + 1)
                else:
                    self.left -= 1
        self.fire = fire
        self.p1, self.s, self.prev = probes, self.p1, s
        self.ev = new_ev


def rand_cfg(rng):
    def pat():
        mask = rng.choice([0, FULL, 1, 3, rng.getrandbits(W) & rng.getrandbits(W)])
        return mask
    cfg = {"stages": rng.choice([0, 1, 2, 3, 4, 4, 3]),
           "cond": [rng.choice([1, 2, 4, 8, 3, 15, 5, 9]) for _ in range(4)],
           "count": [rng.choice([0, 1, 1, 2, 3, 5]) for _ in range(4)],
           "within": [rng.choice([0, 0, 1, 2, 3, 7, 20]) for _ in range(3)]}
    for n in "ab":
        cfg[f"{n}_mask"] = pat()
        cfg[f"{n}_value"] = rng.getrandbits(W) & cfg[f"{n}_mask"] if rng.random() < 0.7 else rng.getrandbits(W)
        edge = rng.choice([0, 0, 1, 2])
        cfg[f"{n}_rise"] = (rng.getrandbits(W) & rng.getrandbits(W)) if edge == 1 else 0
        cfg[f"{n}_fall"] = (rng.getrandbits(W) & rng.getrandbits(W)) if edge == 2 else 0
    return cfg


def apply(dut, cfg):
    dut.stages.value = cfg["stages"]
    for n in "ab":
        for f in ("mask", "value", "rise", "fall"):
            getattr(dut, f"{n}_{f}").value = cfg[f"{n}_{f}"]
    for k in range(4):
        getattr(dut, f"cond{k}").value = cfg["cond"][k]
        getattr(dut, f"count{k}").value = cfg["count"][k]
    for k in range(1, 4):
        getattr(dut, f"within{k}").value = cfg["within"][k - 1]


async def one_config(dut, rng, cycles, tag):
    cfg = rand_cfg(rng)
    # 設定は止めている間（enable = 0）に変える
    dut.enable.value = 0
    await FallingEdge(dut.clk)
    apply(dut, cfg)
    dut.match.value = dut.host.value = dut.rst.value = 0
    for _ in range(4):
        dut.probes.value = 0
        await RisingEdge(dut.clk)
        await FallingEdge(dut.clk)
    model = Model(cfg)
    # 同期化の段・前の値は 0、ev は pins = 0 の判定（match・host は 0）
    model.p1 = model.s = model.prev = 0
    model.ev = (int(hit(0, 0, cfg["b_mask"], cfg["b_value"], cfg["b_rise"], cfg["b_fall"])) << 1)         | int(hit(0, 0, cfg["a_mask"], cfg["a_value"], cfg["a_rise"], cfg["a_fall"]))
    probes = 0
    enable = 0
    fires = 0
    p_flip = rng.choice([0.05, 0.3, 0.7])
    for n in range(cycles):
        if rng.random() < p_flip:
            probes ^= 1 << rng.randrange(W)
        if rng.random() < 0.3:
            probes = rng.getrandbits(W)
        match = int(rng.random() < 0.1)
        host = int(rng.random() < 0.05)
        if rng.random() < 0.01:
            enable ^= 1
        rst = int(rng.random() < 0.003)
        dut.probes.value = probes
        dut.match.value, dut.host.value, dut.enable.value, dut.rst.value = match, host, enable, rst
        model.step(probes, match, host, enable, rst)
        await RisingEdge(dut.clk)
        await ReadOnly()
        assert int(dut.fire.value) == model.fire, f"{tag} cycle {n}: fire {dut.fire.value} != {model.fire} {cfg}"
        assert int(dut.fired.value) == model.fired, f"{tag} cycle {n}: fired {dut.fired.value} != {model.fired} {cfg}"
        assert int(dut.stage.value) == model.stage, f"{tag} cycle {n}: stage {dut.stage.value} != {model.stage} {cfg}"
        fires += model.fire
        await FallingEdge(dut.clk)
    return fires


@cocotb.test()
async def random_configs_match_the_reference_model(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    for name in ("probes", "match", "host", "enable"):
        getattr(dut, name).value = 0
    dut.rst.value = 1
    await RisingEdge(dut.clk)
    rng = random.Random(77)
    total = 0
    for i in range(150):
        total += await one_config(dut, rng, 600, f"config {i}")
    assert total > 20, "発火がほとんど無い試験は意味が薄い"
