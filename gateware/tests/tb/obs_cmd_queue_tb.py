"""obs_cmd_queue: 基準モデルとランダム入力（数千周期）で突き合わせる。"""

import os
import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, RisingEdge

DEPTH = int(os.environ.get("QUEUE_DEPTH", "4"))
WIDTH = 16


class Model:
    def __init__(self):
        self.reset()

    def reset(self):
        self.mem = [0] * DEPTH
        self.head = 0
        self.count = 0
        self.dropped = 0

    def step(self, push, data, pop, flush):
        push_ok = push and self.count < DEPTH
        if push_ok:
            self.mem[(self.head + self.count) % DEPTH] = data
            self.dropped = 0                                 # 最後の push の結果（積めたら消える）
        elif push:
            self.dropped = 1
        if flush:
            self.head = (self.head + self.count) % DEPTH
            self.count = 1 if push_ok else 0
            self.dropped = 0
        else:
            do_pop = pop and self.count != 0
            if do_pop:
                self.head = (self.head + 1) % DEPTH
            self.count += int(push_ok) - int(do_pop)


def check(dut, m, tag):
    assert int(dut.nonempty.value) == (1 if m.count else 0), tag
    assert int(dut.dropped.value) == m.dropped, tag
    if m.count:
        assert int(dut.front.value) == m.mem[m.head], tag


@cocotb.test()
async def random_traffic_matches_the_model(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    rng = random.Random(1234)
    for name in ("push", "pop", "flush", "push_data"):
        getattr(dut, name).value = 0
    dut.rst.value = 1
    await RisingEdge(dut.clk)
    await RisingEdge(dut.clk)
    dut.rst.value = 0
    m = Model()
    for cycle in range(6000):
        await FallingEdge(dut.clk)
        check(dut, m, f"cycle {cycle}")
        # 偏りを時々変えて、満杯・空の両方を何度も通る
        phase = (cycle // 300) % 3
        p_push, p_pop = ((0.7, 0.3), (0.3, 0.7), (0.5, 0.5))[phase]
        push = rng.random() < p_push
        flush = rng.random() < 0.03
        pop = (not flush) and m.count != 0 and rng.random() < p_pop
        data = rng.getrandbits(WIDTH)
        rst = rng.random() < 0.002
        dut.push.value = int(push)
        dut.push_data.value = data
        dut.pop.value = int(pop)
        dut.flush.value = int(flush)
        dut.rst.value = int(rst)
        await RisingEdge(dut.clk)
        if rst:
            m.reset()
        else:
            m.step(push, data, pop, flush)


@cocotb.test()
async def drop_is_sticky_until_flush(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    for name in ("push", "pop", "flush", "push_data"):
        getattr(dut, name).value = 0
    dut.rst.value = 1
    await RisingEdge(dut.clk)
    dut.rst.value = 0
    for i in range(DEPTH + 1):
        await FallingEdge(dut.clk)
        dut.push.value = 1
        dut.push_data.value = 0x100 + i
    await FallingEdge(dut.clk)
    dut.push.value = 0
    await FallingEdge(dut.clk)
    assert int(dut.dropped.value) == 1
    assert int(dut.front.value) == 0x100
    dut.pop.value = 1                                        # 1 つ空けて積み直すと、積めたので dropped は 0 に戻る
    await FallingEdge(dut.clk)
    dut.pop.value = 0
    dut.push.value = 1
    dut.push_data.value = 0x200
    await FallingEdge(dut.clk)
    dut.push.value = 0
    await FallingEdge(dut.clk)
    assert int(dut.dropped.value) == 0
    dut.flush.value = 1
    await FallingEdge(dut.clk)
    dut.flush.value = 0
    await FallingEdge(dut.clk)
    assert int(dut.dropped.value) == 0
    assert int(dut.nonempty.value) == 0
