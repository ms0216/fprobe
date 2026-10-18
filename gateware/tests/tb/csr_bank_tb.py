"""生成したレジスタバンク（tests/test_csr_generator.py の TEST_BLOCK）のテストベンチ。"""

import cocotb
from cocotb.triggers import ReadOnly, RisingEdge

from tests.tb.common import PulseCounter, start_clock_and_reset
from tests.tb.csr_decoder_tb import CsrMaster

BASE = 0x200 // 4


async def setup(dut):
    dut.status_busy.value = 0
    dut.status_count.value = 0
    dut.snap.value = 0
    dut.pins.value = 0
    await start_clock_and_reset(dut)
    return CsrMaster(dut)


@cocotb.test()
async def reset_values_consts_and_fields(dut):
    bus = await setup(dut)
    await ReadOnly()
    assert int(dut.ctrl_enable.value) == 1 and int(dut.ctrl_mode.value) == 5
    assert int(dut.word.value) == 0xDEADBEEF
    await RisingEdge(dut.clk)
    data, err, cycles = await bus.access(BASE + 0)
    assert (data, err) == (0xA5 << 16 | 5 << 4 | 1, False) and cycles == 1
    await bus.access(BASE + 0, write=0xFFFF_FF72)            # enable=0, mode=7、ident は書けない
    assert (await bus.access(BASE + 0))[0] == 0xA5 << 16 | 7 << 4
    await ReadOnly()
    assert int(dut.ctrl_enable.value) == 0 and int(dut.ctrl_mode.value) == 7


@cocotb.test()
async def read_only_inputs_and_rw_word(dut):
    bus = await setup(dut)
    dut.status_busy.value = 1
    dut.status_count.value = 0xBEEF
    assert (await bus.access(BASE + 1))[0] == 0xBEEF << 8 | 1
    await bus.access(BASE + 1, write=0)                      # RO への書き込みは無視（エラーなし）
    assert (await bus.access(BASE + 1))[0] == 0xBEEF << 8 | 1
    await bus.access(BASE + 3, write=0x12345678)
    assert (await bus.access(BASE + 3))[0] == 0x12345678


@cocotb.test()
async def undefined_words_are_errors(dut):
    bus = await setup(dut)
    for offset in (2, 7):
        assert (await bus.access(BASE + offset))[1] is True
        assert (await bus.access(BASE + offset, write=1))[1] is True


@cocotb.test()
async def read_strobe_pulses_once_per_read(dut):
    bus = await setup(dut)
    reads = PulseCounter(dut, dut.snap_read)
    dut.snap.value = 0xCAFEF00D
    assert (await bus.access(BASE + 4))[0] == 0xCAFEF00D
    await bus.access(BASE + 4, write=0)                      # 書き込みでは出ない
    await bus.access(BASE + 0)
    await RisingEdge(dut.clk)
    assert reads.count == 1


@cocotb.test()
async def pulse_fields_pulse_once_per_write_and_read_as_zero(dut):
    bus = await setup(dut)
    starts = PulseCounter(dut, dut.go_start)
    seen = []

    async def watch():
        while True:
            await RisingEdge(dut.clk)
            await ReadOnly()
            if int(dut.go_lanes.value):
                seen.append(int(dut.go_lanes.value))

    cocotb.start_soon(watch())
    await bus.access(BASE + 5, write=0xA1)                   # start = 1, lanes = 0xA
    await bus.access(BASE + 5, write=0x50)                   # start = 0, lanes = 5
    assert (await bus.access(BASE + 5))[0] == 0
    await RisingEdge(dut.clk)
    assert starts.count == 1 and seen == [0xA, 5]


@cocotb.test()
async def response_is_zero_outside_the_ack_cycle(dut):
    # csr_bus_pkg の約束: 応答の周期以外の bus_rdata・bus_err は 0（csr_decoder が窓の応答の論理和を取る）
    bus = await setup(dut)
    await bus.access(BASE + 3, write=0xFFFFFFFF)
    assert (await bus.access(BASE + 3))[0] == 0xFFFFFFFF
    assert (await bus.access(BASE + 7))[1] is True
    for _ in range(3):
        await RisingEdge(dut.clk)
        await ReadOnly()
        assert int(dut.bus_ack.value) == 0 and int(dut.bus_rdata.value) == 0 and int(dut.bus_err.value) == 0


@cocotb.test()
async def write_to_register_reads_its_input_and_writes_the_target(dut):
    bus = await setup(dut)
    dut.pins.value = 0x3C
    assert (await bus.access(BASE + 6))[0] == 0x3C
    assert (await bus.access(BASE + 6, write=0x1234_5678))[1] is False
    assert (await bus.access(BASE + 3))[0] == 0x1234_5678      # 書き先（word）に入る
    assert (await bus.access(BASE + 6))[0] == 0x3C             # 読みは入力のまま
    await ReadOnly()
    assert int(dut.word.value) == 0x1234_5678
