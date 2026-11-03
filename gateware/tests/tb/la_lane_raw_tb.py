"""la_lane_raw（RAW の N 並列の詰め）のテストベンチ。LANES = 8・WIDTH = 16。

毎周期ランダムな語（8 レーン × 16 ビット）を入れ、出た拍をホストの解読（fprobe.la の decode_beats・_expand_words）で
サンプルに戻し、選んだサンプル（細かいサンプルの 2^step 個ごと、語は rate + 1 語ごと）・選んだ ch（下位 16 >> pack ビット）と
欠けも重複もなく一致することを確かめる（往復が無損失）。"""

import random

import cocotb
from cocotb.triggers import ReadOnly, RisingEdge

from fprobe.la import _expand_words, decode_beats
from tests.tb.common import start_clock_and_reset

LANES, WIDTH = 8, 16


async def run(dut, *, step, pack, rate, words, seed, first=0):
    rng = random.Random(seed)
    dut.in_valid.value = 0
    dut.step.value, dut.pack.value, dut.rate.value, dut.first_ch.value = step, pack, rate, first
    dut.start.value = 1
    await RisingEdge(dut.clk)
    dut.start.value = 0
    sent, beats = [], []

    async def collect():
        while True:
            await ReadOnly()
            if dut.beat_valid.value == 1:
                beats.append(int(dut.beat_data.value).to_bytes(16, "little"))
            await RisingEdge(dut.clk)
    task = cocotb.start_soon(collect())
    for _ in range(words):
        lanes = [rng.getrandbits(WIDTH) for _ in range(LANES)]
        sent.append(lanes)
        dut.lane_data.value = sum(v << (WIDTH * k) for k, v in enumerate(lanes))
        dut.in_valid.value = 1
        await RisingEdge(dut.clk)
    dut.in_valid.value = 0
    for _ in range(4):
        await RisingEdge(dut.clk)
    task.cancel()
    return sent, b"".join(beats)


def expected(sent, *, step, pack, rate, first=0):
    c = WIDTH >> pack
    out = []
    for i, lanes in enumerate(sent):
        if i % (rate + 1):
            continue
        out.extend(lanes[j << step] >> first & ((1 << c) - 1) for j in range(LANES >> step))
    return out


@cocotb.test()
async def every_step_and_pack_round_trips_without_loss(dut):
    dut.first_ch.value = 0
    await start_clock_and_reset(dut)
    for step in range(4):
        for pack in range(5):
            rate = 0 if step < 3 else 2
            sent, data = await run(dut, step=step, pack=pack, rate=rate, words=200, seed=step * 10 + pack)
            got = list(_expand_words(decode_beats(data, False), pack))
            want = expected(sent, step=step, pack=pack, rate=rate)
            per_beat = 128 // (WIDTH >> pack)
            assert len(data) // 16 == len(want) // per_beat, (step, pack)       # 揃った拍は全部出る（端数は出さない）
            assert got == want[:len(got)], (step, pack)


@cocotb.test()
async def the_first_channel_selects_which_channels_are_packed(dut):
    # ch first〜first + c − 1 を詰める（16 を超える ch は 0）
    await start_clock_and_reset(dut)
    for pack, first in ((4, 10), (3, 10), (2, 13), (1, 9), (0, 5)):
        sent, data = await run(dut, step=0, pack=pack, rate=0, words=160, seed=40 + first, first=first)
        got = list(_expand_words(decode_beats(data, False), pack))
        want = expected(sent, step=0, pack=pack, rate=0, first=first)
        assert got and got == want[:len(got)], (pack, first)


@cocotb.test()
async def start_discards_the_partial_beat(dut):
    await start_clock_and_reset(dut)
    # 1 ch（pack 4）・1 語 8 ビット: 16 語で 1 拍。5 語だけ入れて始め直すと、作りかけは出ず、次の流れは最初の語から
    await run(dut, step=0, pack=4, rate=0, words=5, seed=1)
    sent, data = await run(dut, step=0, pack=4, rate=0, words=32, seed=2)
    assert list(_expand_words(decode_beats(data, False), 4)) == expected(sent, step=0, pack=4, rate=0)
