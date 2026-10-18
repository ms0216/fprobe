"""csr_decoder（ハーネス経由）のテストベンチ。"""

import cocotb
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge

from tests.tb.common import start_clock_and_reset


class CsrMaster:
    """CSR バスのマスタ（要求を応答まで保持する）。応答までの周期数も返す。"""

    def __init__(self, dut):
        self.dut = dut
        dut.bus_req.value = 0
        dut.bus_we.value = 0
        dut.bus_addr.value = 0
        dut.bus_wdata.value = 0

    async def access(self, addr, *, write=None, timeout=50):
        d = self.dut
        d.bus_req.value = 1
        d.bus_we.value = int(write is not None)
        d.bus_addr.value = addr
        d.bus_wdata.value = write or 0
        for cycles in range(1, timeout):
            await RisingEdge(d.clk)
            await ReadOnly()
            if int(d.bus_ack.value):
                result = (int(d.bus_rdata.value), bool(int(d.bus_err.value)), cycles)
                await RisingEdge(d.clk)
                d.bus_req.value = 0
                return result
        raise AssertionError(f"no ack for address {addr}")


@cocotb.test()
async def routes_to_windows_and_rejects_unmapped(dut):
    await start_clock_and_reset(dut)
    bus = CsrMaster(dut)
    for i, value in enumerate((0x11111111, 0x22222222, 0x33333333, 0x44444444)):
        assert (await bus.access(i, write=value))[1] is False
    for i, value in enumerate((0x11111111, 0x22222222, 0x33333333, 0x44444444)):
        data, err, _ = await bus.access(i)
        assert (data, err) == (value, False)
    data, err, cycles = await bus.access(20)
    assert (data, err) == (0xA0000014, False) and cycles >= 4      # 待ちのあるスレーブ
    assert (await bus.access(31))[1] is True                         # スレーブのエラー
    for addr in (4, 15, 32, 0x3FFFFFFF):
        assert (await bus.access(addr))[1] is True, addr             # 未割り当て


@cocotb.test()
async def back_to_back_requests(dut):
    await start_clock_and_reset(dut)
    bus = CsrMaster(dut)
    for _ in range(3):
        assert (await bus.access(5))[1] is True
        assert (await bus.access(2, write=7))[1] is False
        assert (await bus.access(2))[0] == 7
    await ClockCycles(dut.clk, 2)
