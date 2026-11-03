"""la_lane_rle（変化点の N 並列の符号器）のテストベンチ。LANES = 8・WIDTH = 16。

入れた細かいサンプルの列と、出た拍をホストの解読（fprobe.la の decode_beats(rle=True)）で戻した列が、先頭から一致する
（無損失）ことを、変化の頻度を変えて確かめる: 変化なし（数の欄の飽和）、まばら、すべてのサンプルで変化（1 周期に 8 レコード）。
溜めが溢れたら overflow が立ち、出た拍は欠けの無い先頭の部分であること。"""

import random

import cocotb
from cocotb.triggers import ReadOnly, RisingEdge

from fprobe.la import decode_beats
from tests.tb.common import start_clock_and_reset

LANES, WIDTH = 8, 16


async def run(dut, samples, *, ready=lambda cycle: True, tail=40):
    """samples（細かいサンプル、LANES の倍数）を 1 周期に LANES 個ずつ入れ、出た拍（バイト列）と overflow の回数を返す。"""
    dut.in_valid.value = 0
    dut.beat_ready.value = 1
    dut.start.value = 1
    await RisingEdge(dut.clk)
    dut.start.value = 0
    beats, overflows = [], [0]
    cycle = [0]

    async def collect():
        while True:
            dut.beat_ready.value = int(ready(cycle[0]))
            await ReadOnly()
            if dut.beat_valid.value == 1 and dut.beat_ready.value == 1:
                beats.append(int(dut.beat_data.value).to_bytes(16, "little"))
            overflows[0] += int(dut.overflow.value)
            await RisingEdge(dut.clk)
            cycle[0] += 1
    task = cocotb.start_soon(collect())
    for w in range(0, len(samples), LANES):
        dut.lane_data.value = sum(v << (WIDTH * k) for k, v in enumerate(samples[w:w + LANES]))
        dut.in_valid.value = 1
        await RisingEdge(dut.clk)
    dut.in_valid.value = 0
    for _ in range(tail):
        await RisingEdge(dut.clk)
    task.cancel()
    return b"".join(beats), overflows[0]


def check_prefix(samples, data):
    got = list(decode_beats(data, True))
    assert got == samples[:len(got)], "解読した列が入れた列の先頭と一致しない"
    return len(got)


@cocotb.test()
async def sparse_changes_round_trip(dut):
    await start_clock_and_reset(dut)
    rng = random.Random(3)
    samples, v = [], 0
    for _ in range(8 * 400):
        if rng.random() < 0.05:
            v = rng.getrandbits(WIDTH)
        samples.append(v)
    data, ovf = await run(dut, samples)
    assert ovf == 0
    n = check_prefix(samples, data)
    assert n > len(samples) - 8 * 40                     # 端数（作りかけ・4 個に満たない溜め）以外は全部出た


@cocotb.test()
async def a_change_at_every_sample_makes_eight_records_a_cycle_and_overflows(dut):
    # 毎サンプルの変化 = 1 周期に 8 レコード。出せるのは 1 周期に 1 拍（4 レコード）なので、溜めが溢れる（事実として立つ）。
    # 出た拍は先頭から欠けなし
    await start_clock_and_reset(dut)
    samples = [i & 0xFFFF for i in range(8 * 100)]
    data, ovf = await run(dut, samples)
    assert ovf == 1                                       # 溢れは 1 回だけ知らせ、以後は入れない
    n = check_prefix(samples, data)
    assert 0 < n < len(samples)
    assert all((r >> 16) == 0 for r in memoryview(data).cast("I"))    # 1 レコード 1 サンプル


@cocotb.test()
async def a_short_burst_of_changes_fits_the_buffer(dut):
    # 毎サンプルの変化が 2 語だけなら溜め（16）に収まり、無損失
    await start_clock_and_reset(dut)
    samples = [0] * 64 + list(range(1, 17)) + [16] * 256
    data, ovf = await run(dut, samples)
    assert ovf == 0
    check_prefix(samples, data)
    assert len(decode_beats(data, True)) >= 64 + 15                  # 最後の続き（16）は閉じていない


@cocotb.test()
async def a_long_constant_run_splits_before_the_count_field_overflows(dut):
    # 変化の無い 3 × 65536 サンプル: 数の欄（16 ビット）に収まる長さでレコードを閉じ、合計が一致する
    await start_clock_and_reset(dut)
    samples = [0x1234] * (3 * 65536) + [0x4321, 0x4321, 0x1111, 0x1111] * 2 * 40     # 後ろは拍を出し切るための変化
    data, ovf = await run(dut, samples)
    assert ovf == 0
    records = list(memoryview(data).cast("I"))
    assert all((r >> 16) + 1 <= 65536 for r in records)
    assert all((r >> 16) + 1 >= 65536 - LANES for r in records if r & 0xFFFF == 0x1234)
    n = check_prefix(samples, data)
    assert n > 3 * 65536


@cocotb.test()
async def a_stalled_receiver_is_held_until_the_buffer_overflows(dut):
    # 受け手が長く止まると溜めが溢れる。受けられる間は無損失
    await start_clock_and_reset(dut)
    rng = random.Random(9)
    samples, v = [], 0
    for _ in range(8 * 300):
        if rng.random() < 0.3:
            v = rng.getrandbits(WIDTH)
        samples.append(v)
    data, ovf = await run(dut, samples, ready=lambda c: (c // 20) % 2 == 0)
    check_prefix(samples, data)
    data2, ovf2 = await run(dut, samples, ready=lambda c: c < 10)
    assert ovf2 == 1
    check_prefix(samples, data2)


@cocotb.test()
async def changes_at_lane_boundaries_and_word_boundaries(dut):
    # 変化の位置を語の中・語の境（レーン 0）のすべてに置く
    await start_clock_and_reset(dut)
    samples = []
    v = 0
    for pos in range(3 * LANES):
        samples += [v] * (5 * LANES + pos)
        v ^= 1
    samples += [v] * (LANES * 8 - len(samples) % LANES)
    data, ovf = await run(dut, samples)
    assert ovf == 0
    assert check_prefix(samples, data) > len(samples) - 8 * LANES
