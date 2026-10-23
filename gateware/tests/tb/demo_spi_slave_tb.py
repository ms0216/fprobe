"""demo_spi_slave（Shrike-Lite のデモ: RP2040 から FPGA のレジスタを読み書きする SPI モード 0 のスレーブ）。

1 回の転送 = CS を下げて 24 ビット（MSB から）: 命令 8 ビット（bit7 = 1 で読み、6:0 = 番地）+ データ 16 ビット。
書きは CS を上げたときに 1 回だけ wr_valid。読みは命令の 8 ビットの後に rd_data を MISO に出す。
clk 50 MHz、SCK 1 MHz（clk で標本化するので SCK は clk の 1/8 以下）。"""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge, Timer

SCK_HALF_NS = 500


class Master:
    def __init__(self, dut):
        self.dut = dut
        dut.sck.value = 0
        dut.cs_n.value = 1
        dut.mosi.value = 0

    async def transfer(self, cmd: int, data: int = 0, bits: int = 24) -> int:
        dut = self.dut
        word = (cmd << 16 | data) >> (24 - bits) if bits < 24 else cmd << 16 | data
        dut.cs_n.value = 0
        await Timer(SCK_HALF_NS, "ns")
        got = 0
        for k in range(bits - 1, -1, -1):
            dut.mosi.value = word >> k & 1
            await Timer(SCK_HALF_NS, "ns")
            dut.sck.value = 1                                # 主は立ち上がりで MISO を読む
            got = got << 1 | int(dut.miso.value)
            await Timer(SCK_HALF_NS, "ns")
            dut.sck.value = 0
        await Timer(SCK_HALF_NS, "ns")
        dut.cs_n.value = 1
        await Timer(4 * SCK_HALF_NS, "ns")
        return got


async def setup(dut):
    cocotb.start_soon(Clock(dut.clk, 20, unit="ns").start())
    dut.rst.value = 1
    dut.rd_data.value = 0
    m = Master(dut)
    await ClockCycles(dut.clk, 5)
    dut.rst.value = 0
    await ClockCycles(dut.clk, 5)
    return m


async def collect_writes(dut, out):
    while True:
        await RisingEdge(dut.clk)
        if dut.wr_valid.value == 1:
            out.append((int(dut.wr_addr.value), int(dut.wr_data.value)))


@cocotb.test()
async def a_write_is_delivered_once_when_cs_rises(dut):
    m = await setup(dut)
    writes = []
    cocotb.start_soon(collect_writes(dut, writes))
    await m.transfer(0x02, 0xBEEF)
    await m.transfer(0x7F, 0x0102)
    assert writes == [(0x02, 0xBEEF), (0x7F, 0x0102)]


@cocotb.test()
async def a_read_shifts_out_the_register_after_the_command(dut):
    m = await setup(dut)
    writes = []
    cocotb.start_soon(collect_writes(dut, writes))

    async def answer():
        while True:
            await RisingEdge(dut.clk)
            dut.rd_data.value = 0xA500 | int(dut.rd_addr.value)
    cocotb.start_soon(answer())
    got = await m.transfer(0x80 | 0x05)
    assert got & 0xFFFF == 0xA505
    assert writes == []                                     # 読みは書きにならない


@cocotb.test()
async def an_incomplete_transfer_writes_nothing(dut):
    m = await setup(dut)
    writes = []
    cocotb.start_soon(collect_writes(dut, writes))
    await m.transfer(0x02, 0x1234, bits=20)
    await m.transfer(0x03, 0x5678)
    assert writes == [(0x03, 0x5678)]
