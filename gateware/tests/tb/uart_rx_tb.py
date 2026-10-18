"""uart_rx のテストベンチ（divisor = 8）。"""

import cocotb
from cocotb.triggers import ClockCycles, ReadOnly

from tests.models.uart import uart_bits
from tests.tb.common import PulseCounter, StreamCollector, start_clock_and_reset

DIV = 8


async def setup(dut, *, ready=True):
    dut.divisor.value = DIV
    dut.rx.value = 1
    dut.out_ready.value = 1 if ready else 0
    await start_clock_and_reset(dut)
    await ClockCycles(dut.clk, 4)


async def drive(dut, levels):
    for level in levels:
        dut.rx.value = level
        await ClockCycles(dut.clk, DIV)


@cocotb.test()
async def receives_bytes_back_to_back(dut):
    await setup(dut)
    got = StreamCollector(dut, "out")
    for byte in (0x00, 0xFF, 0x3C):
        await drive(dut, uart_bits(byte))
    await ClockCycles(dut.clk, DIV * 2)
    assert got.items == [0x00, 0xFF, 0x3C]


@cocotb.test()
async def ignores_glitch_and_recovers_from_frame_error(dut):
    await setup(dut)
    got = StreamCollector(dut, "out")
    errors = PulseCounter(dut, dut.frame_error)
    # 半ビット未満のグリッチは開始ビットとみなさない
    dut.rx.value = 0
    await ClockCycles(dut.clk, DIV // 2 - 2)
    dut.rx.value = 1
    await ClockCycles(dut.clk, DIV * 12)
    # 停止ビットが 0 のフレーム → frame_error。線が low のまま続いても次へ進まない
    await drive(dut, uart_bits(0x55)[:-1] + [0, 0, 0])
    dut.rx.value = 1
    await ClockCycles(dut.clk, DIV * 3)
    # その後の正常なバイトは受信できる
    await drive(dut, uart_bits(0x81))
    await ClockCycles(dut.clk, DIV * 2)
    assert errors.count == 1
    assert got.items == [0x81]


@cocotb.test()
async def reports_overrun_and_keeps_latest(dut):
    await setup(dut, ready=False)
    overruns = PulseCounter(dut, dut.overrun)
    for byte in (0x11, 0x22):
        await drive(dut, uart_bits(byte))
    await ClockCycles(dut.clk, DIV)
    await ReadOnly()
    assert int(dut.out_valid.value) == 1
    assert int(dut.out_data.value) == 0x22
    assert overruns.count == 1
