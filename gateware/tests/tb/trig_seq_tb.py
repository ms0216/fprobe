"""trig_seq（多段トリガ）のテストベンチ。WIDTH = 16、clk 10 ns。"""

import cocotb
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge

from tests.tb.common import start_clock_and_reset

A, B, MATCH, HOST = 1, 2, 4, 8


class Seq:
    def __init__(self, dut):
        self.dut = dut
        self.cycle = 0
        self.fires: list[int] = []
        self.probes = 0

    async def monitor(self):
        while True:
            await RisingEdge(self.dut.clk)
            await ReadOnly()
            self.cycle += 1
            if self.dut.fire.value == 1:
                self.fires.append(self.cycle)

    def configure(self, *, stages, conds, counts=(1, 1, 1, 1), withins=(0, 0, 0),
                  a=(0, 0, 0, 0), b=(0, 0, 0, 0)):
        d = self.dut
        d.enable.value = 0
        d.stages.value = stages
        for name, (mask, value, rise, fall) in (("a", a), ("b", b)):
            getattr(d, f"{name}_mask").value = mask
            getattr(d, f"{name}_value").value = value
            getattr(d, f"{name}_rise").value = rise
            getattr(d, f"{name}_fall").value = fall
        for k in range(4):
            getattr(d, f"cond{k}").value = conds[k] if k < len(conds) else 0
            getattr(d, f"count{k}").value = counts[k]
        for k in range(1, 4):
            getattr(d, f"within{k}").value = withins[k - 1]

    async def arm(self):
        self.dut.enable.value = 0
        await RisingEdge(self.dut.clk)
        self.dut.enable.value = 1
        await RisingEdge(self.dut.clk)
        self.fires.clear()

    async def pin(self, bit, level, hold=5):
        self.probes = self.probes | 1 << bit if level else self.probes & ~(1 << bit)
        self.dut.probes.value = self.probes
        await ClockCycles(self.dut.clk, hold)

    async def pulse(self, name):
        getattr(self.dut, name).value = 1
        await RisingEdge(self.dut.clk)
        getattr(self.dut, name).value = 0
        await ClockCycles(self.dut.clk, 3)


async def setup(dut):
    for name in ("probes", "match", "host", "enable"):
        getattr(dut, name).value = 0
    seq = Seq(dut)
    seq.configure(stages=1, conds=(0,))
    await start_clock_and_reset(dut)
    cocotb.start_soon(seq.monitor())
    return seq


@cocotb.test()
async def a_then_b_within_a_time_limit(dut):
    seq = await setup(dut)
    # 段 0: ピン 0 の立ち上がり、段 1: ピン 1 の立ち下がり（段 1 に入ってから 50 周期以内）
    seq.configure(stages=2, conds=(A, B), withins=(50, 0, 0), a=(0, 0, 1, 0), b=(0, 0, 0, 2))
    await seq.pin(1, 1)
    await seq.arm()
    await seq.pin(1, 0)                                  # B だけ: 段 0 なので無視
    await seq.pin(0, 1)                                  # A → 段 1
    await seq.pin(1, 1, hold=100)                        # B が来ないまま制限時間切れ → 段 0
    await seq.pin(1, 0)
    assert seq.fires == [] and int(dut.stage.value) == 0
    await seq.pin(0, 0)
    await seq.pin(0, 1)                                  # A → 段 1
    await seq.pin(1, 1, hold=10)
    assert seq.fires == []
    await seq.pin(1, 0)                                  # 制限時間内の B → 発火
    assert len(seq.fires) == 1 and dut.fired.value == 1
    await seq.pin(0, 0)
    await seq.pin(0, 1)
    await seq.pin(1, 1)
    await seq.pin(1, 0)
    assert len(seq.fires) == 1                           # 発火後は止まる（enable の 0→1 まで）


@cocotb.test()
async def nth_occurrence(dut):
    seq = await setup(dut)
    seq.configure(stages=1, conds=(A,), counts=(3, 1, 1, 1), a=(0, 0, 1, 0))
    await seq.arm()
    rises = []
    for _ in range(4):
        rises.append(seq.cycle)                          # ピンを変える周期
        await seq.pin(0, 1)
        await seq.pin(0, 0)
    # 3 回目の立ち上がりの直後（同期化 2 段 + 判定 1 段 + 段の 1 段）に 1 回だけ
    assert len(seq.fires) == 1 and 0 < seq.fires[0] - rises[2] <= 6, (seq.fires, rises)


@cocotb.test()
async def events_from_the_debugger_and_host_and_four_stages(dut):
    seq = await setup(dut)
    # 段 0: 番地一致 2 回、段 1: ホスト、段 2: パターン A（ピン 2 = 1 の値一致）、段 3: 番地一致かホスト
    seq.configure(stages=4, conds=(MATCH, HOST, A, MATCH | HOST), counts=(2, 1, 1, 1),
                  a=(4, 4, 0, 0))
    await seq.arm()
    await seq.pulse("host")                              # 段 0 では数えない
    await seq.pulse("match")
    assert int(dut.stage.value) == 0
    await seq.pulse("match")
    assert int(dut.stage.value) == 1
    await seq.pulse("host")
    assert int(dut.stage.value) == 2
    await seq.pin(2, 1)                                  # 値一致は続くが、進むのは 1 周期に 1 段
    assert int(dut.stage.value) == 3 and seq.fires == []
    await seq.pulse("host")
    assert len(seq.fires) == 1
    await seq.arm()                                      # 再び仕掛ける
    assert dut.fired.value == 0 and int(dut.stage.value) == 0
