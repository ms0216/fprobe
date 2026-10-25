"""DUT のピン診断ブロック: PS から端子ごとに出力・入力を切り替えて読み戻す（実機の配線・端子の確認用）。"""
import cocotb
from cocotb.clock import Clock
from cocotb.handle import Release
from cocotb.triggers import ClockCycles, FallingEdge, RisingEdge, Timer

OE, OUT, IN, ID = range(4)


async def start(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.rst.value = 1
    dut.wr_en.value = 0
    dut.addr.value = 0
    dut.wdata.value = 0
    await ClockCycles(dut.clk, 3)
    dut.rst.value = 0
    await ClockCycles(dut.clk, 2)


async def write(dut, addr, value):
    await FallingEdge(dut.clk)
    dut.addr.value = addr
    dut.wdata.value = value
    dut.wr_en.value = 1
    await RisingEdge(dut.clk)
    await FallingEdge(dut.clk)
    dut.wr_en.value = 0


async def read(dut, addr):
    await FallingEdge(dut.clk)
    dut.addr.value = addr
    await Timer(1, unit="ns")
    return int(dut.rdata.value)


@cocotb.test()
async def test_id_register(dut):
    await start(dut)
    assert await read(dut, ID) == 0x50494E01


@cocotb.test()
async def test_driven_pin_reads_back_what_it_drives(dut):
    await start(dut)
    await write(dut, OUT, 0b0001)
    await write(dut, OE, 0b0001)
    await ClockCycles(dut.clk, 4)
    assert (await read(dut, IN)) & 1 == 1
    assert str(dut.pin.value)[-1] == "1"
    await write(dut, OUT, 0)
    await ClockCycles(dut.clk, 4)
    assert (await read(dut, IN)) & 1 == 0


@cocotb.test()
async def test_released_pin_follows_the_outside(dut):
    await start(dut)
    await write(dut, OE, 0)
    dut.pin.value = 0b0100                      # 外から bit2 だけ High（ほかは Low）
    await ClockCycles(dut.clk, 4)
    assert await read(dut, IN) == 0b0100
    assert await read(dut, OE) == 0
    dut.pin.value = Release()


@cocotb.test()
async def test_unused_bits_read_zero_and_outputs_are_not_driven_until_enabled(dut):
    await start(dut)
    await write(dut, OUT, 0xFF)                 # OE が 0 の間は出力しない（外から見て Z）
    await ClockCycles(dut.clk, 3)
    assert "Z" in str(dut.pin.value) and "1" not in str(dut.pin.value)
