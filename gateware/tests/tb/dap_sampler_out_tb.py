"""dap_sampler_out（DAP サンプラの確認を待つ語の溜めと件の出力。CHECK_MAX = 4）のテストベンチ。

約束（rtl/debug/dap_sampler_out.vhd）: start の次の周期から、buf の 0 から start_count 語を 1 周期 1 件で出す（語の件 = 種類 7、
bound の語はその直後に種類 17）。誤りの区間（種類 18）は、出していなければ来た周期に、出していれば出し終えてから出す。
draining・gap_pending は入力の周期から 1。quiet は最後の標本から 2 周期で 1 に戻る。"""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, FallingEdge, ReadOnly, RisingEdge

KIND_SAMPLE, KIND_BOUND, KIND_GAP = 0x07, 0x11, 0x12
ACK_OK = 0b001


def entry_bits(index, addr, value, stamp, waits, bound, rsp_time, req):
    """dap_sampler_pkg の to_bits と同じ並び（上位から index・addr・value・stamp・waits・bound・rsp_time・req）。"""
    v = index
    for x, w in ((addr, 32), (value, 32), (stamp, 48), (waits, 5), (bound, 1), (rsp_time, 48), (req, 4)):
        v = (v << w) | x
    return v


class Records:
    """rec_valid の周期の (周期の番号, kind, word) と sample_valid の周期の (周期の番号, addr, data, index, time) を集める。"""

    def __init__(self, dut):
        self.dut, self.recs, self.samples, self.cycle = dut, [], [], 0
        cocotb.start_soon(self._run())

    async def _run(self):
        while True:
            await RisingEdge(self.dut.clk)
            await ReadOnly()
            self.cycle += 1
            if self.dut.rec_valid.value == 1:
                self.recs.append((self.cycle, int(self.dut.rec_kind.value), int(self.dut.rec_word.value)))
            if self.dut.sample_valid.value == 1:
                assert int(self.dut.sample_ack.value) == ACK_OK
                self.samples.append((self.cycle, int(self.dut.sample_addr.value), int(self.dut.sample_data.value),
                                     int(self.dut.sample_index.value), int(self.dut.sample_time.value)))


async def setup(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    for s in (dut.wr_valid, dut.start_valid, dut.gap_valid):
        s.value = 0
    dut.wr_index.value = 0
    dut.wr_data.value = 0
    dut.start_count.value = 1
    dut.gap_data.value = 0
    dut.rst.value = 1
    await ClockCycles(dut.clk, 3)
    await FallingEdge(dut.clk)
    dut.rst.value = 0
    return Records(dut)


async def pulse(dut, **signals):
    """次の立ち上がりで 1 周期だけ signals を与える（立ち下がりで変える）。"""
    for name, value in signals.items():
        getattr(dut, name).value = value
    await FallingEdge(dut.clk)
    for name in signals:
        if name.endswith("_valid"):
            getattr(dut, name).value = 0


async def write(dut, i, **e):
    await pulse(dut, wr_valid=1, wr_index=i, wr_data=entry_bits(**e))


@cocotb.test()
async def drains_words_and_bound_records(dut):
    rec = await setup(dut)
    e0 = dict(index=3, addr=0x2000_0000, value=0x1111_2222, stamp=0x100, waits=2, bound=0, rsp_time=0x150, req=0xB)
    e1 = dict(index=4, addr=0x2000_0004, value=0x3333_4444, stamp=0x200, waits=0, bound=1, rsp_time=0x250, req=0xD)
    await write(dut, 0, **e0)
    await write(dut, 1, **e1)
    await pulse(dut, start_valid=1, start_count=2)
    start = rec.cycle                                      # start を受けた立ち上がり
    await ReadOnly()
    assert dut.draining.value == 1
    await ClockCycles(dut.clk, 6)
    assert [(c - start, k) for c, k, _ in rec.recs] == [(1, KIND_SAMPLE), (2, KIND_SAMPLE), (3, KIND_BOUND)]
    w0 = rec.recs[0][2]
    assert w0 == (2 << 115) | (ACK_OK << 112) | (0x1111_2222 << 80) | (0x2000_0000 << 48) | 0x100
    assert rec.recs[2][2] == (0xD << 112) | (0x2000_0004 << 48) | 0x250
    assert [(a, d, i, t) for _, a, d, i, t in rec.samples] == [(0x2000_0000, 0x1111_2222, 3, 0x100),
                                                                (0x2000_0004, 0x3333_4444, 4, 0x200)]
    assert dut.draining.value == 0


@cocotb.test()
async def gap_alone_goes_out_in_its_cycle(dut):
    rec = await setup(dut)
    await ClockCycles(dut.clk, 2)
    await FallingEdge(dut.clk)
    dut.gap_data.value = 0xABC
    dut.gap_valid.value = 1
    await ReadOnly()
    assert dut.gap_pending.value == 1
    await FallingEdge(dut.clk)
    dut.gap_valid.value = 0
    at = rec.cycle
    await ClockCycles(dut.clk, 3)
    assert [(c, k, w) for c, k, w in rec.recs] == [(at, KIND_GAP, 0xABC)]
    assert dut.gap_pending.value == 0


@cocotb.test()
async def gap_waits_for_the_drain(dut):
    rec = await setup(dut)
    await write(dut, 0, index=0, addr=0x10, value=1, stamp=1, waits=0, bound=0, rsp_time=0, req=0)
    await write(dut, 1, index=1, addr=0x14, value=2, stamp=2, waits=0, bound=0, rsp_time=0, req=0)
    await pulse(dut, start_valid=1, start_count=2)
    await pulse(dut, gap_valid=1, gap_data=0x55)           # 出している間に来た: 出し終えてから
    await ClockCycles(dut.clk, 5)
    assert [k for _, k, _ in rec.recs] == [KIND_SAMPLE, KIND_SAMPLE, KIND_GAP]
    assert rec.recs[2][2] == 0x55
    assert rec.recs[2][0] == rec.recs[1][0] + 1


@cocotb.test()
async def quiet_after_two_cycles(dut):
    rec = await setup(dut)
    await ReadOnly()
    assert dut.quiet.value == 1
    await FallingEdge(dut.clk)
    await write(dut, 0, index=0, addr=0x10, value=1, stamp=1, waits=0, bound=0, rsp_time=0, req=0)
    await pulse(dut, start_valid=1, start_count=1)
    quiet = []
    for _ in range(5):
        await RisingEdge(dut.clk)
        await ReadOnly()
        quiet.append((int(dut.sample_valid.value), int(dut.quiet.value)))
    # 標本の周期（sample_valid = 1）から 2 周期 quiet = 0
    assert quiet[:4] == [(1, 0), (0, 0), (0, 1), (0, 1)], quiet
