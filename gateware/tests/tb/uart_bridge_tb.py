"""uart_bridge（CLK_HZ = 10 MHz、既定 1 Mbaud = 分周比 10）のテストベンチ。FLOW = true の構成（RTS/CTS の回路あり）でも
同じモジュールを走らせ、dut.flow_g の有無で見分ける。"""

import cocotb
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge

from tests.models.uart import decode_uart_line, uart_bits
from tests.tb.common import LevelRecorder, StreamCollector, start_clock_and_reset, stream_send


async def setup(dut):
    dut.baud.value = 0
    dut.baud_load.value = 0
    dut.in_valid.value = 0
    dut.in_data.value = 0
    dut.rx.value = 1
    dut.brk.value = 0
    dut.flow_auto.value = 0
    dut.rts_host.value = 0
    dut.cts_n.value = 0
    await start_clock_and_reset(dut)


async def set_baud(dut, baud):
    dut.baud.value = baud
    dut.baud_load.value = 1
    await RisingEdge(dut.clk)
    dut.baud_load.value = 0
    await ClockCycles(dut.clk, 40)                         # 割り算（約 35 周期）


async def drive_rx(dut, bits, divisor):
    for bit in bits:
        dut.rx.value = bit
        await ClockCycles(dut.clk, divisor)


@cocotb.test()
async def default_baud_and_change(dut):
    await setup(dut)
    line = LevelRecorder(dut, dut.tx)
    await stream_send(dut, "in", b"AB")
    await ClockCycles(dut.clk, 10 * 10 * 2 + 20)
    assert decode_uart_line(line.levels, 10) == b"AB"
    assert int(dut.current_baud.value) == 1_000_000
    await set_baud(dut, 500_000)                           # 分周比 20
    assert int(dut.current_baud.value) == 500_000
    line.levels.clear()
    await stream_send(dut, "in", b"xyz")
    await ClockCycles(dut.clk, 20 * 10 * 3 + 40)
    assert decode_uart_line(line.levels, 20) == b"xyz"
    await set_baud(dut, 0)                                 # 0 は無視
    assert int(dut.current_baud.value) == 500_000


@cocotb.test()
async def receives_and_counts_errors(dut):
    await setup(dut)
    out = StreamCollector(dut, "out")
    for byte in b"hi!":
        await drive_rx(dut, uart_bits(byte), 10)
    await ClockCycles(dut.clk, 20)
    assert bytes(out.items) == b"hi!"
    await drive_rx(dut, [0] + [1] * 8 + [0], 10)          # 停止ビットが 0
    await drive_rx(dut, [1] * 20, 10)
    await ReadOnly()
    assert int(dut.frame_errors.value) == 1
    await RisingEdge(dut.clk)
    dut.out_ready.value = 0                                # 取り出さないうちに 2 バイト
    for byte in b"pq":
        await drive_rx(dut, uart_bits(byte), 10)
    await ReadOnly()
    assert int(dut.overruns.value) == 1


@cocotb.test()
async def a_change_during_the_division_is_not_lost(dut):
    # 割り算中に来たボーレートは、その後で計算し直す（最後の値が残る。以前は捨てていた）
    await setup(dut)
    for baud in (500_000, 250_000):
        dut.baud.value = baud
        dut.baud_load.value = 1
        await RisingEdge(dut.clk)
        dut.baud_load.value = 0
        await ClockCycles(dut.clk, 5)
    await ClockCycles(dut.clk, 100)
    assert int(dut.current_baud.value) == 250_000


def has_flow(dut) -> bool:
    try:
        dut.flow_g
    except AttributeError:
        return False
    return True


@cocotb.test()
async def break_holds_tx_low_and_pauses_sending(dut):
    await setup(dut)
    line = LevelRecorder(dut, dut.tx)
    dut.brk.value = 1
    await ClockCycles(dut.clk, 4)
    dut.in_valid.value = 1
    dut.in_data.value = 0x41
    await ClockCycles(dut.clk, 200)
    await ReadOnly()
    assert int(dut.in_ready.value) == 0                     # ブレークの間は受け取らない
    assert set(line.levels[-150:]) == {0}
    await RisingEdge(dut.clk)
    line.levels.clear()
    dut.brk.value = 0
    await ReadOnly()
    while int(dut.in_ready.value) == 0:
        await RisingEdge(dut.clk)
        await ReadOnly()
    await RisingEdge(dut.clk)
    dut.in_valid.value = 0
    await ClockCycles(dut.clk, 150)
    assert decode_uart_line(line.levels, 10) == b"A"


@cocotb.test()
async def rts_and_cts(dut):
    await setup(dut)
    await ClockCycles(dut.clk, 4)
    await ReadOnly()
    if not has_flow(dut):
        assert int(dut.rts_n.value) == 1 and int(dut.cts.value) == 1   # 回路が無い: RTS は出さず、CTS は出ているとみなす
        return
    await RisingEdge(dut.clk)
    # 手動: RTS はホストの要求どおり、CTS は見ない
    dut.rts_host.value = 1
    dut.cts_n.value = 1
    await ClockCycles(dut.clk, 4)
    await ReadOnly()
    assert int(dut.rts_n.value) == 0 and int(dut.cts.value) == 0
    await RisingEdge(dut.clk)
    line = LevelRecorder(dut, dut.tx)
    await stream_send(dut, "in", b"m")
    await ClockCycles(dut.clk, 120)
    assert decode_uart_line(line.levels, 10) == b"m"
    # 自動: CTS が出るまで送り始めない
    dut.flow_auto.value = 1
    line.levels.clear()
    dut.in_valid.value = 1
    dut.in_data.value = 0x5A
    await ClockCycles(dut.clk, 200)
    await ReadOnly()
    assert int(dut.in_ready.value) == 0 and set(line.levels) == {1}
    await RisingEdge(dut.clk)
    dut.cts_n.value = 0
    await ReadOnly()
    while int(dut.in_ready.value) == 0:
        await RisingEdge(dut.clk)
        await ReadOnly()
    await RisingEdge(dut.clk)
    dut.in_valid.value = 0
    await ClockCycles(dut.clk, 150)
    assert decode_uart_line(line.levels, 10) == b"Z"
    # 自動: 受信の下流が受けられない間は RTS を下げる（ホストの要求に依らない）
    dut.rts_host.value = 0
    dut.out_ready.value = 1
    await ClockCycles(dut.clk, 3)
    await ReadOnly()
    assert int(dut.rts_n.value) == 0
    await RisingEdge(dut.clk)
    dut.out_ready.value = 0
    await ClockCycles(dut.clk, 2)
    await ReadOnly()
    assert int(dut.rts_n.value) == 1
