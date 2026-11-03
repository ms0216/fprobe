"""深いバッファを 1 周期に 8 サンプルで記録する構成（fprobe_pynq_deep_fast と同じつなぎ方）の fprobe_core
（tests/rtl/fprobe_core_deep_lanes_harness.vhd）。CSR とケイパビリティ越しに、ホストの解読（fprobe.caps・fprobe.la）と同じ読み方で
確かめる: ケイパビリティと la_deep.info、RAW の詰め（1 ch・ピン 0）と RLE の流れが 1.25 ns の格子に揃って欠けないこと、リングの
使用量・起点の時刻のレジスタ。流れは PL がリングを読み戻してストリームの口に出す形（PS の直読みは la_deep_buffer の試験で見る）。
仕様 docs/superpowers/specs/2026-11-01-la-deep-fast-design.md §8"""

import random

import cocotb
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge, Timer

from fprobe import generated as g
from fprobe.caps import parse
from fprobe.la import SegmentSplitter, _expand_words, decode_beats
from tests.models.mem_port import MemPortModel
from tests.tb.common import start_clock_and_reset
from tests.tb.control_port import ControlPort

N = 8
SYS_PS = 16000
FINE_PS = SYS_PS // N


async def setup(dut):
    for name in ("aux_rx",):
        getattr(dut, name).value = 1
    for name in ("sync_in", "la_pins", "la_inputs", "uart_in_valid", "uart_in_data", "dap_in_valid", "mem_rd_valid",
                 "mem_rd_data", "mem_cmd_ready"):
        getattr(dut, name).value = 0
    dut.stream_out_ready.value = 1
    dut.dap_out_ready.value = 1
    dut.mem_ready.value = 1
    dut.target_swdio.value = "Z"
    port = ControlPort(dut, request="ctrl_in", response="ctrl_out", rng=random.Random(5))
    await start_clock_and_reset(dut, period_ns=SYS_PS // 1000, cap_ratio=N // 2)
    mem = MemPortModel(dut, prefix="mem_", size_beats=1 << 10, stall=0.2)
    cocotb.start_soon(mem.run())
    return port


async def read_word(port, address):
    status, _, words = await port.read(address, 1)
    assert status == g.STATUS_OK
    return words[0]


async def square(dut, half_fine):
    """ピン 0 を、細かいサンプルの half_fine 個ごとに反転する（clk の立ち上がりの細かい辺の間で変える）。"""
    await RisingEdge(dut.clk)
    await Timer(FINE_PS // 2, unit="ps")
    v = 0
    while True:
        await Timer(half_fine * FINE_PS, unit="ps")
        v ^= 1
        dut.la_pins.value = v


async def stream(dut, port, *, cycles, rle=0, pack=0, step=0):
    """垂れ流しを cycles 周期流して止め、出し切るまで受けたロジアナの拍（見出しを除いて並べたもの）を返す。"""
    got = bytearray()

    async def collect():
        while True:
            await ReadOnly()
            if int(dut.stream_out_valid.value) and int(dut.stream_out_ready.value):
                got.append(int(dut.stream_out_data.value))
            await RisingEdge(dut.clk)
    task = cocotb.start_soon(collect())
    await port.write(g.LA_DEEP_CONTROL, [0])
    await port.write(g.LA_RATE, [0, 0, 0, 0, 0, 0])
    await port.write(g.LA_DEEP_POST_BEATS, [0])
    await port.write(g.LA_DEEP_CONTROL, [1 | rle << g.LA_DEEP_CONTROL_RLE[0] | 1 << g.LA_DEEP_CONTROL_STREAM[0]
                                         | pack << g.LA_DEEP_CONTROL_PACK[0] | step << g.LA_DEEP_CONTROL_STEP[0]])
    await ClockCycles(dut.clk, cycles)
    stats = [await read_word(port, a) for a in (g.LA_DEEP_LA_WRITTEN, g.LA_DEEP_RING_UNREAD, g.LA_DEEP_RING_PEAK)]
    await port.write(g.LA_DEEP_CONTROL, [0])
    for _ in range(400):
        status = await read_word(port, g.LA_DEEP_STATUS)
        if not status >> g.LA_DEEP_STATUS_READING[0] & 1:
            break
    else:
        raise AssertionError("出し切らない")
    await ClockCycles(dut.clk, 40)
    task.cancel()
    la = b"".join(seg.data for seg in SegmentSplitter().feed(bytes(got)) if seg.kind == g.STREAM_SEG_LA)
    return la, stats, status


def runs(bits):
    out, n = [], 1
    for a, b in zip(bits, bits[1:]):
        if a == b:
            n += 1
        else:
            out.append(n)
            n = 1
    return out


@cocotb.test()
async def capabilities_and_info_declare_the_lanes_of_the_deep_buffer(dut):
    port = await setup(dut)
    _, _, header = await port.read(g.CAPS_BASE, 2)
    _, _, words = await port.read(g.CAPS_BASE, header[1] // 4)
    caps = parse(b"".join(w.to_bytes(4, "little") for w in words))
    assert caps.la_sample_hz == caps.sys_clock_hz * N and caps.mem_bytes and caps.la_depth == 0
    assert caps.la_fine == 0xFCFF and caps.la_pack
    assert caps.la_stream_gaps                                       # 欠けの区間の記録（段 B）の申告と窓
    assert await read_word(port, g.LA_DEEP_GAPS) == 0 and await read_word(port, g.LA_DEEP_GAP_LAST_BEAT) == 0
    info = await read_word(port, g.LA_DEEP_INFO)
    assert info >> g.LA_DEEP_INFO_LANES[0] & 0xF == N
    assert info >> g.LA_DEEP_INFO_LATENCY[0] & 0xFF == N + (N - 3)
    cap_info = await read_word(port, g.LA_CAP_INFO)
    assert cap_info >> g.LA_CAP_INFO_RATIO[0] & 0xF == N and cap_info >> g.LA_CAP_INFO_LANES[0] & 0xF == N


@cocotb.test()
async def raw_one_channel_keeps_every_fine_sample(dut):
    port = await setup(dut)
    cocotb.start_soon(square(dut, 5))                                # 細かいサンプル 5 個ごとに反転（80 MHz の方形波）
    la, (written, unread, peak), status = await stream(dut, port, cycles=1500, pack=4)
    assert not status >> g.LA_DEEP_STATUS_OVERRUN[0] & 1
    bits = list(_expand_words(decode_beats(la, False), 4))
    assert len(bits) > 1000 * N
    assert set(runs(bits)[1:-1]) == {5}                              # 格子に揃い、欠けも重複も無い
    assert 0 < written <= len(la) // 16 and peak >= unread           # 止める前に読んだ値（その後も書く）
    info = await read_word(port, g.LA_DEEP_INFO)
    assert info >> g.LA_DEEP_INFO_START_VALID[0] & 1
    lo, hi = (await port.read(g.LA_DEEP_START_TIME_LO, 2))[2]
    assert 0 < (hi << 32 | lo)


@cocotb.test()
async def rle_keeps_every_change_and_step_thins_nothing_but_the_requested_interval(dut):
    port = await setup(dut)
    cocotb.start_soon(square(dut, 13))
    la, _, status = await stream(dut, port, cycles=1500, rle=1)
    assert not status >> g.LA_DEEP_STATUS_OVERRUN[0] & 1
    samples = list(decode_beats(la, True))
    assert set(runs([s & 1 for s in samples])[1:-1]) == {13}
    # RAW・細かいサンプルの 2 個ごと（400 MS/s）: 長さ 13 の区間は 6 か 7 サンプル
    la, _, status = await stream(dut, port, cycles=1500, pack=4, step=1)
    assert not status >> g.LA_DEEP_STATUS_OVERRUN[0] & 1
    bits = list(_expand_words(decode_beats(la, False), 4))
    assert set(runs(bits)[1:-1]) <= {6, 7} and len(bits) > 500 * N // 2
