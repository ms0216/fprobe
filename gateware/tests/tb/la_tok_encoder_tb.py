"""la_tok_encoder（トークン形式の符号器）のテストベンチ。出力の拍を、ホストの参照符号器 fprobe.la_tokens.encode_tokens と
バイト単位で比べる（環境変数 LTE_REPEAT = 1 のとき REPEAT_EN = true の回路を repeat = 1 で動かし、参照符号器も repeat = True。0 なら repeat = False）。
REPEAT の繰り返し回数の上限は generic REPEAT_N_BITS（環境変数 LTE_NBITS で小さくして、参照符号器の REPEAT_N_MAX も同じ値にする）。長い間隔の分割は、generic（TOGGLE_DELTA_BITS 等）を小さくした構成で、参照符号器の同じ上限も同じ値にして比べる
（環境変数 LTE_SMALL = 1。実際の 2^40 を待たずに分割の規則を確かめる）。"""

import os
import random

import cocotb
from cocotb.triggers import ReadOnly, RisingEdge
from fprobe import la_tokens
from fprobe.la_tokens import decode_tokens, encode_tokens
from tests.tb.common import start_clock_and_reset

SMALL = os.environ.get("LTE_SMALL") == "1"
REP = os.environ.get("LTE_REPEAT") == "1"
if os.environ.get("LTE_NBITS"):
    la_tokens.REPEAT_N_MAX = 1 << int(os.environ["LTE_NBITS"])
if SMALL:                                  # generic（TOGGLE_DELTA_BITS = 9、VALUE_DELTA_BITS = 6、WAIT_BITS = 8）と同じ上限
    la_tokens._TOGGLE_DELTA_MAX = la_tokens.DELTA_EXT + (1 << 9) - 1
    la_tokens._VALUE_DELTA_MAX = (1 << 6) - 1
    la_tokens.WAIT_MAX = (1 << 8) - 1


async def setup(dut):
    for name in ("run", "sample_valid", "sample", "resync"):
        getattr(dut, name).value = 0
    dut.tick.value = 0
    dut.repeat.value = int(REP)
    dut.out_ready.value = 1
    await start_clock_and_reset(dut)


async def run_stream(dut, samples, *, tick=0, resync_at=(), ready=None, idle_after=600):
    """samples を 1 周期 1 サンプルで流し、出た拍（data, pos, kf）の列と、最後の overrun を返す。
    ready(周期) が偽の周期は out_ready = 0（None なら常に 1）。"""
    beats = []
    dut.tick.value = tick
    dut.run.value = 1
    dut.sample_valid.value = 0
    await RisingEdge(dut.clk)
    cycle = 0
    resync_at = set(resync_at)

    async def collect():
        nonlocal cycle
        while True:
            dut.out_ready.value = 1 if ready is None else int(ready(cycle))
            await ReadOnly()
            if dut.out_valid.value == 1 and dut.out_ready.value == 1:
                beats.append((int(dut.out_data.value).to_bytes(16, "little"), int(dut.out_pos.value), int(dut.out_kf.value)))
            await RisingEdge(dut.clk)
            cycle += 1

    task = cocotb.start_soon(collect())
    for i, v in enumerate(samples):
        dut.sample_valid.value = 1
        dut.sample.value = v
        dut.resync.value = int(i in resync_at)
        await RisingEdge(dut.clk)
    dut.sample_valid.value = 0
    dut.resync.value = 0
    for _ in range(idle_after):
        await RisingEdge(dut.clk)
    dut.run.value = 0
    for _ in range(idle_after):
        await RisingEdge(dut.clk)
    task.cancel()
    return beats, int(dut.overrun.value)


def runs(rng, n, *, long_idle=False, wide=False):
    """変化の様子を混ぜた列: 1 本の反転・数本の変化・全ビット・長い停止。"""
    out, value = [], rng.getrandbits(16)
    while len(out) < n:
        r = rng.random()
        if r < 0.45:
            value ^= 1 << rng.randrange(16)
        elif r < 0.7:
            for _ in range(rng.choice([2, 3])):
                value ^= 1 << rng.randrange(16)
        elif r < 0.8 and wide:
            value = rng.getrandbits(16)
        elif r < 0.85 and wide:
            value ^= 0xFFFF
        out.append(value)
        hold = rng.choice([0, 0, 0, 1, 2, 3, 5, 6, 7, 8, 20, 70, 71, 100])
        if long_idle and rng.random() < 0.05:
            hold = rng.choice([8198, 8199, 8200, 9000, 2047, 2048, 3000])
        out += [value] * hold
    return out[:n]


def check(beats, samples, **kw):
    data = b"".join(b for b, _, _ in beats)
    kw.setdefault("repeat", REP)
    ref = encode_tokens(samples, **kw)
    first = next((i for i in range(min(len(data), len(ref))) if data[i] != ref[i]), None)
    assert data == ref, f"拍が参照符号器と違う: {len(data)} バイト（参照 {len(ref)} バイト）、最初の違い {first}"


def ref_pad(samples, **kw):
    return decode_tokens(encode_tokens(samples, repeat=REP, **kw)).pad_bytes


@cocotb.test()
async def random_short_intervals(dut):
    await setup(dut)
    rng = random.Random(11)
    samples = runs(rng, 3000)
    beats, ovr = await run_stream(dut, samples)
    assert ovr == 0
    check(beats, samples)
    assert dut.pad_bytes.value.to_unsigned() == ref_pad(samples)


@cocotb.test()
async def wide_changes_k3_k4(dut):
    """全ビット・k = 3/4 の境・毎サンプルの変化。"""
    await setup(dut)
    rng = random.Random(12)
    samples = []
    for _ in range(400):
        mask = 0
        for _ in range(rng.choice([1, 2, 3, 3, 4, 4, 5, 16])):
            mask ^= 1 << rng.randrange(16)
        samples.append((samples[-1] if samples else 0) ^ mask)
    samples += [rng.getrandbits(16) for _ in range(300)] + [0xFFFF, 0, 0xFFFF, 0, 0x5555, 0xAAAA]
    if REP:                                # REPEAT の束のまとめを閉じる周期の分、毎サンプルの変化を 3 周期に 1 回にする
        samples = [x for v in samples for x in (v, v, v)]
    beats, ovr = await run_stream(dut, samples)
    assert ovr == 0
    check(beats, samples)


@cocotb.test()
async def three_byte_groups_pad_nop(dut):
    """3 バイトの束が続く（1 拍に 5 個 + NOP 1 バイト）。"""
    await setup(dut)
    samples = []
    v = 0
    for i in range(300):
        v ^= 0b111 << (i % 3)           # 3 本が同時に変わる
        samples.append(v)
    beats, ovr = await run_stream(dut, samples)
    assert ovr == 0
    check(beats, samples)
    assert dut.pad_bytes.value.to_unsigned() > 0


@cocotb.test()
async def long_intervals(dut):
    """間隔 7・70・71・8198・8199 の前後、VALUE の 2047・2048 前後（小さい構成は上限の前後）。"""
    await setup(dut)
    if SMALL:
        gaps = [1, 6, 7, 70, 71, 100, 517, 518, 519, 520, 600, 1100, 1550, 2000]
        vgaps = [1, 62, 63, 64, 65, 70, 100, 255, 256, 257, 300, 600]
    else:
        gaps = [1, 6, 7, 70, 71, 8198, 8199, 8200, 8300, 20000]
        vgaps = [1, 15, 16, 17, 2046, 2047, 2048, 2049, 4000]
    samples, v = [0], 0
    for g in gaps:
        samples += [v] * g
        v ^= 1 << (g % 16)
        samples.append(v)
    for g in vgaps:
        samples += [v] * g
        v ^= 0b11110000 if g % 2 else 0x0F0F
        samples.append(v)
    for g in gaps[:6]:
        samples += [v] * g
        v ^= 0b111
        samples.append(v)
    beats, ovr = await run_stream(dut, samples, idle_after=900)
    assert ovr == 0
    check(beats, samples)


@cocotb.test()
async def tick_flush_and_wait(dut):
    await setup(dut)
    rng = random.Random(13)
    for tick in (16, 40, 100):
        samples = runs(rng, 1500, wide=False)
        samples = samples[:300] + [samples[299]] * 700 + samples[300:600]     # 長い無変化 → tick の WAIT
        beats, ovr = await run_stream(dut, samples, tick=tick)
        assert ovr == 0, tick
        check(beats, samples, tick_samples=tick)
        await setup(dut)


@cocotb.test()
async def resync_samples(dut):
    """再同期: そのサンプルは VALUE、直前に拍を払う。kf の印は再同期の項目から始まる拍。"""
    await setup(dut)
    rng = random.Random(14)
    samples = runs(rng, 1200, wide=True)
    resync_at = [5, 6, 100, 101, 400, 777, 1000]
    beats, ovr = await run_stream(dut, samples, resync_at=resync_at)
    assert ovr == 0
    check(beats, samples, resync_at=resync_at)
    assert dut.keyframes.value.to_unsigned() == len(resync_at)
    assert any(kf for _, _, kf in beats)


@cocotb.test()
async def tick_and_resync_together(dut):
    await setup(dut)
    rng = random.Random(15)
    samples = runs(rng, 2000)
    resync_at = [50, 51, 800, 1500]
    beats, ovr = await run_stream(dut, samples, tick=64, resync_at=resync_at)
    assert ovr == 0
    check(beats, samples, tick_samples=64, resync_at=resync_at)


@cocotb.test()
async def decode_matches_samples(dut):
    """出力を復号すると元のサンプルになる（参照符号器との一致と別に、仕様の意味で）。"""
    await setup(dut)
    rng = random.Random(16)
    samples = runs(rng, 2500, wide=True)
    beats, _ = await run_stream(dut, samples)
    data = b"".join(b for b, _, _ in beats)
    out = la_tokens.expand_samples(data)
    assert list(out) == samples[:len(out)] and len(out) >= len(samples) - 150


@cocotb.test()
async def overrun_when_not_ready(dut):
    """受け手が止まると拍を捨てて overrun を立てる（黙って捨てない）。ready なら立たない。"""
    await setup(dut)
    rng = random.Random(17)
    samples = [rng.getrandbits(16) for _ in range(300)]
    beats, ovr = await run_stream(dut, samples, ready=lambda c: c % 50 < 5)
    assert ovr == 1
    await setup(dut)
    beats, ovr = await run_stream(dut, samples)
    assert ovr == 0


@cocotb.test()
async def pos_is_last_token_time(dut):
    """out_pos = その拍の最後のトークンの時刻（サンプルの番号）。復号の最後の出来事の時刻と、拍ごとに一致する。"""
    await setup(dut)
    rng = random.Random(18)
    samples = runs(rng, 1500, wide=True)
    beats, _ = await run_stream(dut, samples)
    first, prev = 0, None
    for data, pos, _ in beats:
        d = decode_tokens(data, first, prev)
        assert pos == d.end_time, (pos, d.end_time)
        first, prev = d.end_time, d.last_value


def bursty(rng, n, *, holds=(1, 2, 3, 4, 5, 6, 7, 8, 70, 71, 100, 600), counts=(1, 2, 3, 4, 5, 9, 33, 40, 100)):
    """周期的な信号（クロック・デューティが 50 % でない・2 本の交互）と、乱数の変化・長い間隔を混ぜた列。REPEAT の境を見る。"""
    out, value = [0], 0
    while len(out) < n:
        kind = rng.random()
        if kind < 0.35:                                  # 1 本のクロック（半周期 h を count 回）
            ch, h = rng.randrange(16), rng.choice(holds)
            for _ in range(rng.choice(counts)):
                value ^= 1 << ch
                out += [value] * h
        elif kind < 0.55:                                # デューティが 50 % でない（B = 2）
            ch, h1, h2 = rng.randrange(16), rng.choice(holds), rng.choice(holds)
            for _ in range(rng.choice(counts)):
                value ^= 1 << ch
                out += [value] * h1
                value ^= 1 << ch
                out += [value] * h2
        elif kind < 0.65:                                # 2 本が交互に（SCK と別の線）
            a, b, h = rng.randrange(16), rng.randrange(16), rng.choice(holds)
            for _ in range(rng.choice(counts)):
                value ^= 1 << a
                out += [value] * h
                value ^= 1 << b
                out += [value] * h
        elif kind < 0.8:                                 # 乱数の 1 本の変化
            for _ in range(rng.randrange(1, 6)):
                value ^= 1 << rng.randrange(16)
                out += [value] * rng.choice(holds)
        elif kind < 0.9:                                 # 同時に複数・全体の変化（束・VALUE は REPEAT にならない）
            value ^= rng.getrandbits(16) if rng.random() < 0.5 else (0b11 << rng.randrange(14))
            out += [value] * rng.choice(holds)
        else:                                            # 長い無変化
            out += [value] * rng.choice([8198, 8199, 9000, 20000, 2047, 3000])
    return out[:n]


@cocotb.test()
async def repeat_bursts(dut):
    """周期的な信号の繰り返し（N = 1・2・3…、B = 1・2、途中で崩れる）を、参照符号器とバイト単位で比べる。"""
    for seed in range(3):
        await setup(dut)
        rng = random.Random(100 + seed)
        samples = bursty(rng, 6000)
        beats, ovr = await run_stream(dut, samples, idle_after=900)
        assert ovr == 0, seed
        check(beats, samples)
        if REP:
            assert decode_tokens(b"".join(b for b, _, _ in beats)).repeat_tokens > 0


@cocotb.test()
async def repeat_pure_clock(dut):
    """一定周期のクロック: REPEAT でほとんど 0 バイト、周期を変えて新しい REPEAT。"""
    await setup(dut)
    samples, v = [0], 0
    for h, k in ((4, 3000), (5, 2000), (3, 40), (70, 500), (2, 7), (1, 90 if not os.environ.get("LTE_NBITS") else 0)):
        for _ in range(k):
            v ^= 1
            samples += [v] * h
    beats, ovr = await run_stream(dut, samples, idle_after=900)
    assert ovr == 0
    check(beats, samples)
    if REP:
        assert 16 * len(beats) < len(samples) // 20


@cocotb.test()
async def repeat_with_tick(dut):
    """tick の払いとの組み合わせ（まとめを持ったまま tick が来る・まとめの閉じで拍が出る）。"""
    rng = random.Random(21)
    for tick in (40, 100, 333):
        await setup(dut)
        samples = bursty(rng, 4000)
        beats, ovr = await run_stream(dut, samples, tick=tick, idle_after=900)
        assert ovr == 0, tick
        check(beats, samples, tick_samples=tick)


@cocotb.test()
async def repeat_with_resync(dut):
    await setup(dut)
    rng = random.Random(22)
    samples = bursty(rng, 4000)
    resync_at = [3, 4, 50, 51, 52, 700, 701, 1500, 2600, 3000, 3900]
    beats, ovr = await run_stream(dut, samples, resync_at=resync_at, idle_after=900)
    assert ovr == 0
    check(beats, samples, resync_at=resync_at)
    assert dut.keyframes.value.to_unsigned() == len(resync_at)


@cocotb.test()
async def repeat_tick_and_resync(dut):
    await setup(dut)
    rng = random.Random(23)
    samples = bursty(rng, 4000)
    resync_at = [30, 31, 900, 2000, 3500]
    beats, ovr = await run_stream(dut, samples, tick=64, resync_at=resync_at, idle_after=900)
    assert ovr == 0
    check(beats, samples, tick_samples=64, resync_at=resync_at)


@cocotb.test()
async def repeat_positions_and_decode(dut):
    """out_pos = 拍の最後の出来事の時刻（REPEAT の展開後）。復号が元のサンプルになる。"""
    await setup(dut)
    rng = random.Random(24)
    samples = bursty(rng, 5000)
    beats, _ = await run_stream(dut, samples, idle_after=900)
    first, prev = 0, None
    for data, pos, _ in beats:
        d = decode_tokens(data, first, prev)
        assert pos == d.end_time, (pos, d.end_time)
        first, prev = d.end_time, d.last_value
    out = la_tokens.expand_samples(b"".join(b for b, _, _ in beats))
    assert list(out) == samples[:len(out)] and len(out) >= len(samples) - 20000


@cocotb.test()
async def repeat_backpressure_no_silent_loss(dut):
    """受け手が止まっても、黙って壊れない（拍を捨てたら overrun を立てる）。ready なら立たない。"""
    await setup(dut)
    rng = random.Random(25)
    samples = bursty(rng, 3000)
    beats, ovr = await run_stream(dut, samples, ready=lambda c: c % 40 < 3)
    if ovr == 0:
        check(beats, samples)
    await setup(dut)
    beats, ovr = await run_stream(dut, samples, idle_after=900)
    assert ovr == 0


@cocotb.test()
async def repeat_fuzz(dut):
    """乱数の組み合わせ（周期的な信号・tick・再同期）。環境変数 LTE_FUZZ = 試行の数（既定 0 = しない。長いので普段は走らせない）。"""
    overruns = 0
    for seed in range(int(os.environ.get("LTE_FUZZ", "0"))):
        rng = random.Random(1000 + seed)
        await setup(dut)
        n = rng.randrange(500, 5000)
        samples = bursty(rng, n, holds=rng.choice([(1, 2, 3, 4), (4, 5), (2, 3, 7, 70), (1, 6, 7, 8, 100)]),
                         counts=rng.choice([(1, 2, 3), (4, 5, 9, 33), (40, 100)]))
        tick = rng.choice([0, 0, 40, 64, 100, 777])
        resync_at = sorted(rng.sample(range(1, n), rng.choice([0, 0, 1, 3, 8])))
        beats, ovr = await run_stream(dut, samples, tick=tick, resync_at=resync_at, idle_after=1200)
        if ovr:                          # 溜めが溢れた（密な列 + 小さい tick。黙って捨てず overrun を立てる設計）: 数えて、一致の比べはしない
            dut._log.warning("fuzz seed %d: overrun (tick %d)", seed, tick)
            overruns += 1
            continue
        check(beats, samples, tick_samples=tick or None, resync_at=resync_at)
    assert overruns <= int(os.environ.get("LTE_FUZZ", "0")) // 4, overruns


def square(period, n):
    """周期 period（半周期 period / 2 サンプル）の 1 本の方形波 n サンプル（出来事の間隔 = period / 2）。"""
    h = period // 2
    return [(i // h) & 1 for i in range(n)]


@cocotb.test()
async def repeat_dense_clock_with_tick(dut):
    """間隔 1・2 サンプルの出来事が続く方形波 + tick で REPEAT のまとめを閉じ続けても、受け手が 1 拍/周期で取るなら溜めが溢れない
    （実機 PYNQ-Z2 100 MS/s で見つかった不具合: 閉じの間の後段の遅れが取り戻せず、tick の数回で overrun）。参照符号器とバイト単位で一致する。"""
    if os.environ.get("LTE_NBITS"):      # 繰り返しの回数の上限を 8 に絞った構成では、8 回ごとに閉じる（上限の閉じは前段の処理を止める）ので対象外
        return
    for period in (2, 4, 6, 8):
        for tick in ((23, 97, 200, 1000) if period <= 4 else (97, 200, 1000)):   # 小さい tick（約 8 周期ごとに閉じる）は間隔 1・2 だけ
            await setup(dut)
            samples = square(period, 30000)
            beats, ovr = await run_stream(dut, samples, tick=tick, idle_after=900)
            assert ovr == 0, (period, tick)
            check(beats, samples, tick_samples=tick)
