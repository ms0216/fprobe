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


def boundary_runs(lengths, rng):
    """長さ lengths の繰り返しを、隣と値が必ず違うように並べる（RLE のカウンタの境界: 65535・65536・65537 サンプル等）。"""
    out, prev = [], None
    for n in lengths:
        value = rng.getrandbits(16)
        while value == prev:
            value = rng.getrandbits(16)
        out += [value] * n
        prev = value
    return out


@cocotb.test()
async def rle_counter_boundaries_round_trip(dut):
    """繰り返しの長さがカウンタの上限（count = 65535 = 65536 サンプル）の前後でも元の列に戻り、トリガは上限で切れた
    次のレコードの頭（65536 サンプル目）でも拍の位置が合う。"""
    await setup(dut)
    rng = random.Random(11)
    lengths = [65535, 65536, 65537, 1, 131072, 2, 65534, 3, 65536, 1, 1, 1, 1]
    samples = boundary_runs(lengths, rng)
    hit = 65535 + 65536 + 65536                                              # 3 本目の繰り返しのちょうど上限の次
    data, trig = await run(dut, samples, rle=True, hit_at={hit}, gap=0)
    decoded = list(decode_beats(data, rle=True))
    assert decoded == samples[:len(decoded)] and len(decoded) > 300_000      # 最後の作りかけの拍と開いたレコードは出ない
    assert trigger_sample(data, True, *trig) == hit


@cocotb.test()
async def random_traces_with_random_triggers_match_the_model(dut):
    """ランダムな列・隙間・出力の止まりで RAW と RLE を流し、復号が元の列に戻る。トリガは最初の 1 回の位置（RLE は
    そのサンプルから始まるレコード、RAW はそのサンプルの番号）。"""
    await setup(dut)
    for seed in range(12):
        rng = random.Random(100 + seed)
        rle = bool(seed & 1)
        n = rng.choice([1, 7, 8, 9, 31, 32, 33, 250, 1000])
        samples = runs(rng, n) if rle else [rng.getrandbits(16) for _ in range(n)]
        hits = {rng.randrange(n) for _ in range(rng.choice([0, 1, 3]))}
        data, trig = await run(dut, samples, rle=rle, hit_at=hits, gap=rng.choice([0, 0.3, 0.8]), rng=rng)
        decoded = list(decode_beats(data, rle=rle))
        assert decoded == samples[:len(decoded)], (seed, rle, n)
        if hits and min(hits) < len(decoded):
            assert trig is not None, (seed, rle, n, sorted(hits))
            assert trigger_sample(data, rle, *trig) == min(hits), (seed, rle, n, sorted(hits))
        if not hits:
            assert trig is None, (seed, rle, n)
