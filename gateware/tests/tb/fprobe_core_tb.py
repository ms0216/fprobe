"""fprobe_core（ハーネス経由）のテストベンチ。ホストライブラリの符号化と解析で端から端まで確かめる。"""

import random

import cocotb
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge

from fprobe import generated as g
from fprobe.caps import parse
from tests.tb.common import start_clock_and_reset, stream_send
from tests.models.uart import uart_bits
from tests.tb.control_port import ControlPort
from tests.test_caps import TEST_CAPS


async def setup(dut):
    dut.aux_rx.value = 1
    dut.la_pins.value = 0
    dut.la_inputs.value = 0
    dut.uart_in_valid.value = 0
    dut.uart_in_data.value = 0
    port = ControlPort(dut, request="ctrl_in", response="ctrl_out", rng=random.Random(3))
    await start_clock_and_reset(dut)
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
                             (caps.trig_seq, g.TRIGSEQ_CONTROL), (caps.aux, g.AUX_TARGET)):
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
    await port.write(g.LA_FILTER, [0b100 << g.LA_FILTER_CHANNELS[0] | 5 << g.LA_FILTER_CYCLES[0],
                                   1 << g.LA_PULSE_MODE[0] | 1 << g.LA_PULSE_LEVEL[0] | 2 << g.LA_PULSE_CHANNEL[0],
                                   20, 40])
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
        await port.write(g.LA_FILTER, [1 << ch << g.LA_FILTER_CHANNELS[0] | 5 << g.LA_FILTER_CYCLES[0], 0, 0, 0])
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
