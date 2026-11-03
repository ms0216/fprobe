"""la_pack（使うチャンネルだけ詰める: 1 サンプル 16 / 8 / 4 / 2 ビットの連続サンプルを 1 つの 16 ビットの語にする）のテストベンチ。

語の中は、時間の古いサンプルが下位（la_encoder の RAW の拍と同じ向き）。"""

import random

import cocotb
from cocotb.triggers import ReadOnly, RisingEdge

from tests.tb.common import start_clock_and_reset


async def setup(dut):
    for name in ("run", "sample_valid", "sample", "sample_hit", "pack"):
        getattr(dut, name).value = 0
    await start_clock_and_reset(dut)


async def feed(dut, samples, *, pack, hits=(), gap=0.3, rng=None):
    """samples を流し、出てきた (語, hit) の列を返す。"""
    rng = rng or random.Random(1)
    out = []
    dut.pack.value = pack
    dut.run.value = 1
    await RisingEdge(dut.clk)

    async def collect():
        while True:
            await ReadOnly()
            if dut.out_valid.value == 1:
                out.append((int(dut.out_sample.value), int(dut.out_hit.value)))
            await RisingEdge(dut.clk)

    task = cocotb.start_soon(collect())
    for i, value in enumerate(samples):
        while rng.random() < gap:
            dut.sample_valid.value = 0
            await RisingEdge(dut.clk)
        dut.sample_valid.value = 1
        dut.sample.value = value
        dut.sample_hit.value = int(i in hits)
        await RisingEdge(dut.clk)
    dut.sample_valid.value = 0
    dut.sample_hit.value = 0
    for _ in range(5):
        await RisingEdge(dut.clk)
    task.cancel()
    dut.run.value = 0
    await RisingEdge(dut.clk)
    return out


def expected(samples, pack):
    """pack = 0..3: 1 サンプル 16 >> pack ビット、1 語に 1 << pack サンプル。端数のサンプルは出さない。"""
    width, k = 16 >> pack, 1 << pack
    mask = (1 << width) - 1
    words = []
    for i in range(0, len(samples) - len(samples) % k, k):
        words.append(sum((samples[i + j] & mask) << (width * j) for j in range(k)))
    return words


@cocotb.test()
async def pack_0_passes_every_sample_through_unchanged(dut):
    await setup(dut)
    rng = random.Random(2)
    samples = [rng.getrandbits(16) for _ in range(100)]
    out = await feed(dut, samples, pack=0, hits={7, 50})
    assert [w for w, _ in out] == samples
    assert [i for i, (_, h) in enumerate(out) if h] == [7, 50]


@cocotb.test()
async def narrow_samples_are_packed_oldest_in_the_low_bits(dut):
    await setup(dut)
    for pack in (1, 2, 3):
        rng = random.Random(10 + pack)
        samples = [rng.getrandbits(16) for _ in range(203)]
        out = await feed(dut, samples, pack=pack)
        assert [w for w, _ in out] == expected(samples, pack), pack


@cocotb.test()
async def a_word_has_the_trigger_when_any_of_its_samples_has_it(dut):
    await setup(dut)
    samples = list(range(1, 41))
    out = await feed(dut, samples, pack=2, hits={5, 6, 30})                  # k = 4: 語 1 に 5・6 の両方、語 7 に 30
    assert [i for i, (_, h) in enumerate(out) if h] == [1, 7]


@cocotb.test()
async def run_low_forgets_the_partial_word_so_the_next_run_starts_aligned(dut):
    await setup(dut)
    first = await feed(dut, [0xAB, 0xCD, 0xEF], pack=1)                      # 1 語（AB, CD）+ 端数 EF
    assert [w for w, _ in first] == [0xCDAB]
    second = await feed(dut, [0x11, 0x22, 0x33, 0x44], pack=1)               # 端数 EF は持ち越さない
    assert [w for w, _ in second] == [0x2211, 0x4433]
