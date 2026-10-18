"""text_report（テンプレート "a=<f0> b=<f1>\r\n"）のテストベンチ。"""

import cocotb
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge

from tests.tb.common import StreamCollector, start_clock_and_reset


async def setup(dut, *, ready_prob=1.0):
    dut.start.value = 0
    dut.field0.value = 0
    dut.field1.value = 0
    dut.out_ready.value = 0
    await start_clock_and_reset(dut)


async def pulse_start(dut):
    dut.start.value = 1
    await RisingEdge(dut.clk)
    dut.start.value = 0


@cocotb.test()
async def renders_fields_in_hex(dut):
    await setup(dut)
    out = StreamCollector(dut, "out")
    dut.field0.value = 0x0A
    dut.field1.value = 0xF3
    await pulse_start(dut)
    dut.field0.value = 0x55                    # 開始後の変化は反映されない
    await ClockCycles(dut.clk, 30)
    assert bytes(out.items) == b"a=0A b=F3\r\n"
    await ReadOnly()
    assert int(dut.busy.value) == 0


@cocotb.test()
async def respects_backpressure_and_ignores_start_while_busy(dut):
    await setup(dut)
    out = StreamCollector(dut, "out", always_ready=False)
    dut.field0.value = 0x12
    dut.field1.value = 0x34
    await pulse_start(dut)
    for _ in range(40):                        # ゆっくり読む
        dut.out_ready.value = 1
        await RisingEdge(dut.clk)
        dut.out_ready.value = 0
        await pulse_start(dut)                 # 出力中の start は無視
        await RisingEdge(dut.clk)
    assert bytes(out.items[:11]) == b"a=12 b=34\r\n"
