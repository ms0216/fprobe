"""obs_soft_core（obs_soft_tap + obs_core。1 クロック版は obs_tck_sampler も）。JTAG の 4 本だけで、TAP の IDCODE・
観測の回路の ID・記録が読め、印の遅れが申告どおり。TCK 5 MHz・clk 50 MHz（1 クロック版は TCK ≤ clk / 8）。"""

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge, Timer
from cocotb.utils import get_sim_time

HALF_NS = 100
CLK_NS = 20


class Jtag:
    def __init__(self, dut, half_ns=HALF_NS):
        self.dut, self.half = dut, half_ns
        self.update_ns = 0.0
        dut.tck.value = 0
        dut.tms.value = 1
        dut.tdi.value = 0

    async def clock(self, tms: int, tdi: int = 0) -> int:
        """TCK の立ち下がりで TMS・TDI を変え、立ち上がりの直前に TDO を読む（送り手の作法）。"""
        self.dut.tms.value = tms
        self.dut.tdi.value = tdi
        await Timer(self.half, "ns")
        tdo = int(self.dut.tdo.value) if self.dut.tdo_oe.value == 1 else None
        self.dut.tck.value = 1
        self.rise_ns = get_sim_time("ns")
        await Timer(self.half, "ns")
        self.dut.tck.value = 0
        return tdo

    async def reset(self):
        for _ in range(5):
            await self.clock(1)
        await self.clock(0)                                  # Run-Test/Idle

    async def shift(self, bits: list[int], ir: bool) -> list[int]:
        """Run-Test/Idle から IR か DR を通って Run-Test/Idle に戻る。TDI に bits を送り、TDO を返す。"""
        for tms in ([1, 1, 0, 0] if ir else [1, 0, 0]):     # → Shift
            await self.clock(tms)
        out = []
        for k, b in enumerate(bits):
            out.append(await self.clock(1 if k == len(bits) - 1 else 0, b))
        await self.clock(1)                                  # Exit1 → Update
        await self.clock(0)                                  # Update → Run-Test/Idle（この立ち上がりで実行）
        self.update_ns = self.rise_ns
        return out

    async def dr(self, cmd: int = 0, words: int = 0) -> list[int]:
        """観測の回路の DR: 命令 cmd（最後の 16 ビット）を送り、見出し + words 語を受ける。"""
        n = 32 * (1 + words)
        bits = [0] * (n - 16) + bits_of(cmd, 16)
        out = await self.shift(bits, ir=False)
        return [word(out, w) for w in range(1 + words)]


def bits_of(v: int, n: int) -> list[int]:
    return [v >> k & 1 for k in range(n)]


def word(bits: list[int], w: int) -> int:
    return sum((b or 0) << k for k, b in enumerate(bits[32 * w:32 * w + 32]))


def command(op, addr=0, data=0):
    return op | addr << 4 | data << 8


async def drain(jtag) -> list[int]:
    out = []
    for _ in range(20):
        r = await jtag.dr(0, 16)
        assert r[0] >> 24 == 0xA6
        count = r[0] & 0xFFFF
        out += r[1:1 + min(count, 16)]
        if count == 0:
            return out
    raise AssertionError("records never ran out")


async def setup(dut):
    t0 = get_sim_time("ns")
    cocotb.start_soon(Clock(dut.clk, CLK_NS, unit="ns").start())
    dut.rst.value = 1
    dut.probes.value = 0x1234
    jtag = Jtag(dut)
    await ClockCycles(dut.clk, 5)
    dut.rst.value = 0
    await jtag.reset()
    return jtag, t0


@cocotb.test()
async def the_id_and_records_are_read_through_the_four_pins(dut):
    jtag, _ = await setup(dut)
    idcode = await jtag.shift([0] * 32, ir=False)            # Test-Logic-Reset の後は IDCODE
    assert word(idcode, 0) == 0x0F0B5001
    await jtag.shift(bits_of(0x2, 4), ir=True)               # USER
    await jtag.dr(command(2, 0))                             # ID の下位を読む
    await jtag.dr(command(2, 1))                             # 上位
    await jtag.dr(command(1, 14, 1))                         # 開始
    await ClockCycles(dut.clk, 20)
    rec = await drain(jtag)
    responses = {w >> 16 & 0xF: w & 0xFFFF for w in rec if w >> 26 == 0b110001}
    assert responses == {0: 0x4253, 1: 0x464F}, [hex(w) for w in rec]
    timed = [w for w in rec if w >> 30 != 3]
    assert timed == [0x1234]                                 # 鎖の始めの値（delta 0）


@cocotb.test()
async def the_sync_mark_is_where_the_core_declares(dut):
    # 印の瞬間は、TCK のピンの Update-DR を出る立ち上がりより SYNC_DELAY / 2 周期（平均）遅い。±0.5 周期
    jtag, t0 = await setup(dut)
    await jtag.shift(bits_of(0x2, 4), ir=True)
    await jtag.dr(command(2, 7))                             # SYNC_DELAY
    await jtag.dr(command(2, 8))                             # SAMPLE_DELAY
    await jtag.dr(command(1, 14, 1))
    await ClockCycles(dut.clk, 20)
    rec = await drain(jtag)
    regs = {w >> 16 & 0xF: w & 0xFFFF for w in rec if w >> 26 == 0b110001}
    declared, sample = regs[7] / 2, regs[8]
    await RisingEdge(dut.clk)
    await Timer(1, "ns")
    dut.probes.value = 0x5678
    edge = round((get_sim_time("ns") - 1 - t0) / CLK_NS)
    rng = random.Random(5)
    updates = []
    for seq in range(1, 31):
        await Timer(rng.randrange(1, 997), "ns")
        await jtag.dr(command(3, 0, seq))
        updates.append((seq, (jtag.update_ns - t0) / CLK_NS))
        rec += await drain(jtag)
    t, marks, t_value = None, {}, None
    for w in rec:                                            # 鎖の時刻
        k = w >> 30
        if k == 0:
            t = 0 if w >> 16 & 0x3FFF == 0 else t + (w >> 16 & 0x3FFF)
            if w & 0xFFFF == 0x5678 and t_value is None:
                t_value = t
        elif k == 1:
            t += w >> 16 & 0x3FFF
            if w >> 15 & 1:
                marks[w >> 7 & 0xFF] = t - (w & 0x7F)
    offset = edge + sample - t_value
    lag = [marks[s] + offset - u for s, u in updates]
    assert abs(sum(lag) / len(lag) - declared) < 0.15, (sum(lag) / len(lag), declared)
    assert declared - 0.5 - 1e-6 <= min(lag) and max(lag) <= declared + 0.5 + 1e-6, (min(lag), max(lag), declared)
