"""1 周期に複数のサンプルの構成（LA_LANES = N、fprobe_pynq_fast と同じつなぎ方）の fprobe_core（tests/rtl/fprobe_core_lanes_harness.vhd）。
CSR とケイパビリティ越しに、ホストの解析（fprobe.caps・fprobe.la の並べ方）と同じ読み方で確かめる。
N（CORE_LANES）と、ピンも ISERDESE2 で取るか（CORE_PIN_LANES。fprobe_pynq_fast の段 2 = N 8・ピンも）は試験のラッパが渡す。
仕様 docs/superpowers/specs/2026-11-01-la-resolution-design.md §3.5・§3.6・§4.1・§11"""

import os
import random

import cocotb
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge, Timer
from cocotb.utils import get_sim_time

from fprobe import generated as g
from fprobe.caps import parse
from tests.tb.common import start_clock_and_reset
from tests.tb.control_port import ControlPort

N = int(os.environ["CORE_LANES"])
PIN_LANES = os.environ["CORE_PIN_LANES"] == "1"
# 直並列変換の最も新しいレーンの遅れ（fprobe_gw.designs の pynq_z2_iserdes_newest_age。この試験の包みの値）
IN_AGE = N - 3
CAP_RATIO = N // 2
SYS_PS = 12000 if N == 6 else 16000              # cap_clk の周期（SYS_PS / CAP_RATIO）と細かい辺の周期（SYS_PS / N）が整数の ps
FINE_PS = SYS_PS // N
IN0, PIN0 = 10, 0                                # 専用の入力 0・拡張ヘッダのピン 0 の ch


async def setup(dut):
    dut.aux_rx.value = 1
    dut.sync_in.value = 0
    dut.la_pins.value = 0
    dut.la_inputs.value = 0
    dut.uart_in_valid.value = 0
    dut.uart_in_data.value = 0
    dut.stream_out_ready.value = 1
    dut.dap_in_valid.value = 0
    dut.dap_out_ready.value = 1
    dut.target_swdio.value = "Z"
    port = ControlPort(dut, request="ctrl_in", response="ctrl_out", rng=random.Random(5))
    await start_clock_and_reset(dut, period_ns=SYS_PS // 1000, cap_ratio=CAP_RATIO)
    return port


async def read_capabilities(port):
    _, _, header = await port.read(g.CAPS_BASE, 2)
    status, _, words = await port.read(g.CAPS_BASE, header[1] // 4)
    assert status == g.STATUS_OK
    return parse(b"".join(w.to_bytes(4, "little") for w in words))


async def read_word(port, address):
    status, _, words = await port.read(address, 1)
    assert status == g.STATUS_OK
    return words[0]


async def capture(dut, port, *, step_slot, pretrigger_words=4, channel=IN0, both=False):
    """ch channel（専用の入力 0 かピン 0）の立ち上がりで捕捉する。ピンは、捕捉を始めてから、clk の立ち上がりの細かい辺から
    step_slot 辺目の後に 1 にする（both なら専用の入力 0 とピン 0 を同時に）。その辺のシミュレーション時刻を返す。"""
    dut.la_inputs.value = 0
    dut.la_pins.value = 0
    await port.write(g.LA_CONTROL, [0])
    await port.write(g.LA_RATE, [0, pretrigger_words, 0, 0, 1 << channel, 0])
    await port.write(g.LA_CONTROL, [1])
    await ClockCycles(dut.clk, pretrigger_words + 20)
    await RisingEdge(dut.clk)
    edge_ps = round(get_sim_time("ps")) + step_slot * FINE_PS
    await Timer(step_slot * FINE_PS + FINE_PS // 2, unit="ps")        # 辺 step_slot と次の辺の間で変える
    if channel == IN0 or both:
        dut.la_inputs.value = 1
    if channel == PIN0 or both:
        dut.la_pins.value = 1
    for _ in range(200):
        status = await read_word(port, g.LA_STATUS)
        if status >> g.LA_STATUS_DONE[0] & 1:
            return edge_ps
    raise AssertionError("捕捉が終わらない")


async def read_lanes(port, depth_words):
    """la_lanes_mem から start_index の時間順に、サンプル（16 ch）の列（fprobe.la と同じ並べ方）。"""
    start = await read_word(port, g.LA_START_INDEX)
    slices = []
    for j in range(N // 2):
        status, _, words = await port.read(g.LA_LANES_MEM_BASE + 4 * j * depth_words, depth_words)
        assert status == g.STATUS_OK
        slices.append(words)
    samples = []
    for w in range(depth_words):
        word = (start + w) % depth_words
        for j in range(N // 2):
            samples += [slices[j][word] & 0xFFFF, slices[j][word] >> 16]
    return samples, start


@cocotb.test()
async def capabilities_and_info_declare_the_lanes(dut):
    port = await setup(dut)
    caps = await read_capabilities(port)
    assert caps.la_sample_hz == caps.sys_clock_hz * N and not caps.la_filter and not caps.la_pulse
    assert caps.la_fine == (0xFCFF if PIN_LANES else 0xFC00)   # 専用の入力（ch 10〜15）と、PIN_LANES ならピン（ch 0〜7）が細かい
    info = await read_word(port, g.LA_CAP_INFO)
    assert info >> g.LA_CAP_INFO_RATIO[0] & 0xF == N
    assert info >> g.LA_CAP_INFO_LANES[0] & 0xF == N
    assert info >> g.LA_CAP_INFO_LATENCY[0] & 0xFF == N * 4 + (N - 3)
    assert (await port.read(g.LA_MEM_BASE, 1))[0] == g.STATUS_BUS_ERROR   # 1 サンプル 1 ワードの窓は無い


async def trigger_n(port):
    info = await read_word(port, g.LA_CAP_INFO)
    lo, hi = (await port.read(g.LA_TRIGGER_TIME_LO, 2))[2]
    return ((hi << 32 | lo) * N + (info >> g.LA_CAP_INFO_TRIGGER_PHASE[0] & 0xF)
            - (info >> g.LA_CAP_INFO_LATENCY[0] & 0xFF))


async def fine_edges_are_timed_per_lane(dut, channel):
    """ピンを変える辺を 1 つずつずらすと、n = trigger_time × N + trigger_lane − latency も 1 つずつ動き、記録の位置もそこ。"""
    port = await setup(dut)
    caps = await read_capabilities(port)
    depth_words = caps.la_depth // N
    first = None
    for d in range(N + 2):
        edge_ps = await capture(dut, port, step_slot=d, channel=channel)
        info = await read_word(port, g.LA_CAP_INFO)
        lo, hi = (await port.read(g.LA_TRIGGER_TIME_LO, 2))[2]
        n = ((hi << 32 | lo) * N + (info >> g.LA_CAP_INFO_TRIGGER_PHASE[0] & 0xF)
             - (info >> g.LA_CAP_INFO_LATENCY[0] & 0xFF))
        if first is None:
            first = (edge_ps, n)
        assert n - first[1] == (edge_ps - first[0]) // FINE_PS, f"d={d}"
        samples, start = await read_lanes(port, depth_words)
        trigger = await read_word(port, g.LA_TRIGGER_INDEX)
        pos = (trigger - start) % depth_words * N + (info >> g.LA_CAP_INFO_TRIGGER_PHASE[0] & 0xF)
        assert samples[pos] >> channel & 1 and not samples[pos - 1] >> channel & 1
        assert pos >= 4 * N                               # プリトリガ 4 語
    await port.write(g.LA_CONTROL, [0])


@cocotb.test()
async def fine_input_edges_are_timed_per_lane(dut):
    await fine_edges_are_timed_per_lane(dut, IN0)


@cocotb.test(skip=not PIN_LANES)
async def fine_pin_edges_are_timed_per_lane(dut):
    """ピンも ISERDESE2 で取る構成: 拡張ヘッダのピン 0（ch 0）でも同じ（細かいサンプルの格子）。"""
    await fine_edges_are_timed_per_lane(dut, PIN0)


@cocotb.test(skip=not PIN_LANES)
async def pins_and_inputs_share_one_time_axis(dut):
    """同じ細かい辺の後に変えたピン 0 と専用の入力 0 は、記録の同じサンプルで変わり、トリガの n も同じ（時刻の表
    fprobe_gw/timing.py の「ピンのずれ 0」の根拠。端子から ISERDESE2 までの配線の差は含まない）。"""
    port = await setup(dut)
    caps = await read_capabilities(port)
    depth_words = caps.la_depth // N
    for d in (0, 3):
        edge_in = await capture(dut, port, step_slot=d, channel=IN0, both=True)
        n_in = await trigger_n(port)
        samples, _ = await read_lanes(port, depth_words)
        rise = {ch: next(i for i in range(1, len(samples)) if samples[i] >> ch & 1 and not samples[i - 1] >> ch & 1)
                for ch in (IN0, PIN0)}
        assert rise[IN0] == rise[PIN0], (d, rise)
        edge_pin = await capture(dut, port, step_slot=d, channel=PIN0, both=True)    # 別の捕捉: 辺の時刻からの位置で比べる
        assert await trigger_n(port) - edge_pin // FINE_PS == n_in - edge_in // FINE_PS, d
    await port.write(g.LA_CONTROL, [0])


@cocotb.test(skip=PIN_LANES)
async def sys_channels_repeat_in_every_lane(dut):
    """拡張ヘッダのピン（sys で取る ch 0〜7。ピンを ISERDESE2 で取らない構成）は sys の周期ごとの値（LA_FINE に無い ch）。
    la_lane_inputs がレーンごとに細かい辺の前の sys の周期の値を入れるので（時刻の正しさ S1b の N2）、値はレーン IN_AGE mod N で
    切り替わり、N サンプルごとに 1 ずつ増える（欠けなし）。"""
    port = await setup(dut)
    caps = await read_capabilities(port)
    depth_words = caps.la_depth // N

    async def toggle_pins():
        value = 0
        while True:
            await RisingEdge(dut.clk)
            value = (value + 1) & 0xFF
            dut.la_pins.value = value

    task = cocotb.start_soon(toggle_pins())
    await capture(dut, port, step_slot=1)
    task.cancel()
    samples, _ = await read_lanes(port, depth_words)
    shift = IN_AGE % N
    runs = [samples[shift + w * N:shift + (w + 1) * N] for w in range(depth_words - 1)]
    assert all(len({s & 0xFF for s in run}) == 1 for run in runs)
    values = [run[0] & 0xFF for run in runs]
    assert all((b - a) & 0xFF == 1 for a, b in zip(values, values[1:]))   # sys の周期ごとに 1 ずつ（欠けなし）
    await port.write(g.LA_CONTROL, [0])


@cocotb.test()
async def internal_channels_repeat_in_every_lane(dut):
    """内部の ch（ch 9。selftest = 1 で sys の周期ごとに反転）は sys の信号（LA_FINE に入らない）。la_lane_inputs が細かい ch の
    時間軸に揃えるので（時刻の正しさ S1b の N2: sys の辺 T = 細かい辺 N × T の直後のサンプルから新しい値）、値はレーン IN_AGE mod N で
    切り替わり、N サンプルごとに反転する。"""
    port = await setup(dut)
    caps = await read_capabilities(port)
    depth_words = caps.la_depth // N
    await port.write(g.LA_CAP_CONTROL, [1])
    await capture(dut, port, step_slot=1)
    samples, _ = await read_lanes(port, depth_words)
    shift = IN_AGE % N
    runs = [[x >> 9 & 1 for x in samples[shift + w * N:shift + (w + 1) * N]] for w in range(depth_words - 1)]
    assert all(len(set(run)) == 1 for run in runs)
    assert all(a[0] != b[0] for a, b in zip(runs, runs[1:]))
    await port.write(g.LA_CAP_CONTROL, [0])
    await port.write(g.LA_CONTROL, [0])
