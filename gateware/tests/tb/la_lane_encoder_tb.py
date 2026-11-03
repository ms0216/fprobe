"""la_lane_encoder（深いバッファの N 並列の符号器: RAW・RLE と起点の時刻・溢れ）のテストベンチ。LANES = 8・WIDTH = 16・IN_AGE = 5。

入力の語は、細かいサンプルの通し番号（語 w のレーン k = 8w + k）をそのまま値にする。タイムベース time は語の番号 w と同じに進める
（clk の立ち上がり e の後に、time = e、lanes = 語 e）。"""

import cocotb
from cocotb.triggers import ReadOnly, RisingEdge

from fprobe.la import _expand_words, decode_beats
from tests.tb.common import start_clock_and_reset

LANES = 8


class Source:
    def __init__(self, dut):
        self.dut, self.e = dut, 0
        cocotb.start_soon(self._run())

    async def _run(self):
        while True:
            await RisingEdge(self.dut.clk)
            self.e += 1
            self.dut.time.value = self.e
            self.dut.lane_data.value = sum(((LANES * self.e + k) & 0xFFFF) << (16 * k) for k in range(LANES))


async def setup(dut):
    for name in ("run", "rle", "step", "pack", "first_ch", "rate", "lane_data", "time"):
        getattr(dut, name).value = 0
    dut.out_ready.value = 1
    await start_clock_and_reset(dut)
    src = Source(dut)
    for _ in range(3):
        await RisingEdge(dut.clk)
    return src


async def stream(dut, cycles, *, rle=0, step=0, pack=0, rate=0, ready=lambda c: True):
    dut.rle.value, dut.step.value, dut.pack.value, dut.rate.value = rle, step, pack, rate
    dut.run.value = 1
    out = []
    for c in range(cycles):
        dut.out_ready.value = int(ready(c))
        await ReadOnly()
        if dut.out_valid.value == 1 and dut.out_ready.value == 1:
            out.append(int(dut.out_data.value).to_bytes(16, "little"))
        await RisingEdge(dut.clk)
    dut.run.value = 0
    dut.out_ready.value = 1
    await RisingEdge(dut.clk)
    await RisingEdge(dut.clk)
    return b"".join(out)


@cocotb.test()
async def the_start_time_and_latency_place_the_first_sample(dut):
    # 最初の語の時刻 start_time と、その語の値（= 細かいサンプルの通し番号 8 × 語 + レーン）が一致する
    await setup(dut)
    data = await stream(dut, 100)
    samples = list(decode_beats(data, False))
    assert int(dut.started.value) == 1
    assert int(dut.latency.value) == LANES + 5
    t0 = int(dut.start_time.value)
    assert samples[0] == (LANES * t0) & 0xFFFF
    assert all((b - a) & 0xFFFF == 1 for a, b in zip(samples, samples[1:])), str(samples[:40])    # 欠け・重複なし


@cocotb.test()
async def rle_of_a_counter_is_one_record_per_sample_and_overflows(dut):
    # 毎サンプル変わる入力: 1 周期に 8 レコードは 4 レコード/周期の出口を超えるので、溢れる（事実として立つ）。出た分は先頭から正しい
    await setup(dut)
    data = await stream(dut, 200, rle=1)
    assert int(dut.overrun.value) == 1
    samples = list(decode_beats(data, True))
    t0 = int(dut.start_time.value)
    assert samples and samples[0] == (LANES * t0) & 0xFFFF
    assert all((b - a) & 0xFFFF == 1 for a, b in zip(samples, samples[1:]))


@cocotb.test()
async def a_raw_beat_that_cannot_leave_sets_overrun_and_stops(dut):
    # 1 拍 = 1 語（16 ch・step 0）: 受け手が 3 周期止まると、保持中に次の拍が出て溢れる。以後は拍を作らない
    await setup(dut)
    data = await stream(dut, 60, ready=lambda c: not 20 <= c < 23)
    assert int(dut.overrun.value) == 1
    samples = list(decode_beats(data, False))
    assert all((b - a) & 0xFFFF == 1 for a, b in zip(samples, samples[1:]))
    # 次の流れの開始で溢れは消える
    data = await stream(dut, 50, pack=4)
    assert int(dut.overrun.value) == 0
    bits = list(_expand_words(decode_beats(data, False), 4))
    t0 = int(dut.start_time.value)
    assert bits == [(LANES * t0 + i) & 1 for i in range(len(bits))]
