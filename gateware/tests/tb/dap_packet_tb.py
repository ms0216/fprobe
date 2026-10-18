"""dap_packet（CMSIS-DAP のパケットの枠）のテストベンチ。ユニット側はテストが演じる。"""

import random

import cocotb
from cocotb.triggers import ReadOnly, RisingEdge

from tests.tb.common import start_clock_and_reset
from tests.tb.control_port import ControlPort

U_NONE, U_CONFIG = 0, 1                      # dap_unit_t の並び


class FakeUnit:
    """start を待ち、手順（("get", n) / ("put", n, value) / ("done", header, n)）を順に行う。"""

    def __init__(self, dut):
        self.dut = dut
        self.args: list[int] = []
        for name in ("done", "get_req", "put_req"):
            getattr(dut, name).value = 0
        dut.get_n.value = dut.put_n.value = 1
        dut.put_val.value = 0
        dut.header.value = 0
        dut.header_n.value = 0

    async def pulse(self, name):
        getattr(self.dut, name).value = 1
        await RisingEdge(self.dut.clk)
        getattr(self.dut, name).value = 0

    async def wait_for(self, name):
        while True:
            await RisingEdge(self.dut.clk)
            await ReadOnly()
            if getattr(self.dut, name).value == 1:
                await RisingEdge(self.dut.clk)
                return

    async def run(self, steps):
        await self.wait_for("start")
        for step in steps:
            if step[0] == "get":
                self.dut.get_n.value = step[1]
                await self.pulse("get_req")
                await self.wait_for("get_ack")
                self.args.append(int(self.dut.arg.value))
            elif step[0] == "put":
                self.dut.put_n.value, self.dut.put_val.value = step[1], step[2]
                await self.pulse("put_req")
                await self.wait_for("put_ack")
            else:
                self.dut.header.value, self.dut.header_n.value = step[1], step[2]
                await self.pulse("done")


async def setup(dut, packet_size=64):
    dut.packet_size.value = packet_size
    port = ControlPort(dut, request="in", response="out", rng=random.Random(2), gap=0.3, stall=0.3)
    unit = FakeUnit(dut)
    await start_clock_and_reset(dut)
    return port, unit


@cocotb.test()
async def unsupported_and_abort(dut):
    port, _ = await setup(dut)
    assert await port.transact(bytes([0xF0, 1, 2, 3])) == b"\xff"           # 担当なし: 0xFF の 1 バイト
    # TransferAbort（0x07）は応答なし。次のコマンドは普通に処理される
    dut.in_valid.value, dut.in_data.value, dut.in_last.value = 1, 0x07, 1
    await RisingEdge(dut.clk)
    while not dut.in_ready.value:
        await RisingEdge(dut.clk)
    dut.in_valid.value = 0
    for _ in range(50):
        await RisingEdge(dut.clk)
        assert dut.out_valid.value == 0
    assert await port.transact(bytes([0xF1])) == b"\xff"


@cocotb.test()
async def arguments_responses_and_header(dut):
    port, unit = await setup(dut)
    task = cocotb.start_soon(unit.run([("get", 2), ("put", 3, 0xCCBBAA), ("get", 4), ("done", 0x77, 1)]))
    response = await port.transact(bytes([0x00, 0x34, 0x12, 0x01]))         # 0x00 = Info（config ユニット）
    await task
    # 2 つ目の get はパケットの残り 1 バイト + 足りない分は 0
    assert unit.args == [0x1234, 0x01]
    assert response == bytes([0x00, 0x77, 0xBB, 0xCC])                      # [1] を header で書き直し


@cocotb.test()
async def response_is_cut_at_packet_size(dut):
    port, unit = await setup(dut, packet_size=64)
    steps = [("put", 8, 0x0807060504030201)] * 10 + [("done", 0, 0)]
    task = cocotb.start_soon(unit.run(steps))
    response = await port.transact(bytes([0x00]))
    await task
    assert len(response) == 64 and response[1:9] == bytes(range(1, 9))
