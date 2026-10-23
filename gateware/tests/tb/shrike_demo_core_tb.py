"""shrike_demo_core（Shrike-Lite のデモの FPGA 側）。RP2040 の役の SPI で計算を頼み、同じ出来事を fprobe の役の
JTAG で観測の記録として読む（MCU と FPGA の両方から同じ出来事が見える）。"""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles

from tests.tb.demo_spi_slave_tb import Master
from tests.tb.obs_soft_core_tb import Jtag, bits_of, command


class SpiPins:
    """Master が使う名前（sck・cs_n・mosi・miso）をデモの spi_* のピンに向ける。"""

    def __init__(self, dut):
        self.sck, self.cs_n, self.mosi, self.miso = dut.spi_sck, dut.spi_cs_n, dut.spi_mosi, dut.spi_miso


async def spi_write(m, addr, data):
    await m.transfer(addr, data)


async def spi_read(m, addr):
    return await m.transfer(0x80 | addr) & 0xFFFF


@cocotb.test()
async def a_computation_is_seen_from_both_sides(dut):
    cocotb.start_soon(Clock(dut.clk, 20, unit="ns").start())
    dut.nreset.value = 0
    dut.spi_sck.value = 0
    dut.spi_cs_n.value = 1
    dut.spi_mosi.value = 0
    m = Master(SpiPins(dut))
    jtag = Jtag(dut)
    await ClockCycles(dut.clk, 5)
    dut.nreset.value = 1
    await ClockCycles(dut.clk, 5)

    await jtag.reset()
    await jtag.shift(bits_of(0x2, 4), ir=True)                      # USER
    await jtag.dr(command(1, 14, 1))                                # 観測を始める
    values, running = [], [True]

    async def scan_continuously():                                  # fprobe のエンジンと同じく読み続ける
        while running[0]:
            r = await jtag.dr(0, 4)
            values.extend(r[1:1 + (r[0] & 0xFFFF)])
    scanner = cocotb.start_soon(scan_continuously())

    assert await spi_read(m, 0x00) == 0x5D01
    await spi_write(m, 0x02, 7)
    await spi_write(m, 0x03, 5)
    await spi_write(m, 0x01, 1)
    await ClockCycles(dut.clk, 20)
    assert await spi_read(m, 0x06) == 0b10 and await spi_read(m, 0x04) == 35
    await ClockCycles(dut.clk, 2000)
    running[0] = False
    await scanner

    assert not any(w >> 30 == 2 for w in values)                    # 溢れていない
    probes = [w & 0xFFFF for w in values if w >> 30 == 0]
    states = [v & 3 for v in probes]
    assert 1 in states and states[-1] == 2                          # 計算中 → 終わった
    addrs = [v >> 5 & 0x7 for v in probes]
    assert [a for k, a in enumerate(addrs) if k == 0 or a != addrs[k - 1]] == [0x00, 0x02, 0x03, 0x01, 0x06, 0x04]


@cocotb.test()
async def probes_carry_drop_toggle_stuck_and_mode(dut):
    # 観測の信号（16 ビット）の上位 4 ビット: bit12 = S2 で捨てるたびに反転（標本型でも数え落とさない）、
    # bit13 = S4 で固まっている、bit15:14 = MODE の下位 2 ビット
    cocotb.start_soon(Clock(dut.clk, 20, unit="ns").start())
    dut.nreset.value = 0
    dut.spi_sck.value = 0
    dut.spi_cs_n.value = 1
    dut.spi_mosi.value = 0
    dut.tck.value = 0
    dut.tms.value = 1
    dut.tdi.value = 0
    m = Master(SpiPins(dut))
    await ClockCycles(dut.clk, 5)
    dut.nreset.value = 1
    await ClockCycles(dut.clk, 5)
    probes = lambda: int(dut.probes.value)                          # noqa: E731
    await spi_write(m, 0x08, 0b110)                                 # MODE = S2 + S4
    assert probes() >> 14 == 0b10
    await spi_write(m, 0x09, 1)                                     # S2_MASK = 1: 約半分を捨てる
    toggles, last = 0, probes() >> 12 & 1
    for k in range(20):
        await spi_write(m, 0x03, k)
        bit = probes() >> 12 & 1
        toggles += bit != last
        last = bit
    assert toggles > 0
    assert toggles % 16 == (await spi_read(m, 0x0A)) & 0xF          # EVENTS の捨てた回数と同じ
    assert probes() >> 13 & 1 == 0
    await spi_write(m, 0x09, 0)
    await spi_write(m, 0x02, 0xFF)
    await spi_write(m, 0x03, 100)
    await spi_write(m, 0x01, 1)
    await spi_write(m, 0x01, 1)                                     # 計算中の開始: S4 で固まる
    assert probes() >> 13 & 1 == 1
