"""fprobe_core（ハーネス経由）のテストベンチ。ホストライブラリの符号化と解析で端から端まで確かめる。"""

import os
import random

import cocotb
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge

from fprobe import generated as g
from fprobe.caps import parse
from tests.tb.common import start_clock_and_reset, stream_send
from tests.models.uart import uart_bits
from tests.tb.control_port import ControlPort
from tests.test_caps import TEST_CAPS


CAP_RATIO = int(os.environ.get("CORE_CAP_RATIO", "1"))     # 取り込みのクロックの比（1 = sys と同じ。2 以上の構成の試験）


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
    port = ControlPort(dut, request="ctrl_in", response="ctrl_out", rng=random.Random(3))
    await start_clock_and_reset(dut, period_ns=12 if CAP_RATIO > 1 else 10, cap_ratio=CAP_RATIO)
    return port


@cocotb.test()
async def identity_and_scratch(dut):
    port = await setup(dut)
    assert await port.read(g.SYSTEM_MAGIC, 2) == (g.STATUS_OK, 2, [g.SYSTEM_MAGIC_VALUE, g.PROTOCOL_VERSION])
    assert await port.write(g.SYSTEM_SCRATCH, [0x5A5AA5A5]) == (g.STATUS_OK, 1)
    assert (await port.read(g.SYSTEM_SCRATCH, 1))[2] == [0x5A5AA5A5]
    assert await port.write(g.SYSTEM_MAGIC, [0]) == (g.STATUS_OK, 1)       # CONST は書いても変わらない
    assert (await port.read(g.SYSTEM_MAGIC, 1))[2] == [g.SYSTEM_MAGIC_VALUE]
    assert (await port.read(g.SYSTEM_SCRATCH + 4, 1))[0] == g.STATUS_BUS_ERROR   # 窓内の未定義
    assert (await port.read(0x8000, 1))[0] == g.STATUS_BUS_ERROR                  # 未割り当て


async def read_capabilities(port):
    _, _, header = await port.read(g.CAPS_BASE, 2)
    status, _, words = await port.read(g.CAPS_BASE, header[1] // 4)
    assert status == g.STATUS_OK
    return parse(b"".join(w.to_bytes(4, "little") for w in words))


@cocotb.test()
async def capabilities_parse(dut):
    port = await setup(dut)
    caps = await read_capabilities(port)
    assert (caps.board, caps.design, caps.timebase_hz) == (TEST_CAPS.board, TEST_CAPS.design,
                                                           TEST_CAPS.timebase_hz)


@cocotb.test()
async def removed_features_answer_bus_errors(dut):
    # 構成で外した機能の CSR の窓はバスエラー、ある機能の窓は答える（申告と実装の一致）
    port = await setup(dut)
    caps = await read_capabilities(port)
    for present, address in ((caps.sampler, g.SAMPLER_PERIOD), (caps.xtrig, g.XTRIG_ROUTE),
                             (caps.trig_seq, g.TRIGSEQ_CONTROL), (caps.aux, g.AUX_TARGET),
                             (caps.sync, g.SYNC_CONTROL), (caps.sampler_match, g.SAMPLER_MATCH_CONTROL)):
        status = (await port.read(address, 1))[0]
        assert status == (g.STATUS_OK if present else g.STATUS_BUS_ERROR), hex(address)


@cocotb.test()
async def timebase_is_monotonic(dut):
    port = await setup(dut)
    values = []
    for _ in range(5):
        _, _, (lo, hi) = await port.read(g.TIMEBASE_TIME_LO, 2)
        values.append(hi << 32 | lo)
    assert all(b > a for a, b in zip(values, values[1:]))


@cocotb.test()
async def host_command_triggers_the_analyzer_through_the_cross_trigger(dut):
    port = await setup(dut)
    await port.write(g.LA_RATE, [0, 4, 0, 0, 0, 0])        # 毎周期、プリトリガ 4
    await port.write(g.LA_TRIG_MASK, [1 << 15])             # ch15（予約 = 常に 0）が 1 になるまで待つ
    await port.write(g.LA_TRIG_VALUE, [1 << 15])
    await port.write(g.LA_CONTROL, [1])
    for _ in range(3):
        assert (await port.read(g.LA_STATUS, 1))[2][0] >> g.LA_STATUS_TRIGGERED[0] & 1 == 0
    await port.write(g.XTRIG_ROUTE, [0b100 | 0b100 << 8])    # ホストの指令 → ロジアナと割り込み
    await port.write(g.XTRIG_COMMAND, [1])
    assert (await port.read(g.LA_STATUS, 1))[2][0] >> g.LA_STATUS_TRIGGERED[0] & 1 == 1
    _, _, (status,) = await port.read(g.XTRIG_INJECT_STATUS, 1)
    caps = await read_capabilities(port)
    assert status >> g.XTRIG_INJECT_STATUS_RESULT[0] & 3 == (2 if caps.inject else 3)   # SWD 未接続 / 構成に無い


@cocotb.test()
async def pulse_width_trigger_sees_filtered_pins(dut):
    """U21: ピン 2 のグリッチフィルタ（5 周期）を通した 20〜40 周期の High の脈でトリガする。"""
    port = await setup(dut)
    caps = await read_capabilities(port)
    if not (caps.la_filter and caps.la_pulse):
        assert (await port.read(g.LA_FILTER, 4))[0] == g.STATUS_OK        # レジスタはあるが何もしない
        return
    await port.write(g.LA_RATE, [0, 4, 0, 0, 0, 0])                         # 毎周期、プリトリガ 4、パターンなし
    # フィルタとパルス幅の周期は、取り込みの周期（cap の周期）。sys の周期で書いた長さに合わせて CAP_RATIO 倍にする
    await port.write(g.LA_FILTER, [0b100 << g.LA_FILTER_CHANNELS[0] | 5 * CAP_RATIO << g.LA_FILTER_CYCLES[0],
                                   1 << g.LA_PULSE_MODE[0] | 1 << g.LA_PULSE_LEVEL[0] | 2 << g.LA_PULSE_CHANNEL[0],
                                   20 * CAP_RATIO, 40 * CAP_RATIO])
    await port.write(g.LA_CONTROL, [1])

    async def pulse(width):
        await RisingEdge(dut.clk)
        dut.la_pins.value = 0b100
        await ClockCycles(dut.clk, width)
        dut.la_pins.value = 0
        await ClockCycles(dut.clk, 30)

    async def triggered():
        return (await port.read(g.LA_STATUS, 1))[2][0] >> g.LA_STATUS_TRIGGERED[0] & 1

    for width in (4, 10, 60):                  # ひげ（フィルタで消える）、短すぎ、長すぎ
        await pulse(width)
        assert not await triggered(), width
    for _ in range(3):                         # 22 周期の脈がひげで途切れても、フィルタが埋めて 1 本の脈
        await RisingEdge(dut.clk)
        dut.la_pins.value = 0b100
        await ClockCycles(dut.clk, 6)
        dut.la_pins.value = 0
        await ClockCycles(dut.clk, 2)
    dut.la_pins.value = 0
    await ClockCycles(dut.clk, 30)
    assert await triggered()
    await port.write(g.LA_CONTROL, [0])
    await port.write(g.LA_PULSE, [0])


@cocotb.test()
async def auxiliary_samples_reach_the_csr(dut):
    # 補助マイコン（Pico）の計測値の組（1 Mbaud = 100 周期 / ビット）
    port = await setup(dut)
    for b in (0x80, 3300 >> 7, 3300 & 0x7F):
        for level in uart_bits(b):
            dut.aux_rx.value = level
            await ClockCycles(dut.clk, 100)
    status, _, words = await port.read(g.AUX_TARGET, 3)
    assert status == g.STATUS_OK and words == [3300, 1, 0]


class OutputMonitor:
    """xout_oe のビット k が 1 だった周期を数える。"""

    def __init__(self, dut):
        self.dut = dut
        self.high = [0, 0]

    async def run(self):
        while True:
            await RisingEdge(self.dut.clk)
            await ReadOnly()
            oe = int(self.dut.xout_oe.value)
            for k in range(2):
                self.high[k] += oe >> k & 1


def ctrl(*, enable=1, mode=0, level=1, claim=1, length=1, unit=0, pin=0):
    return enable | mode << 4 | level << 8 | claim << 12 | length << 16 | unit << 24 | pin << 28


@cocotb.test()
async def output_follows_a_pin_pattern_through_the_csr(dut):
    port = await setup(dut)
    caps = await read_capabilities(port)
    status = (await port.read(g.XTRIG_OUT_STATUS, 1))[0]
    assert status == (g.STATUS_OK if caps.outputs else g.STATUS_BUS_ERROR)    # 申告と実装の一致
    if not caps.outputs:
        assert int(dut.xout_oe.value) == 0 and int(dut.xout_claim.value) == 0
        return
    # 動作 1（端子の候補 3 = ch7）: ch2 が High の間（条件の間）、High を出す
    await port.write(g.XTRIG_OUT_WHEN1, [0b100 | 0b100 << 8])
    await port.write(g.XTRIG_OUT_CTRL1, [ctrl(mode=2, pin=3)])
    assert int(dut.xout_claim.value) == 0b10 and int(dut.xout_pin.value) == 3 << 2
    assert int(dut.xout_oe.value) == 0
    dut.la_pins.value = 0b100
    await ClockCycles(dut.clk, 6)
    assert int(dut.xout_oe.value) == 0b10 and int(dut.xout_o.value) >> 1 & 1 == 1
    assert (await port.read(g.XTRIG_OUT_STATUS, 1))[2] == [0b10]
    dut.la_pins.value = 0
    await ClockCycles(dut.clk, 6)
    assert int(dut.xout_oe.value) == 0
    # 無効にすると条件が成り立っても動かない。claim は残る
    await port.write(g.XTRIG_OUT_CTRL1, [ctrl(enable=0, mode=2, pin=3)])
    dut.la_pins.value = 0b100
    await ClockCycles(dut.clk, 6)
    assert int(dut.xout_oe.value) == 0 and int(dut.xout_claim.value) == 0b10


@cocotb.test()
async def host_command_pulses_an_output(dut):
    port = await setup(dut)
    caps = await read_capabilities(port)
    if not caps.outputs:
        return
    mon = OutputMonitor(dut)
    cocotb.start_soon(mon.run())
    await port.write(g.XTRIG_OUT_WHEN0, [0b100 << 16])                  # 源 bit2 = ホストの指令
    await port.write(g.XTRIG_OUT_CTRL0, [ctrl(mode=0, length=25, level=0)])
    await port.write(g.XTRIG_COMMAND, [1])
    await ClockCycles(dut.clk, 60)
    assert mon.high == [25, 0]
    assert int(dut.xout_o.value) & 1 == 0                               # level = 0: Low の脈


@cocotb.test()
async def uart_tx_is_driven_only_after_the_first_byte(dut):
    """B29: UART の TX はホストが最初の 1 バイトを送るまで駆動しない（ターゲットの線を勝手に High にしない）。
    1 バイト受け取った後は駆動し続け（休止 = High）、リセットで駆動しないに戻る。"""
    await setup(dut)
    await ClockCycles(dut.clk, 20)
    await ReadOnly()
    assert int(dut.uart_tx_oe.value) == 0
    await RisingEdge(dut.clk)
    await stream_send(dut, "uart_in", [0x55])
    await ClockCycles(dut.clk, 2)
    await ReadOnly()
    assert int(dut.uart_tx_oe.value) == 1
    await ClockCycles(dut.clk, 12000)                                   # 送り終えて休止しても駆動し続ける
    await ReadOnly()
    assert int(dut.uart_tx_oe.value) == 1 and int(dut.uart_tx.value) == 1
    await RisingEdge(dut.clk)
    dut.rst.value = 1
    await ClockCycles(dut.clk, 3)
    dut.rst.value = 0
    await ClockCycles(dut.clk, 2)
    await ReadOnly()
    assert int(dut.uart_tx_oe.value) == 0


@cocotb.test()
async def gpio_io_exchange_drives_and_samples_the_pins(dut):
    """U42: gpio.io は書くと drive、読むと端子（level）。EXCHANGE で 1 要求の中で書いて読むを繰り返せる
    （ビットバングの SPI 等）。端子は drive を 1 周期遅れで返す配線にする（ハーネスの外の線のまね）。"""
    port = await setup(dut)

    async def wire():
        while True:
            await RisingEdge(dut.clk)
            dut.gpio_level.value = int(dut.gpio_drive.value) & int(dut.gpio_enable.value)

    cocotb.start_soon(wire())
    assert await port.write(g.GPIO_ENABLE, [0xFF]) == (g.STATUS_OK, 1)
    states = [0x01, 0x01, 0x82, 0x82, 0x5A, 0x5A, 0xFF, 0xFF]
    status, done, levels = await port.exchange(g.GPIO_IO, states)
    assert (status, done) == (g.STATUS_OK, len(states))
    # 同じ値を 2 回続けて書けば、2 回目の読みには必ずその値が見える（1 回目は前の値のこともある）
    assert levels[1::2] == states[1::2]
    assert all(level in (state, prev) for level, state, prev in zip(levels, states, [0] + states))
    assert (await port.read(g.GPIO_DRIVE, 1))[2] == [0xFF]                 # 書いた値は drive に残る
    assert (await port.read(g.GPIO_LEVEL, 1))[2] == [0xFF]


async def la_triggered(port):
    return (await port.read(g.LA_STATUS, 1))[2][0] >> g.LA_STATUS_TRIGGERED[0] & 1


async def pulse_input(dut, bit, width):
    await RisingEdge(dut.clk)
    dut.la_inputs.value = 1 << bit
    await ClockCycles(dut.clk, width)
    dut.la_inputs.value = 0
    await ClockCycles(dut.clk, 20)


@cocotb.test()
async def dedicated_inputs_are_channels_after_the_internal_probes(dut):
    """U22: 専用の入力 k は申告の並び（LA_LAYOUT）の ch。トリガ・グリッチフィルタ・バッファに拡張ヘッダのピンと
    同じように入る。申告に無い ch は 0 のまま。"""
    port = await setup(dut)
    caps = await read_capabilities(port)
    inputs = [c.channel for c in caps.la_layout if c.kind == "input"]
    assert len(inputs) in (0, len(dut.la_inputs.value))                 # ハーネスは 6 本（構成で 0 本）
    if not inputs:
        await port.write(g.LA_RATE, [0, 4, 0, 0, 0xFC00, 0])            # ch 10〜15 の立ち上がりを待つ（来ない）
        await port.write(g.LA_CONTROL, [1])
        await pulse_input(dut, 2, 30)
        assert not await la_triggered(port)
        await port.write(g.LA_CONTROL, [0])
        return
    ch = inputs[2]
    if caps.la_filter:
        await port.write(g.LA_FILTER, [1 << ch << g.LA_FILTER_CHANNELS[0] | 5 * CAP_RATIO << g.LA_FILTER_CYCLES[0], 0, 0, 0])
    await port.write(g.LA_RATE, [0, 16, 0, 0, 1 << ch, 0])                # 毎周期、プリトリガ 16、ch の立ち上がり
    await port.write(g.LA_CONTROL, [1])
    await ClockCycles(dut.clk, 40)
    assert not await la_triggered(port)
    if caps.la_filter:
        await pulse_input(dut, 2, 3)                                    # ひげ（5 周期より短い）は捨てる
        assert not await la_triggered(port)
    await pulse_input(dut, 2, 30)
    assert await la_triggered(port)
    while not (await port.read(g.LA_STATUS, 1))[2][0] >> g.LA_STATUS_DONE[0] & 1:
        pass
    start, trigger = (await port.read(g.LA_START_INDEX, 2))[2]
    _, _, (before, at) = await port.read(g.LA_MEM_BASE + 4 * ((trigger - 1) % 8192), 2)
    assert (before, at) == (0, 1 << ch)                                  # 他の ch は動いていない
    await port.write(g.LA_CONTROL, [0])
    await port.write(g.LA_FILTER, [0, 1 << g.LA_PULSE_LEVEL[0], 0, 0xFFFFFFFF])


@cocotb.test()
async def external_trigger_input_fires_the_cross_trigger_through_the_sequencer(dut):
    """U22 の外部トリガ入力: 専用の入力の立ち上がり → 多段トリガ（パターン A、1 段）→ クロストリガの源 bit3 →
    ロジアナ。ロジアナ自身の条件（ch15 = 1）は成り立たない設定にしておく。"""
    port = await setup(dut)
    caps = await read_capabilities(port)
    inputs = [c.channel for c in caps.la_layout if c.kind == "input"]
    if not (inputs and caps.trig_seq and caps.xtrig):
        return
    ch = inputs[0]
    await port.write(g.TRIGSEQ_CONTROL, [0])
    await port.write(g.TRIGSEQ_PATTERN_A, [0, 1 << ch, 0, 0])            # A = ch の立ち上がり
    await port.write(g.TRIGSEQ_STAGE0, [1 | 1 << 16, 0, 0, 0, 0, 0, 0])
    await port.write(g.TRIGSEQ_CONTROL, [1 << g.TRIGSEQ_CONTROL_STAGES[0] | 1])
    await port.write(g.XTRIG_ROUTE, [0b1000])                            # 多段トリガ → ロジアナ
    await port.write(g.LA_RATE, [0, 4, 1 << 3, 1 << 3, 0, 0])            # ch3（ピン、0 のまま）= 1 を待つ
    await port.write(g.LA_CONTROL, [1])
    await ClockCycles(dut.clk, 20)
    assert not await la_triggered(port)
    await pulse_input(dut, 0, 10)
    assert await la_triggered(port)
    assert (await port.read(g.TRIGSEQ_STATUS, 1))[2][0] >> g.TRIGSEQ_STATUS_FIRED[0] & 1
    await port.write(g.LA_CONTROL, [0])
    await port.write(g.XTRIG_ROUTE, [0])
    await port.write(g.TRIGSEQ_CONTROL, [0])


@cocotb.test()
async def events_are_recorded_in_the_internal_ring_without_external_memory(dut):
    """外部メモリの無い構成のイベント記録（BRAM のリング）: PC の EventLog と同じ手順（events.control → la_deep の読み出しの窓
    → ストリーム）で読める。ホスト → ターゲットの UART のバイトがイベント（種類 4）になる。"""
    from fprobe.events import decode_events
    port = await setup(dut)
    capacity = (await port.read(g.EVENTS_CAPACITY, 1))[2][0]
    if capacity == 0:
        return                                                          # この構成にはイベント記録が無い
    assert capacity == 64
    got = bytearray()

    async def collect():
        while True:
            await RisingEdge(dut.clk)
            if dut.stream_out_valid.value == 1 and dut.stream_out_ready.value == 1:
                got.append(int(dut.stream_out_data.value))
    cocotb.start_soon(collect())
    await port.write(g.EVENTS_CONTROL, [1])
    await stream_send(dut, "uart_in", [0x41, 0x42, 0x43])
    for _ in range(100):                                               # UART の速さで 1 バイトずつ出ていく
        await ClockCycles(dut.clk, 1000)
        written = (await port.read(g.EVENTS_WRITTEN, 1))[2][0]
        if written == 4:
            break
    await ClockCycles(dut.clk, 5000)                                   # 詰めた形の最後の件（差の範囲を過ぎて出る）
    written = (await port.read(g.EVENTS_WRITTEN, 1))[2][0]
    # 記録を始めた周期の PHY の設定（種類 16。time-accuracy R7）+ バイトの 1〜3 件
    assert 2 <= written <= 4
    await port.write(g.LA_DEEP_READ_REGION, [1])
    await port.write(g.LA_DEEP_READ_FIRST, [0, written])
    await port.write(g.LA_DEEP_COMMAND, [1 << g.LA_DEEP_COMMAND_READ[0]])
    for _ in range(2000):
        if len(got) >= 16 * written:
            break
        await RisingEdge(dut.clk)
    events = decode_events(bytes(got[:16 * written]))
    assert events[0].kind == 16 and events[0].config is not None       # 記録を始めた周期の設定
    events = events[1:]
    assert [e.kind for e in events] == [4, 4, 4]                       # 詰めた形も、解読すれば種類 4 のバイト
    assert [e.data & 0xFF for e in events] == [0x41, 0x42, 0x43]
    assert events[0].time < events[1].time < events[2].time
    # 記録の種類: 詰めた形を申告する構成は 14（UART の送信を詰めた形）、ほかは 4。バイトの間隔（UART の 1 バイト）が
    # 差の 12 ビットより長ければ 1 件 1 バイト、短ければまとまる（どちらでも解読は同じ）
    caps = await read_capabilities(port)
    kinds = {got[16 * i + 15] for i in range(1, written)}
    assert kinds == ({14} if caps.event_byte_pack else {4}), kinds


@cocotb.test()
async def a_sync_frame_on_sync_in_becomes_a_pair_event(dut):
    """子: sync_in で受けた親の時刻が、受けた時刻（子のタイムベース）と組でイベント（種類 10）になる。"""
    from fprobe.events import KIND_SYNC, decode_events
    from tests.tb.sync_link_tb import BIT, frame_bits
    port = await setup(dut)
    caps = await read_capabilities(port)
    if not caps.sync:
        return
    got = bytearray()

    async def collect():
        while True:
            await RisingEdge(dut.clk)
            if dut.stream_out_valid.value == 1 and dut.stream_out_ready.value == 1:
                got.append(int(dut.stream_out_data.value))
    cocotb.start_soon(collect())
    await port.write(g.EVENTS_CONTROL, [1])
    await port.write(g.SYNC_CONTROL, [1 << g.SYNC_CONTROL_ENABLE[0]])
    parent = (0x8765_4321 << 32) | 0x0FED_CBA9
    before = (await port.read(g.TIMEBASE_TIME_LO, 1))[2][0]
    for bit in [1, *frame_bits(parent)]:                                 # パルス → 時刻 → CRC
        dut.sync_in.value = bit
        await ClockCycles(dut.clk, BIT)
    dut.sync_in.value = 0
    await ClockCycles(dut.clk, BIT * 3)
    status = (await port.read(g.SYNC_STATUS, 1))[2][0]
    assert status == 1                                                   # pairs = 1、crc_errors = 0
    await port.write(g.LA_DEEP_READ_REGION, [1])
    await port.write(g.LA_DEEP_READ_FIRST, [0, 2])                       # 記録を始めた周期の設定（種類 16）と組
    await port.write(g.LA_DEEP_COMMAND, [1 << g.LA_DEEP_COMMAND_READ[0]])
    for _ in range(2000):
        if len(got) >= 32:
            break
        await RisingEdge(dut.clk)
    cfg, e = decode_events(bytes(got))
    assert cfg.kind == 16
    assert (e.kind, e.address, e.data) == (KIND_SYNC, parent & 0xFFFF_FFFF, parent >> 32)
    assert 0 < (e.time - before) & 0xFFFF_FFFF < 2000                                    # 受けた時刻（線が 1 になった直後）


@cocotb.test()
async def sampler_match_fires_the_cross_trigger(dut):
    """R21: サンプラが DHCSR を読み続け、S_HALT が立った瞬間（前の標本は 0）に源 bit5 → ロジアナ。CSR sampler_match →
    sampler_match → クロストリガの源の並び → ロジアナの外部トリガ、の配線を端から端まで通す。"""
    from tests.models.swd_target import SwdTarget
    from tests.tb.dap_processor_tb import Dap
    port = await setup(dut)
    caps = await read_capabilities(port)
    if not (caps.sampler and caps.xtrig and caps.sampler_match):
        return
    dhcsr, s_halt = 0xE000EDF0, 1 << 17
    model = SwdTarget(dut)
    model.memory[dhcsr] = 0
    cocotb.start_soon(model.run())
    dap = Dap(dut, ControlPort(dut, request="dap_in", response="dap_out", rng=random.Random(5)))
    await port.write(g.DEBUG_SAMPLE_DELAY, [0])                       # モデルは立ち上がりと同時に SWDIO を変える
    await dap.connect()
    await dap.mem_setup()
    await port.write(g.SAMPLER_SLOT_INDEX, [2, dhcsr, 1 << g.SAMPLER_SLOT_CONFIG_ENABLE[0], 1])   # か所 2 = DHCSR 1 語
    assert (await port.read(g.SAMPLER_MATCH_ADDR_MASK, 1))[2][0] == 0xFFFFFFFF                # 既定は 1 番地（今までどおり）
    await port.write(g.SAMPLER_MATCH_ADDR, [dhcsr, s_halt, s_halt, 1])                         # 番地・mask・value・有効
    await port.write(g.SAMPLER_PERIOD, [2000])
    await port.write(g.SAMPLER_CONTROL, [1])
    await port.write(g.XTRIG_ROUTE, [1 << 5])                                                  # サンプラの一致 → ロジアナ
    await port.write(g.LA_RATE, [0, 4, 1 << 3, 1 << 3, 0, 0])                                  # ロジアナ自身は成り立たない
    await port.write(g.LA_CONTROL, [1])
    for _ in range(50):
        await ClockCycles(dut.clk, 1000)
        if (await port.read(g.SAMPLER_MATCH_SEEN, 1))[2][0] >= 2:
            break
    assert (await port.read(g.SAMPLER_MATCH_SEEN, 1))[2][0] >= 2
    assert not await la_triggered(port)
    model.memory[dhcsr] = s_halt | 0x3                                                        # 止まった
    for _ in range(50):
        await ClockCycles(dut.clk, 1000)
        if await la_triggered(port):
            break
    assert await la_triggered(port)
    _, _, (ctrl, seen, count, t_lo, t_hi, p_lo, p_hi) = await port.read(g.SAMPLER_MATCH_CONTROL, 7)
    assert ctrl >> g.SAMPLER_MATCH_CONTROL_MATCHED[0] & 1 and count == 1 and seen >= 3
    assert (p_hi << 32 | p_lo) < (t_hi << 32 | t_lo)
    fire = (await port.read(g.SAMPLER_MATCH_FIRE, 1))[2][0]
    assert fire >> g.SAMPLER_MATCH_FIRE_INDEX[0] & 0x7F == 2 * 16                              # か所 2 の語 0
    await port.write(g.LA_CONTROL, [0])
    await port.write(g.XTRIG_ROUTE, [0])
    await port.write(g.SAMPLER_CONTROL, [0])


@cocotb.test()
async def injected_halt_is_an_event_with_its_transfer_count(dut):
    """R20: クロストリガの割り込み書き込み（ホストの指令 → 停止）が、イベント記録に種類 11 で、停止の書き込みまでの
    転送数（ホストの CSW・TAR の控えがあるので 4）つきで入る。"""
    from fprobe.events import KIND_INJECT_TRANSFERS, decode_events
    from tests.models.swd_target import SwdTarget
    from tests.tb.dap_processor_tb import AP_TAR_W, Dap
    port = await setup(dut)
    caps = await read_capabilities(port)
    capacity = (await port.read(g.EVENTS_CAPACITY, 1))[2][0]
    if not (caps.xtrig and caps.inject and capacity):
        return
    model = SwdTarget(dut)
    cocotb.start_soon(model.run())
    dap = Dap(dut, ControlPort(dut, request="dap_in", response="dap_out", rng=random.Random(7)))
    await port.write(g.DEBUG_SAMPLE_DELAY, [0])                       # モデルは立ち上がりと同時に SWDIO を変える
    await dap.connect()
    await dap.mem_setup()
    await dap.transfer((AP_TAR_W, 0x20000000))                        # CSW・TAR の控えができる
    got = bytearray()

    async def collect():
        while True:
            await RisingEdge(dut.clk)
            if dut.stream_out_valid.value == 1 and dut.stream_out_ready.value == 1:
                got.append(int(dut.stream_out_data.value))
    cocotb.start_soon(collect())
    await port.write(g.EVENTS_CONTROL, [1])
    await port.write(g.XTRIG_ROUTE, [0b100 << 8])                     # ホストの指令 → 割り込み書き込み
    await port.write(g.XTRIG_COMMAND, [1])
    for _ in range(100):
        await ClockCycles(dut.clk, 500)
        if (await port.read(g.XTRIG_INJECT_STATUS, 1))[2][0] >> g.XTRIG_INJECT_STATUS_COUNT[0]:
            break
    assert model.memory[0xE000EDF0] == 0xA05F0003
    written = (await port.read(g.EVENTS_WRITTEN, 1))[2][0]
    await port.write(g.LA_DEEP_READ_REGION, [1])
    await port.write(g.LA_DEEP_READ_FIRST, [0, written])
    await port.write(g.LA_DEEP_COMMAND, [1 << g.LA_DEEP_COMMAND_READ[0]])
    for _ in range(5000):
        if len(got) >= 16 * written:
            break
        await RisingEdge(dut.clk)
    injected = [e for e in decode_events(bytes(got)) if e.kind == KIND_INJECT_TRANSFERS]
    assert [(e.address, e.data, e.transfers) for e in injected] == [(0xE000EDF0, 0xA05F0003, 4)]
    await port.write(g.XTRIG_ROUTE, [0])


@cocotb.test()
async def injected_word_list_is_written_and_each_word_is_an_event(dut):
    """halt-watch-design §3.2: CSR inject の表で語の列を書く（語 0 は xtrig の inject_addr / inject_value）。書き込みごとに
    イベント 種類 11（その語の番地と値）、inject.status.written_words に受理された数。"""
    from fprobe.events import KIND_INJECT_TRANSFERS, decode_events
    from tests.models.swd_target import SwdTarget
    from tests.tb.dap_processor_tb import AP_TAR_W, Dap
    port = await setup(dut)
    caps = await read_capabilities(port)
    capacity = (await port.read(g.EVENTS_CAPACITY, 1))[2][0]
    if not (caps.xtrig and caps.inject and capacity):
        return
    model = SwdTarget(dut)
    cocotb.start_soon(model.run())
    dap = Dap(dut, ControlPort(dut, request="dap_in", response="dap_out", rng=random.Random(8)))
    await port.write(g.DEBUG_SAMPLE_DELAY, [0])
    await dap.connect()
    await dap.mem_setup()
    await dap.transfer((AP_TAR_W, 0x20000000))
    words = [(0x80010090 + 0x2000 * n, 1) for n in range(3)]            # 3 コアの DBGDRCR に停止要求
    await port.write(g.XTRIG_INJECT_ADDR, [words[0][0], words[0][1]])
    for i, (a, v) in enumerate(words[1:], start=1):
        await port.write(g.INJECT_ENTRY, [i, a, v, 1])                  # 位置・番地・値・書く
    await port.write(g.INJECT_CONTROL, [2])                             # 語 0 の後に 2 語
    got = bytearray()

    async def collect():
        while True:
            await RisingEdge(dut.clk)
            if dut.stream_out_valid.value == 1 and dut.stream_out_ready.value == 1:
                got.append(int(dut.stream_out_data.value))
    cocotb.start_soon(collect())
    await port.write(g.EVENTS_CONTROL, [1])
    await port.write(g.XTRIG_ROUTE, [0b100 << 8])
    await port.write(g.XTRIG_COMMAND, [1])
    for _ in range(100):
        await ClockCycles(dut.clk, 500)
        if (await port.read(g.XTRIG_INJECT_STATUS, 1))[2][0] >> g.XTRIG_INJECT_STATUS_COUNT[0]:
            break
    assert all(model.memory[a] == v for a, v in words)
    status = (await port.read(g.INJECT_STATUS, 1))[2][0]
    assert status >> g.INJECT_STATUS_WRITTEN_WORDS[0] & 0xF == 3
    written = (await port.read(g.EVENTS_WRITTEN, 1))[2][0]
    await port.write(g.LA_DEEP_READ_REGION, [1])
    await port.write(g.LA_DEEP_READ_FIRST, [0, written])
    await port.write(g.LA_DEEP_COMMAND, [1 << g.LA_DEEP_COMMAND_READ[0]])
    for _ in range(5000):
        if len(got) >= 16 * written:
            break
        await RisingEdge(dut.clk)
    injected = [e for e in decode_events(bytes(got)) if e.kind == KIND_INJECT_TRANSFERS]
    assert [(e.address, e.data) for e in injected] == words
    await port.write(g.XTRIG_ROUTE, [0])
    await port.write(g.INJECT_CONTROL, [0])


@cocotb.test()
async def one_core_halting_makes_the_fpga_halt_the_others(dut):
    """halt-watch-design: サンプラが 4 コアの DBGDSCR を読み続け（4 か所 × 1 語）、値の一致（番地のマスクで 4 つをまとめ、
    HALTED が立った瞬間）→ クロストリガの源 bit5 → 割り込み書き込み（4 コアの DBGDRCR に HRQ、語の列）。ホストは何もしない。
    模擬のターゲット: DBGDRCR に HRQ が書かれたら、そのコアの DBGDSCR の HALTED を立てる。"""
    from tests.models.swd_target import SwdTarget
    from tests.tb.dap_processor_tb import AP_TAR_W, Dap
    port = await setup(dut)
    caps = await read_capabilities(port)
    if not (caps.sampler and caps.sampler_match and caps.xtrig and caps.inject):
        return
    dscr = [0x80010088 + 0x2000 * n for n in range(4)]
    drcr = [0x80010090 + 0x2000 * n for n in range(4)]

    drw = []                                                           # (転送の番号, RnW, 番地): DRW のアクセス

    class FourCores(SwdTarget):
        def _ap_read(self, reg):
            if reg == 0x0C:
                drw.append((len(self.transactions), 1, self.tar))
            return super()._ap_read(reg)

        def _ap_write(self, reg, value):
            tar = self.tar                                             # 書いた番地（自動増加の前）
            if reg == 0x0C:
                drw.append((len(self.transactions), 0, tar))
            super()._ap_write(reg, value)
            if reg == 0x0C and tar in drcr and value & 1:
                self.memory[dscr[drcr.index(tar)]] |= 1               # HRQ → 止まる

    model = FourCores(dut)
    for a in dscr:
        model.memory[a] = 0x03000002                                   # 走っている（HALTED = 0）
    cocotb.start_soon(model.run())
    dap = Dap(dut, ControlPort(dut, request="dap_in", response="dap_out", rng=random.Random(9)))
    await port.write(g.DEBUG_SAMPLE_DELAY, [0])
    await dap.connect()
    await dap.mem_setup()
    await dap.transfer((AP_TAR_W, 0x20000000))                        # ホストの CSW・TAR の控え（直接の受け渡しの条件）
    for k, a in enumerate(dscr):
        await port.write(g.SAMPLER_SLOT_INDEX, [k, a, 1 << g.SAMPLER_SLOT_CONFIG_ENABLE[0], 1])
    await port.write(g.SAMPLER_MATCH_ADDR_MASK, [0xFFFFFFFF & ~0x6000])
    await port.write(g.SAMPLER_MATCH_ADDR, [dscr[0], 1, 1, 1 | 1 << g.SAMPLER_MATCH_CONTROL_ONCE[0]])   # 番地・mask・value・有効（1 回だけ）
    await port.write(g.XTRIG_INJECT_ADDR, [drcr[0], 1])
    for i in range(1, 4):
        await port.write(g.INJECT_ENTRY, [i, drcr[i], 1, 1])
    await port.write(g.INJECT_CONTROL, [3])
    await port.write(g.XTRIG_ROUTE, [(1 << 5) << 8])                  # サンプラの値の一致 → 割り込み書き込み
    await port.write(g.SAMPLER_PERIOD, [1])                           # できるだけ速く
    await port.write(g.SAMPLER_CONTROL, [1])
    for _ in range(100):
        await ClockCycles(dut.clk, 1000)
        if (await port.read(g.SAMPLER_MATCH_SEEN, 1))[2][0] >= 8:
            break
    assert (await port.read(g.SAMPLER_MATCH_SEEN, 1))[2][0] >= 8      # 4 コアとも、走っているのを見た
    # 番号ごとの「不一致の標本を見た」（か所 k の語 0 = 番号 16k）: ホストが見逃しを確かめる印。count から 1 回で読める
    _, _, poll = await port.read(g.SAMPLER_MATCH_COUNT, 11)
    assert poll[0] == 0 and poll[7:] == [1 | 1 << 16, 1 | 1 << 16, 0, 0], poll
    model.memory[dscr[2]] |= 1                                         # コア 2 がブレークポイントで止まった
    for _ in range(100):
        await ClockCycles(dut.clk, 500)
        if (await port.read(g.XTRIG_INJECT_STATUS, 1))[2][0] >> g.XTRIG_INJECT_STATUS_COUNT[0]:
            break
    assert all(model.memory[a] & 1 for a in dscr)                     # 全コアが止まった
    await ClockCycles(dut.clk, 30_000)                                 # サンプラは読み続ける（止まった他のコアも見る）
    fire = (await port.read(g.SAMPLER_MATCH_FIRE, 1))[2][0]
    assert fire >> g.SAMPLER_MATCH_FIRE_INDEX[0] & 0x7F == 2 * 16     # 最初に止まったのはコア 2（後から止まったコアで上書きしない）
    assert (await port.read(g.SAMPLER_MATCH_COUNT, 1))[2][0] == 1     # once: 1 回だけ
    assert (await port.read(g.INJECT_STATUS, 1))[2][0] >> g.INJECT_STATUS_WRITTEN_WORDS[0] & 0xF == 4
    _, _, (t_lo, t_hi, p_lo, p_hi) = await port.read(g.SAMPLER_MATCH_TIME_LO, 4)
    _, _, (f_lo, f_hi) = await port.read(g.INJECT_FIRST_TIME_LO, 2)
    _, _, (l_lo, l_hi) = await port.read(g.XTRIG_INJECT_TIME_LO, 2)
    t_fire, t_prev = t_hi << 32 | t_lo, p_hi << 32 | p_lo
    t_first, t_last = f_hi << 32 | f_lo, l_hi << 32 | l_lo
    assert t_prev < t_fire < t_first < t_last
    dut._log.info("halt watch (SWD model): previous sample -> fire %d, fire -> first DBGDRCR write %d, first -> last %d "
                  "(timebase ticks)", t_fire - t_prev, t_first - t_fire, t_last - t_first)
    # 止まったコアの DBGDSCR を読んだ転送から、最初の停止要求（DBGDRCR への DRW の書き込み）までの転送（stop-precision §3.6・§3.7）:
    # 次のか所の TAR・DRW（その応答が止まった値を返し、ACK が誤りの確認）、渡す前に次のか所の値を回収（RDBUFF・CTRL/STAT）、
    # 書き込みの TAR（受け渡しの控えが正しいので SELECT・CSW を書き直さない）。3.2 の後の作り（RDBUFF・CTRL/STAT・SELECT・
    # CSW・TAR）と同じ 5 転送で、悪くならない
    request = next(n for n, rnw, a in drw if not rnw and a == drcr[0])
    halted = max(n for n, rnw, a in drw if rnw and a == dscr[2] and n < request)
    between = [t[:3] for t in model.transactions[halted + 1:request]]
    dut._log.info("halt watch (SWD model): halted DBGDSCR read -> first DBGDRCR write: %d transfers, between: %s",
                  len(between) + 1, between)
    assert between == [(1, 0, 0x4), (1, 1, 0xC), (0, 1, 0xC), (0, 1, 0x4), (1, 0, 0x4)], between
    await port.write(g.XTRIG_ROUTE, [0])
    await port.write(g.SAMPLER_CONTROL, [0])
    await port.write(g.INJECT_CONTROL, [0])


@cocotb.test()
async def over_jtag_the_halt_request_follows_the_halted_sample_without_reading_another_slot(dut):
    """stop-precision-design §3.2: JTAG（JtagDp のモデルにメモリの番地で DBGDSCR・DBGDRCR を持つ 4 コア）で、止まったコアの
    DBGDSCR を読んだ DRW のスキャンから、最初の停止要求（DBGDRCR への DRW の書き込み）のスキャンまでに、サンプラが次のか所の
    DRW を読まない。§3.6（先読み）・§3.7（受け渡しの控え）の目標: 定常の 1 か所 = TAR・DRW・CTRL/STAT の 3 スキャン + IR 2 回
    （直す前は 5 スキャン + IR 2 回）、止まったのを読んでから最初の停止要求まで = 4 スキャン + IR 2 回（直す前は 7 + IR 2）。
    スキャンの数と TCK の数を記録に出す。"""
    from tests.models.jtag_target import Bypass, JtagChain, JtagDp
    from tests.tb.dap_jtag_tb import JTAG_CONFIGURE, JTAG_SEQUENCE, sequence
    from tests.tb.dap_processor_tb import AP_TAR_W, Dap
    port = await setup(dut)
    caps = await read_capabilities(port)
    if not (caps.jtag and caps.sampler and caps.sampler_match and caps.xtrig and caps.inject):
        return
    dscr = [0x80010088 + 0x2000 * n for n in range(4)]
    drcr = [0x80010090 + 0x2000 * n for n in range(4)]
    tck = [0]
    log = []                                              # (TCK の番号, IR, RnW, A, 値, その転送の前の TAR)

    class Chain(JtagChain):
        def _edge(self, tms, tdi):
            tck[0] += 1
            super()._edge(tms, tdi)

    class FourCores(JtagDp):
        def update_dr(self, value):
            tar = self.regs.tar
            before = len(self.scans)
            super().update_dr(value)
            if len(self.scans) == before:
                return                                    # 実行しなかったスキャン（WAIT 等）
            ir, rnw, addr, data = self.scans[-1]
            log.append((tck[0], ir, rnw, addr, data, tar))
            if ir == self.IR_APACC and not rnw and addr == 0xC and tar in drcr and data & 1:
                self.regs.memory[dscr[drcr.index(tar)]] |= 1          # HRQ → 止まる

    dp = FourCores()
    for a in dscr:
        dp.regs.memory[a] = 0x03000002                                 # 走っている（HALTED = 0）
    dut.tdo.value = 1
    cocotb.start_soon(Chain(dut, [dp, Bypass()], tck="swclk", tms="swdio", tdi="tdi", tdo="tdo").run())
    dap = Dap(dut, ControlPort(dut, request="dap_in", response="dap_out", rng=random.Random(10)))
    await port.write(g.DEBUG_SAMPLE_DELAY, [0])
    assert await dap.command(bytes([0x02, 2])) == bytes([0x02, 2])                    # JTAG で接続
    assert await dap.command(bytes([0x11]) + (20_000_000).to_bytes(4, "little")) == bytes([0x11, 0])
    assert await dap.command(bytes([JTAG_CONFIGURE, 2, 4, 5])) == bytes([JTAG_CONFIGURE, 0])
    reset = bytes([JTAG_SEQUENCE, 2]) + sequence(6, tms=1, tdi=0x3F) + sequence(1, tms=0)
    assert await dap.command(reset) == bytes([JTAG_SEQUENCE, 0])
    await dap.mem_setup()
    await dap.transfer((AP_TAR_W, 0x20000000))                        # ホストの CSW・TAR の控え（直接の受け渡しの条件）
    for k, a in enumerate(dscr):
        await port.write(g.SAMPLER_SLOT_INDEX, [k, a, 1 << g.SAMPLER_SLOT_CONFIG_ENABLE[0], 1])
    await port.write(g.SAMPLER_MATCH_ADDR_MASK, [0xFFFFFFFF & ~0x6000])
    await port.write(g.SAMPLER_MATCH_ADDR, [dscr[0], 1, 1, 1 | 1 << g.SAMPLER_MATCH_CONTROL_ONCE[0]])
    await port.write(g.XTRIG_INJECT_ADDR, [drcr[0], 1])
    for i in range(1, 4):
        await port.write(g.INJECT_ENTRY, [i, drcr[i], 1, 1])
    await port.write(g.INJECT_CONTROL, [3])
    await port.write(g.XTRIG_ROUTE, [(1 << 5) << 8])                  # サンプラの値の一致 → 割り込み書き込み
    await port.write(g.SAMPLER_PERIOD, [1])                           # できるだけ速く
    await port.write(g.SAMPLER_CONTROL, [1])
    for _ in range(200):
        await ClockCycles(dut.clk, 1000)
        if (await port.read(g.SAMPLER_MATCH_SEEN, 1))[2][0] >= 8:
            break
    assert (await port.read(g.SAMPLER_MATCH_SEEN, 1))[2][0] >= 8      # 4 コアとも、走っているのを見た
    dp.regs.memory[dscr[2]] |= 1                                       # コア 2 がブレークポイントで止まった
    for _ in range(200):
        await ClockCycles(dut.clk, 500)
        if (await port.read(g.XTRIG_INJECT_STATUS, 1))[2][0] >> g.XTRIG_INJECT_STATUS_COUNT[0]:
            break
    assert all(dp.regs.memory[a] & 1 for a in dscr)                   # 全コアが止まった
    assert (await port.read(g.SAMPLER_MATCH_FIRE, 1))[2][0] >> g.SAMPLER_MATCH_FIRE_INDEX[0] & 0x7F == 2 * 16
    assert (await port.read(g.INJECT_STATUS, 1))[2][0] >> g.INJECT_STATUS_WRITTEN_WORDS[0] & 0xF == 4
    rounds = (await port.read(g.SAMPLER_ROUNDS, 1))[2][0]
    await ClockCycles(dut.clk, 20_000)
    assert (await port.read(g.SAMPLER_ROUNDS, 1))[2][0] > rounds      # 書き込みの後、サンプラは巡回を続ける（止まったまま残らない）
    assert (await port.read(g.SAMPLER_MATCH_COUNT, 1))[2][0] == 1     # once: 1 回だけ
    await port.write(g.XTRIG_ROUTE, [0])
    await port.write(g.SAMPLER_CONTROL, [0])
    await port.write(g.INJECT_CONTROL, [0])

    apacc, dpacc = JtagDp.IR_APACC, JtagDp.IR_DPACC
    # 1 スキャンの TCK: DR = Select-DR・Capture・Shift への 3 + 本体 35 + BYPASS 1 + Exit1・Update の 2、
    # IR = 4 + IR 4 + BYPASS の IR 5 + 2（チェーンは JTAG-DP と BYPASS の 2 装置。idle_cycles = 0）
    dr, ir_scan = 3 + 35 + 1 + 2, 4 + 4 + 5 + 2
    # 最初の停止要求より前の、コア 2 の DBGDSCR の最後の DRW の読み = 止まったのを読んだ DRW（その標本で発火した）
    request = next(i for i, (_, ir, rnw, a, _, tar) in enumerate(log)
                   if ir == apacc and not rnw and a == 0xC and tar == drcr[0])
    halted = max(i for i, (_, ir, rnw, a, _, tar) in enumerate(log[:request])
                 if ir == apacc and rnw and a == 0xC and tar == dscr[2])
    between = log[halted + 1:request]
    slot_reads = [s for s in between if s[1] == apacc and rnw_drw(s)]
    halt_tck = log[request][0] - log[halted][0]
    # 定常の巡回の 1 か所: 続く 2 つのか所の TAR の書き込みの間の TCK（借りた最初の巡回と、止まった後は除く）
    tars = [(t, data) for t, ir, rnw, a, data, _ in log[:halted] if ir == apacc and not rnw and a == 0x4 and data in dscr]
    slot_tck = sorted({b[0] - a[0] for a, b in zip(tars[4:], tars[5:])
                       if dscr.index(b[1]) == (dscr.index(a[1]) + 1) % 4})
    dut._log.info("halt watch (JTAG model): one slot %s TCK; halted DBGDSCR read -> first DBGDRCR write: %d scans, "
                  "%d TCK; between: %s", slot_tck, len(between) + 1, halt_tck,
                  [("AP" if ir == apacc else "DP", "R" if rnw else "W", a) for _, ir, rnw, a, _, _ in between])
    assert slot_reads == [], slot_reads                                # 次のか所を読まない（DRW の読みをしない）
    # stop-precision-design §3.6・§3.7: 前のか所の値の誤りの確認（CTRL/STAT）は、か所ごと。CTRL/STAT の読みのスキャンが
    # DRW の値を受け取り、次のか所の TAR の書き込みのスキャンが CTRL/STAT を受け取る（RDBUFF を読まない）。
    # 書き込みは、受け渡しの控えが正しいので SELECT・CSW を書き直さない
    assert [(ir, rnw, a) for _, ir, rnw, a, _, _ in between] == [
        (dpacc, 1, 0x4), (apacc, 0, 0x4),                              # サンプラ: CTRL/STAT（DRW の値を受け取る）・次のか所の TAR（CTRL/STAT を受け取る）
        (apacc, 0, 0x4)]                                               # 書き込み: TAR
    assert slot_tck == [3 * dr + 2 * ir_scan], slot_tck                # 1 か所 = TAR・DRW・CTRL/STAT + IR の切り替え 2 回
    assert halt_tck == 4 * dr + 2 * ir_scan, halt_tck                  # CTRL/STAT・TAR（IR 2 回）+ 書き込みの TAR・DRW


async def restart_and_watch(dut, *, fpga_restarts: bool):
    """停止の見張りの窓（docs/superpowers/specs/2026-11-03-halt-watch-fpga-restart-design.md §1・§7）。模擬のターゲット: 4 コア、
    全部止まっている（DBGDSCR の HALTED = RESTARTED = 1: 前回の再開から止まったまま）。DBGDRCR に RRQ が書かれたら、コア 0 は
    再開してすぐ止まる（HALTED = RESTARTED = 1。ブレークポイントがすぐ当たった）、コア 1 は 3 回の読みの間デバッグ状態のまま
    （HALTED = 1・RESTARTED = 0）の後に走る、コア 2・3 はすぐ走る（HALTED = 0・RESTARTED = 1）。HRQ が書かれたら止まる。
    fpga_restarts = False: 旧い作り（見張りを始めてから、ホストが再開を書く。値の一致は HALTED の「入った」瞬間）。
    True: 新しい作り（FPGA が始動の列で再開を書き、門の後の標本で HALTED = RESTARTED = 1 なら最初の標本でも発火）。"""
    from tests.models.swd_target import SwdTarget
    from tests.tb.dap_processor_tb import AP_DRW_W, AP_TAR_W, Dap
    port = await setup(dut)
    caps = await read_capabilities(port)
    if not (caps.sampler and caps.sampler_match and caps.xtrig and caps.inject):
        return None
    dscr = [0x80010088 + 0x2000 * n for n in range(4)]
    drcr = [0x80010090 + 0x2000 * n for n in range(4)]
    halted, restarted, hrq, rrq_cse = 1, 2, 1, 0b110
    slow = {"left": 0}

    class FourCores(SwdTarget):
        def _ap_read(self, reg):
            if reg == 0x0C and self.tar == dscr[1] and slow["left"]:
                slow["left"] -= 1
                if not slow["left"]:                                   # デバッグ状態を出た（停止要求が来ていれば、また止まる）
                    self.memory[dscr[1]] = 0x03000000 | restarted | (halted if slow.get("hrq") else 0)
            return super()._ap_read(reg)

        def _ap_write(self, reg, value):
            tar = self.tar
            super()._ap_write(reg, value)
            if reg != 0x0C or tar not in drcr:
                return
            n = drcr.index(tar)
            if value & hrq:
                self.memory[dscr[n]] |= halted
                if n == 1:
                    slow["hrq"] = True
            elif value & 2:                                                # RRQ
                if n == 0:
                    self.memory[dscr[n]] = 0x03000000 | halted | restarted   # 再開してすぐ止まった
                elif n == 1:
                    self.memory[dscr[n]] = 0x03000000 | halted               # まだデバッグ状態（RESTARTED = 0）
                    slow["left"] = 3
                else:
                    self.memory[dscr[n]] = 0x03000000 | restarted

    model = FourCores(dut)
    for a in dscr:
        model.memory[a] = 0x03000000 | halted | restarted
    cocotb.start_soon(model.run())
    dap = Dap(dut, ControlPort(dut, request="dap_in", response="dap_out", rng=random.Random(11)))
    await port.write(g.DEBUG_SAMPLE_DELAY, [0])
    await dap.connect()
    await dap.mem_setup()
    await dap.transfer((AP_TAR_W, 0x20000000))
    for k, a in enumerate(dscr):
        await port.write(g.SAMPLER_SLOT_INDEX, [k, a, 1 << g.SAMPLER_SLOT_CONFIG_ENABLE[0], 1])
    await port.write(g.SAMPLER_MATCH_ADDR_MASK, [0xFFFFFFFF & ~0x6000])
    await port.write(g.XTRIG_INJECT_ADDR, [drcr[0], hrq])
    for i in range(1, 4):
        await port.write(g.INJECT_ENTRY, [i, drcr[i], hrq, 1])
    for i in range(4):
        await port.write(g.INJECT_ENTRY, [4 + i, drcr[i], rrq_cse, 1])     # 始動の列（新しい作りだけが使う）
    await port.write(g.INJECT_CONTROL, [3])
    await port.write(g.XTRIG_ROUTE, [(1 << 5) << 8])
    await port.write(g.SAMPLER_PERIOD, [1])
    once = 1 << g.SAMPLER_MATCH_CONTROL_ONCE[0]
    if fpga_restarts:
        gate = 1 << g.SAMPLER_MATCH_CONTROL_AFTER_START[0] | 1 << g.SAMPLER_MATCH_CONTROL_FIRST_SAMPLE[0]
        await port.write(g.SAMPLER_MATCH_ADDR, [dscr[0], halted | restarted, halted | restarted, 1 | once | gate])
        await port.write(g.SAMPLER_CONTROL, [1])
        await ClockCycles(dut.clk, 5000)                                   # 門の前: 止まったままの標本（比べない）
        assert (await port.read(g.SAMPLER_MATCH_SEEN, 1))[2][0] == 0
        await port.write(g.INJECT_START, [4 | 7 << g.INJECT_START_LAST[0] | 1 << g.INJECT_START_GO[0]])
    else:
        await port.write(g.SAMPLER_MATCH_ADDR, [dscr[0], halted, halted, 1 | once])
        await port.write(g.SAMPLER_CONTROL, [1])
        await ClockCycles(dut.clk, 5000)
        for a in drcr:                                                     # ホストが全コアの再開を書く
            await dap.transfer((AP_TAR_W, a), (AP_DRW_W, rrq_cse))
    for _ in range(100):
        await ClockCycles(dut.clk, 1000)
        if (await port.read(g.XTRIG_INJECT_STATUS, 1))[2][0] >> g.XTRIG_INJECT_STATUS_COUNT[0] >= 1 + fpga_restarts:
            break
    await ClockCycles(dut.clk, 5000)
    _, _, poll = await port.read(g.SAMPLER_MATCH_COUNT, 11)
    state = [model.memory[a] & 3 for a in dscr]
    await port.write(g.XTRIG_ROUTE, [0])
    await port.write(g.SAMPLER_CONTROL, [0])
    await port.write(g.SAMPLER_MATCH_CONTROL, [0])
    return port, poll, state


@cocotb.test()
async def without_the_fpga_restart_a_core_halting_right_after_the_restart_is_missed(dut):
    """旧い作り（RED の記録）: ホストが再開を書くと、FPGA はコア 0 の走っている値を一度も読めず、止まった → 止まったで発火しない
    （count = 0、コア 0 の unmatched = 0）。コア 2・3 は走ったまま（FPGA は止めない）。これが見落としの窓。"""
    r = await restart_and_watch(dut, fpga_restarts=False)
    if r is None:
        return
    _, poll, state = r
    assert poll[0] == 0, poll                                              # 発火しない
    assert not poll[7] & 1                                                 # コア 0（番号 0）の不一致の標本を見ていない
    assert state[2] == 2 and state[3] == 2, state                          # 走ったまま


@cocotb.test()
async def with_the_fpga_restart_a_core_halting_right_after_the_restart_halts_the_others(dut):
    """新しい作り（GREEN）: FPGA が始動の列で全コアに再開を書き、門の後のコア 0 の最初の標本（HALTED = RESTARTED = 1）で発火し、
    全コアに停止要求を書く。fire.first = 1、prev = 始動の列の最初の語の時刻。まだデバッグ状態のコア 1（RESTARTED = 0）では
    発火しない。始動の列の結果は成功・4 語。"""
    r = await restart_and_watch(dut, fpga_restarts=True)
    if r is None:
        return
    port, poll, state = r
    assert poll[0] == 1, poll                                              # 1 回だけ発火
    assert state == [3, 3, 3, 3], state                                    # 全コアが止まった（再開の後に）
    fire = (await port.read(g.SAMPLER_MATCH_FIRE, 1))[2][0]
    assert fire & 0x7F == 0 and fire >> g.SAMPLER_MATCH_FIRE_FIRST[0] & 1  # コア 0、その最初の標本
    _, _, (st, f_lo, f_hi, l_lo, l_hi) = await port.read(g.INJECT_START_STATUS, 5)
    assert not st & 1 and st >> g.INJECT_START_STATUS_RESULT[0] & 3 == 0
    assert st >> g.INJECT_START_STATUS_WRITTEN_WORDS[0] & 0xF == 4 and st >> g.INJECT_START_STATUS_COUNT[0] == 1
    _, _, (t_lo, t_hi, p_lo, p_hi) = await port.read(g.SAMPLER_MATCH_TIME_LO, 4)
    t_start_first, t_start_last = f_hi << 32 | f_lo, l_hi << 32 | l_lo
    t_prev, t_fire = p_hi << 32 | p_lo, t_hi << 32 | t_lo
    assert t_prev == t_start_first & (1 << 48) - 1 and t_start_first < t_start_last < t_fire
    _, _, (h_lo, h_hi) = await port.read(g.INJECT_FIRST_TIME_LO, 2)
    assert h_hi << 32 | h_lo > t_fire                                      # 停止要求の時刻（始動の列で上書きされない）
    assert (await port.read(g.SAMPLER_MATCH_CONTROL, 1))[2][0] >> g.SAMPLER_MATCH_CONTROL_STARTED[0] & 1


def rnw_drw(scan) -> bool:
    """スキャンの記録 (TCK, IR, RnW, A, 値, TAR) が DRW の読み。"""
    return bool(scan[2]) and scan[3] == 0xC


@cocotb.test()
async def the_internal_logic_analyzer_runs_in_the_cap_domain(dut):
    """CAP_RATIO >= 2 の構成: CSR から捕捉し、cap の周期ごとに 1 サンプル（欠けなし）、トリガの位相が CSR で読める。
    比 1 の構成では、取り込みの情報の窓が無い（読むとエラー）。"""
    port = await setup(dut)
    caps = await read_capabilities(port)
    status, _, _ = await port.read(g.LA_CAP_INFO, 1)
    if CAP_RATIO == 1:
        assert status != g.STATUS_OK                                      # 窓が無い構成
        return
    if not caps.la_depth:
        return
    status, _, (info,) = await port.read(g.LA_CAP_INFO, 1)
    assert status == g.STATUS_OK and info >> g.LA_CAP_INFO_RATIO[0] & 0xF == CAP_RATIO

    count = [0]

    async def pins():
        while True:
            await RisingEdge(dut.cap_clk)
            count[0] += 1
            dut.la_pins.value = count[0] & 0xFF
    cocotb.start_soon(pins())
    await ClockCycles(dut.clk, 20)
    await port.write(g.LA_RATE, [0, 4, 0xFF, 0x80, 0, 0])                 # 毎サンプル、プリトリガ 4、ピン 8 本が 0x80 でトリガ
    await port.write(g.LA_CONTROL, [1])
    for _ in range(200):
        await ClockCycles(dut.clk, 20)
        if (await port.read(g.LA_STATUS, 1))[2][0] >> g.LA_STATUS_DONE[0] & 1:
            break
    else:
        raise AssertionError("捕捉が終わらない")
    _, _, (start, trig, t_lo, t_hi) = await port.read(g.LA_START_INDEX, 4)
    depth = caps.la_depth
    samples = []
    for i in range(depth):
        _, _, (w,) = await port.read(g.LA_MEM_BASE + 4 * ((start + i) % depth), 1)
        samples.append(w & 0xFF)
    steps = {(b - a) & 0xFF for a, b in zip(samples, samples[1:])}
    assert steps == {1}, steps                                            # cap の周期ごと、欠けなし
    assert samples[(trig - start) % depth] == 0x80 and (trig - start) % depth == 4
    _, _, (info,) = await port.read(g.LA_CAP_INFO, 1)
    phase = info >> g.LA_CAP_INFO_TRIGGER_PHASE[0] & 0xF
    assert 0 <= phase < CAP_RATIO and (t_hi << 32 | t_lo) > 0
    assert info >> g.LA_CAP_INFO_LATENCY[0] & 0xFF == 5 - CAP_RATIO       # 固定の遅れ（la_cap_domain の試験が測った値）
    await port.write(g.LA_CONTROL, [0])


@cocotb.test()
async def the_cap_selftest_signal_toggles_every_sys_cycle(dut):
    """CAP_RATIO >= 2 の構成: selftest = 1 のとき ch LA_PIN_COUNT + 1（= 9）は sys の周期ごとに反転する。
    取り込みの周期ごとに取るので、同じ値が CAP_RATIO 個ずつ続く（配線なしで取り込みの周波数の比を確かめる）。0 のときは変わらない。"""
    port = await setup(dut)
    caps = await read_capabilities(port)
    if CAP_RATIO == 1 or not caps.la_depth:
        return
    depth = caps.la_depth

    async def grab():
        await port.write(g.LA_RATE, [0, 4, 0, 0, 0, 0])                    # 毎サンプル、プリトリガ 4、条件なし（すぐトリガ）
        await port.write(g.LA_CONTROL, [1])
        for _ in range(400):
            await ClockCycles(dut.clk, 20)
            if (await port.read(g.LA_STATUS, 1))[2][0] >> g.LA_STATUS_DONE[0] & 1:
                break
        else:
            raise AssertionError("捕捉が終わらない")
        _, _, (start, _, _, _) = await port.read(g.LA_START_INDEX, 4)
        bits = []
        for i in range(depth):
            _, _, (w,) = await port.read(g.LA_MEM_BASE + 4 * ((start + i) % depth), 1)
            bits.append(w >> 9 & 1)
        await port.write(g.LA_CONTROL, [0])
        return bits

    assert len(set(await grab())) == 1                                      # 既定は dap_busy（動いていないので一定）
    await port.write(g.LA_CAP_CONTROL, [1])
    bits = await grab()
    runs, n = [], 1
    for a, b in zip(bits, bits[1:]):
        if a == b:
            n += 1
        else:
            runs.append(n)
            n = 1
    assert len(runs) > 8 and set(runs[1:]) == {CAP_RATIO}, (CAP_RATIO, runs)    # 最初の 1 つは途中から
    await port.write(g.LA_CAP_CONTROL, [0])


@cocotb.test()
async def the_square_wave_generator_follows_the_capability(dut):
    """試験用の方形波 la_testgen: ケイパビリティがある構成だけ、窓（CSR la_testgen）が読めてピンに方形波が出る。
    刻むクロックは CAP_RATIO > 1 なら cap_clk、そうでなければ sys（clk）。無い構成は窓が無く（読むとエラー）、ピンは常に駆動しない。"""
    from tests.tb.la_testgen_tb import check_pattern
    port = await setup(dut)
    caps = await read_capabilities(port)
    status, _, _ = await port.read(g.LA_TESTGEN_PERIOD, 1)
    await ReadOnly()
    if not caps.la_testgen:
        assert status != g.STATUS_OK and dut.testgen_oe.value == 0 and dut.testgen_out.value == 0
        await RisingEdge(dut.clk)
        assert (await port.write(g.LA_TESTGEN_CONTROL, [1]))[0] != g.STATUS_OK     # 書いても効かない
        await ClockCycles(dut.clk, 40)
        await ReadOnly()
        assert dut.testgen_oe.value == 0
        return
    assert status == g.STATUS_OK
    tick = dut.cap_clk if CAP_RATIO > 1 else dut.clk

    async def sample(n):
        bits = []
        for _ in range(n):
            await RisingEdge(tick)
            await ReadOnly()
            bits.append(int(dut.testgen_out.value))
        return bits

    await RisingEdge(dut.clk)
    assert (await port.write(g.LA_TESTGEN_PERIOD, [7]))[0] == g.STATUS_OK
    assert (await port.write(g.LA_TESTGEN_HIGH, [3]))[0] == g.STATUS_OK
    assert (await port.write(g.LA_TESTGEN_CONTROL, [1]))[0] == g.STATUS_OK
    await ClockCycles(dut.clk, 60)
    await ReadOnly()
    assert dut.testgen_oe.value == 1
    check_pattern(await sample(40), 7, 3)
    await RisingEdge(dut.clk)
    assert (await port.write(g.LA_TESTGEN_CONTROL, [0]))[0] == g.STATUS_OK
    await ClockCycles(dut.clk, 60)
    await ReadOnly()
    assert dut.testgen_oe.value == 0 and dut.testgen_out.value == 0
