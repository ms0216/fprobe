"""xilinx_la_iserdes（ISERDESE2 の包み）のテストベンチ。GHDL では ISERDESE2 は tests/rtl/sim_xilinx_prims.vhd の模型。

1. 模型が Vivado の UNISIM の記録（tests/data/iserdes_ref_w<N>.txt。同じビット列を ch 0 に入れた xsim の Q1〜Q8）と同じ出力を出す。
2. 包みのレーンの並び: ch c のレーン k = q[k × CHANNELS + c] = 辺 N × w − 2N + 4 + k のサンプル（k = 0 が最も古い）。
   ch ごとに違うビット列を入れて、ch の取り違え・レーンの順を確かめる。
仕様 docs/superpowers/specs/2026-11-01-la-resolution-design.md §4.1"""

import os
import random
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, Timer

LANES = int(os.environ["ISERDES_LANES"])
CHANNELS = int(os.environ["ISERDES_CHANNELS"])
TC_PS = 6000                       # clk_fast の周期（比だけが意味を持つ）
TD_PS = TC_PS * LANES // 2         # clk_div の周期
DATA = Path(__file__).resolve().parents[1] / "data"
# pynq_z2_pkg.iserdes_newest_age と同じ値（正は fprobe_gw.designs。時刻の表がこの値を段数に使うので、模型で確かめる）
from fprobe_gw.designs import pynq_z2_iserdes_newest_age  # noqa: E402

NEWEST_AGE = pynq_z2_iserdes_newest_age(LANES)


def reference():
    """記録: ビット列 b（b[i] = 辺 i のサンプル）と、立ち上がり w の後の Q（Q8..Q1 の文字列）。"""
    b, qs = None, {}
    for line in (DATA / f"iserdes_ref_w{LANES}.txt").read_text(encoding="utf-8").splitlines():
        if line.startswith("B "):
            b = [int(c) for c in reversed(line[2:])]
        elif line.startswith("Q "):
            _, w, v = line.split()
            qs[int(w)] = v
    return b, qs


async def drive(dut, streams):
    """辺 i（時刻 i × TC/2）で取られる区間に streams[c][i] を置く（辺の TC/4 前に変える）。"""
    slots = len(streams[0])
    value = sum(streams[c][0] << c for c in range(CHANNELS))
    dut.pins.value = value
    await Timer(TC_PS // 4, unit="ps")
    for i in range(1, slots):
        dut.pins.value = sum(streams[c][i] << c for c in range(CHANNELS))
        await Timer(TC_PS // 2, unit="ps")


async def start(dut):
    dut.pins.value = 0
    dut.rst.value = 1
    # 前の試験のクロックが止まった値によらず、両方 0 から同じ時刻に立ち上げる（同じ MMCM の 2 本の出力と同じ）
    dut.clk_fast.value = 0
    dut.clk_div.value = 0
    await Timer(TD_PS, unit="ps")
    cocotb.start_soon(Clock(dut.clk_fast, TC_PS, unit="ps").start())
    cocotb.start_soon(Clock(dut.clk_div, TD_PS, unit="ps").start())


async def collect(dut, words):
    """clk_div の立ち上がり w（時刻 w × TD）の直後の lanes を w ごとに返す。"""
    out = {}
    for w in range(words):
        if w > 0:
            await RisingEdge(dut.clk_div)
        await Timer(1, unit="ps")
        if w == 4:
            dut.rst.value = 0
        out[w] = int(dut.q.value)
    return out


@cocotb.test()
async def model_matches_unisim_record(dut):
    b, qs = reference()
    await start(dut)
    streams = [b] + [[0] * len(b) for _ in range(CHANNELS - 1)]
    cocotb.start_soon(drive(dut, streams))
    got = await collect(dut, max(qs) + 1)
    checked = 0
    for w, q in qs.items():
        if w < 14:                              # 記録の始めは UNISIM の GSR（100 ns）とリセットの間
            continue
        lanes = got[w]
        model = "".join(str(lanes >> ((LANES - j) * CHANNELS) & 1) for j in range(LANES, 0, -1))   # Qj = レーン LANES − j
        unisim = q[8 - LANES:]                  # Q<LANES>..Q1（それより上の Q は DATA_WIDTH の外で使わない）
        assert model == unisim, f"w={w}: 模型 Q{LANES}..Q1 = {model}、UNISIM = {unisim}"
        checked += 1
    assert checked > 40


@cocotb.test()
async def lanes_are_oldest_first_per_channel(dut):
    rng = random.Random(LANES * 100 + CHANNELS)
    slots = 40 * LANES
    streams = [[rng.randrange(2) for _ in range(slots)] for _ in range(CHANNELS)]
    await start(dut)
    cocotb.start_soon(drive(dut, streams))
    got = await collect(dut, slots // LANES - 1)
    checked = 0
    for w, lanes in got.items():
        if w < 6:
            continue
        for k in range(LANES):
            i = LANES * w - 2 * LANES + 4 + k
            assert k != LANES - 1 or i == LANES * w - NEWEST_AGE
            for c in range(CHANNELS):
                assert lanes >> (k * CHANNELS + c) & 1 == streams[c][i], f"w={w} k={k} c={c}"
        checked += 1
    assert checked > 20
