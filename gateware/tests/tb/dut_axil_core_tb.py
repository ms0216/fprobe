"""DUT の AXI-Lite の受け口 + コア。PS の 0x40000000 から 4 KB に見える形（語番地は addr[5:2]）。"""
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, FallingEdge, RisingEdge

CTRL, PERIOD, DATA, LEVEL, OVERFLOWS, GENERATED, FAULT, ID = range(8)


async def start(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.rst.value = 1
    for s in ("s_axi_awvalid", "s_axi_wvalid", "s_axi_arvalid", "s_axi_bready", "s_axi_rready"):
        getattr(dut, s).value = 0
    dut.s_axi_awaddr.value = 0
    dut.s_axi_wdata.value = 0
    dut.s_axi_wstrb.value = 0xF
    dut.s_axi_araddr.value = 0
    await ClockCycles(dut.clk, 3)
    dut.rst.value = 0
    await ClockCycles(dut.clk, 2)


async def axi_write(dut, word, value):
    await FallingEdge(dut.clk)
    dut.s_axi_awaddr.value = word * 4
    dut.s_axi_wdata.value = value
    dut.s_axi_awvalid.value = 1
    dut.s_axi_wvalid.value = 1
    dut.s_axi_bready.value = 1
    for _ in range(20):
        await RisingEdge(dut.clk)
        if int(dut.s_axi_awready.value) and int(dut.s_axi_wready.value):
            break
    else:
        raise AssertionError("AW / W が受け付けられない")
    await FallingEdge(dut.clk)
    dut.s_axi_awvalid.value = 0
    dut.s_axi_wvalid.value = 0
    for _ in range(20):
        if int(dut.s_axi_bvalid.value):
            break
        await RisingEdge(dut.clk)
        await FallingEdge(dut.clk)
    else:
        raise AssertionError("B 応答が来ない")
    assert int(dut.s_axi_bresp.value) == 0
    await RisingEdge(dut.clk)
    await FallingEdge(dut.clk)
    dut.s_axi_bready.value = 0


async def axi_read(dut, word):
    await FallingEdge(dut.clk)
    dut.s_axi_araddr.value = word * 4
    dut.s_axi_arvalid.value = 1
    dut.s_axi_rready.value = 1
    for _ in range(20):
        await RisingEdge(dut.clk)
        if int(dut.s_axi_arready.value):
            break
    else:
        raise AssertionError("AR が受け付けられない")
    await FallingEdge(dut.clk)
    dut.s_axi_arvalid.value = 0
    for _ in range(20):
        if int(dut.s_axi_rvalid.value):
            break
        await RisingEdge(dut.clk)
        await FallingEdge(dut.clk)
    else:
        raise AssertionError("R 応答が来ない")
    value = int(dut.s_axi_rdata.value)
    assert int(dut.s_axi_rresp.value) == 0
    await RisingEdge(dut.clk)
    await FallingEdge(dut.clk)
    dut.s_axi_rready.value = 0
    return value


@cocotb.test()
async def test_id_over_axi(dut):
    await start(dut)
    assert await axi_read(dut, ID) == 0x44555401


@cocotb.test()
async def test_write_reaches_the_register(dut):
    await start(dut)
    await axi_write(dut, PERIOD, 0x1234)
    assert await axi_read(dut, PERIOD) == 0x1234


@cocotb.test()
async def test_data_read_pops_exactly_once_per_axi_read(dut):
    await start(dut)
    await axi_write(dut, PERIOD, 10)
    await axi_write(dut, CTRL, 1)
    await ClockCycles(dut.clk, 120)
    await axi_write(dut, CTRL, 0)
    assert [await axi_read(dut, DATA) for _ in range(4)] == [0, 1, 2, 3]
    pulses = 0
    previous = 0
    level = await axi_read(dut, LEVEL)
    for _ in range(3):
        await FallingEdge(dut.clk)
    dut.s_axi_araddr.value = DATA * 4
    dut.s_axi_arvalid.value = 1
    dut.s_axi_rready.value = 1
    for _ in range(12):
        await RisingEdge(dut.clk)
        now = int(dut.dbg.value) >> 3 & 1
        pulses += now and not previous                # 拍は 4 クロックに伸びる: 立ち上がりを数える
        previous = now
        await FallingEdge(dut.clk)
        if int(dut.s_axi_arready.value):
            dut.s_axi_arvalid.value = 0
    await ClockCycles(dut.clk, 3)
    assert pulses == 1, pulses
    assert await axi_read(dut, LEVEL) == level - 1


PIN_OE, PIN_OUT, PIN_IN, PIN_ID = 8, 9, 10, 11


@cocotb.test()
async def test_pin_block_is_reachable_over_axi_and_does_not_disturb_the_stream_registers(dut):
    # ピン診断ブロック（語番地 8〜11）。端子を出して読み戻し、ストリームのレジスタ（0〜7）には影響しない
    await start(dut)
    assert await axi_read(dut, PIN_ID) == 0x50494E01
    assert await axi_read(dut, ID) == 0x44555401
    await axi_write(dut, PIN_OUT, 0b01)
    await axi_write(dut, PIN_OE, 0b01)
    await ClockCycles(dut.clk, 4)
    assert (await axi_read(dut, PIN_IN)) & 1 == 1
    assert str(dut.pins.value)[-1] == "1"
    assert await axi_read(dut, PERIOD) == 1000                  # ストリームの PERIOD は既定のまま（書いていない）
    assert await axi_read(dut, PIN_OE) == 0b01
