"""uart_tx のテストベンチ（divisor = 8）。"""

import cocotb
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge

from tests.models.uart import decode_uart_line
from tests.tb.common import LevelRecorder, start_clock_and_reset, stream_send

DIV = 8


async def setup(dut):
    dut.divisor.value = DIV
    dut.in_valid.value = 0
    dut.in_data.value = 0
    await start_clock_and_reset(dut)


@cocotb.test()
async def emits_8n1_frame(dut):
    await setup(dut)
    rec = LevelRecorder(dut, dut.tx)
    await stream_send(dut, "in", [0xA5])
    await ClockCycles(dut.clk, DIV * 12)
    assert decode_uart_line(rec.levels, DIV) == bytes([0xA5])
    start = rec.levels.index(0)
    assert rec.levels[start:start + DIV] == [0] * DIV
    await ReadOnly()
    assert int(dut.busy.value) == 0 and int(dut.tx.value) == 1


@cocotb.test()
async def back_to_back_frames_have_exact_period(dut):
    # 連続送信のフレーム周期はちょうど 10 ビット。余分な休止があると連続エコーで取りこぼす
    await setup(dut)
    rec = LevelRecorder(dut, dut.tx)
    await stream_send(dut, "in", [0x00, 0x00, 0x00])
    await ClockCycles(dut.clk, DIV * 12)
    lv = rec.levels
    falling = [i for i in range(1, len(lv)) if lv[i - 1] == 1 and lv[i] == 0]
    assert len(falling) == 3, falling
    assert [b - a for a, b in zip(falling, falling[1:])] == [10 * DIV, 10 * DIV]


@cocotb.test()
async def reset_during_a_frame_returns_line_to_idle_high(dut):
    # 送出中（開始ビット = '0' の最中）にリセットされても、リセット期間中と解除後は '1' を保つ
    await setup(dut)
    rec = LevelRecorder(dut, dut.tx)
    dut.in_valid.value = 1
    dut.in_data.value = 0x00
    await RisingEdge(dut.clk)
    dut.in_valid.value = 0
    await ClockCycles(dut.clk, DIV // 2)
    await ReadOnly()
    assert int(dut.tx.value) == 0                # 開始ビットの最中
    await RisingEdge(dut.clk)
    dut.rst.value = 1
    mark = len(rec.levels)
    await ClockCycles(dut.clk, 4)
    dut.rst.value = 0
    await ClockCycles(dut.clk, DIV * 12)
    after_reset = rec.levels[mark + 1:]          # リセットが効いたエッジ以降
    assert after_reset and all(level == 1 for level in after_reset), after_reset
    await ReadOnly()
    assert int(dut.busy.value) == 0
