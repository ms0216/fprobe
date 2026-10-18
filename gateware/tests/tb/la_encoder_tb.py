"""la_encoder（ロジアナのサンプル列 → 128 ビットの拍）のテストベンチ。復号はホストライブラリ（fprobe.la）を使う。"""

import random

import cocotb
from cocotb.triggers import ReadOnly, RisingEdge

from fprobe.la import decode_beats, trigger_sample
from tests.tb.common import start_clock_and_reset


async def setup(dut):
    for name in ("run", "rle", "sample_valid", "sample", "sample_hit"):
        getattr(dut, name).value = 0
    dut.out_ready.value = 1
    await start_clock_and_reset(dut)


async def run(dut, samples, *, rle, hit_at=None, gap=0.3, stall=0.0, rng=None):
    """samples を流し、出てきた拍（bytes）とトリガ（拍の番号, 番号）を返す。"""
    rng = rng or random.Random(1)
    beats, trig = [], None
    dut.rle.value = int(rle)
    dut.run.value = 1
    await RisingEdge(dut.clk)

    async def collect():
        nonlocal trig
        while True:
            dut.out_ready.value = int(rng.random() >= stall)
            await ReadOnly()
            if dut.out_valid.value == 1 and dut.out_ready.value == 1:
                if dut.out_trig.value == 1 and trig is None:
                    trig = (len(beats), int(dut.out_slot.value))
                beats.append(int(dut.out_data.value).to_bytes(16, "little"))
            await RisingEdge(dut.clk)

    task = cocotb.start_soon(collect())
    for i, value in enumerate(samples):
        while rng.random() < gap:
            dut.sample_valid.value = 0
            await RisingEdge(dut.clk)
        dut.sample_valid.value = 1
        dut.sample.value = value
        dut.sample_hit.value = int(i in (hit_at or ()))
        await RisingEdge(dut.clk)
    dut.sample_valid.value = 0
    dut.sample_hit.value = 0
    for _ in range(5):
        await RisingEdge(dut.clk)
    task.cancel()
    dut.run.value = 0
    await RisingEdge(dut.clk)
    return b"".join(beats), trig


def runs(rng, n):
    out, value = [], 0
    while len(out) < n:
        value = rng.getrandbits(16) if rng.random() < 0.7 else value ^ 1
        out += [value] * rng.choice([1, 1, 2, 5, 40, 300])
    return out[:n]


@cocotb.test()
async def raw_round_trip_and_trigger(dut):
    await setup(dut)
    rng = random.Random(2)
    samples = [rng.getrandbits(16) for _ in range(203)]
    data, trig = await run(dut, samples, rle=False, hit_at={50, 120})
    decoded = list(decode_beats(data, rle=False))
    assert decoded == samples[:len(decoded)] and len(decoded) == 200       # 最後の作りかけの拍は捨てる
    assert trigger_sample(data, False, *trig) == 50                          # 最初のトリガだけ
    assert dut.overrun.value == 0


@cocotb.test()
async def rle_round_trip_long_runs_and_trigger(dut):
    await setup(dut)
    rng = random.Random(3)
    samples = runs(rng, 3000) + [7] * 70000 + runs(rng, 500)               # 65536 を超える繰り返しも
    data, trig = await run(dut, samples, rle=True, hit_at={1234}, gap=0)
    decoded = list(decode_beats(data, rle=True))
    assert decoded == samples[:len(decoded)] and len(decoded) > 70000
    assert trigger_sample(data, True, *trig) == 1234                         # トリガはレコードの頭
    assert decoded[1234] == samples[1234]


@cocotb.test()
async def stalled_output_reports_overrun(dut):
    await setup(dut)
    rng = random.Random(4)
    samples = [rng.getrandbits(16) for _ in range(400)]
    data, _ = await run(dut, samples, rle=False, gap=0, stall=0.9)
    await ReadOnly()
    assert dut.overrun.value == 1
