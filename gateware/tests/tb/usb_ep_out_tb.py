"""usb_ep_out（DEPTH=64, MPS=16）のテストベンチ。"""

import random

import cocotb
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge

from tests.tb.common import start_clock_and_reset

DEPTH, MPS = 64, 16


class Reader:
    """ユーザー側の出力を、out_last で区切ったパケットとして集める。"""

    def __init__(self, dut, rng, *, ready_prob=1.0):
        self.packets: list[bytes] = []
        self.enabled = True
        self._dut, self._rng, self._p = dut, rng, ready_prob
        cocotb.start_soon(self._run())

    async def _run(self):
        d = self._dut
        current = bytearray()
        while True:
            d.out_ready.value = int(self.enabled and self._rng.random() < self._p)
            await ReadOnly()
            if int(d.out_valid.value) and int(d.out_ready.value):
                current.append(int(d.out_data.value))
                if int(d.out_last.value):
                    self.packets.append(bytes(current))
                    current = bytearray()
            await RisingEdge(d.clk)


async def setup(dut, *, ready_prob=1.0):
    dut.packet_size.value = MPS
    dut.wr_valid.value = 0
    dut.wr_data.value = 0
    dut.commit.value = 0
    dut.discard.value = 0
    await start_clock_and_reset(dut)
    return Reader(dut, random.Random(2), ready_prob=ready_prob)


async def write_packet(dut, payload: bytes, *, keep: bool):
    for byte in payload:
        dut.wr_valid.value = 1
        dut.wr_data.value = byte
        await RisingEdge(dut.clk)
        dut.wr_valid.value = 0
        await RisingEdge(dut.clk)
    (dut.commit if keep else dut.discard).value = 1
    await RisingEdge(dut.clk)
    dut.commit.value = 0
    dut.discard.value = 0
    await RisingEdge(dut.clk)


@cocotb.test()
async def committed_packets_come_out_with_boundaries(dut):
    reader = await setup(dut, ready_prob=0.5)
    packets = [bytes(range(16)), b"\x99", bytes(range(100, 110))]
    for p in packets:
        await write_packet(dut, p, keep=True)
    await ClockCycles(dut.clk, 100)
    assert reader.packets == packets


@cocotb.test()
async def discarded_packets_leave_no_trace(dut):
    reader = await setup(dut)
    await write_packet(dut, b"keep-1", keep=True)
    await write_packet(dut, b"drop-me", keep=False)
    await write_packet(dut, b"keep-2", keep=True)
    await ClockCycles(dut.clk, 50)
    assert reader.packets == [b"keep-1", b"keep-2"]


@cocotb.test()
async def zero_length_packet_is_ignored(dut):
    reader = await setup(dut)
    await write_packet(dut, b"", keep=True)
    await write_packet(dut, b"x", keep=True)
    await ClockCycles(dut.clk, 20)
    assert reader.packets == [b"x"]


@cocotb.test()
async def space_for_packet_reflects_free_room(dut):
    reader = await setup(dut)
    reader.enabled = False                       # 読み出さずに溜める
    await ReadOnly()
    assert int(dut.space_for_packet.value) == 1
    await RisingEdge(dut.clk)
    for _ in range(DEPTH // MPS - 1):
        await write_packet(dut, bytes(MPS), keep=True)
    await ReadOnly()
    assert int(dut.space_for_packet.value) == 1   # 1 パケット分以上残っている
    await RisingEdge(dut.clk)
    # 先頭の 1 バイトは出力レジスタに先読みされメモリを空けるので、空きを MPS 未満にするには 2 バイト要る
    await write_packet(dut, bytes(2), keep=True)
    await ReadOnly()
    assert int(dut.space_for_packet.value) == 0
    await RisingEdge(dut.clk)
    reader.enabled = True
    await ClockCycles(dut.clk, 120)
    await ReadOnly()
    assert int(dut.space_for_packet.value) == 1


@cocotb.test()
async def space_check_uses_runtime_packet_size(dut):
    reader = await setup(dut)
    reader.enabled = False
    dut.packet_size.value = 4
    await RisingEdge(dut.clk)
    await write_packet(dut, bytes(DEPTH - 5), keep=True)   # 先読み 1 バイト分で空きは 6
    await ReadOnly()
    assert int(dut.space_for_packet.value) == 1             # 4 バイトのパケットなら入る
    await RisingEdge(dut.clk)
    dut.packet_size.value = MPS
    await ReadOnly()
    assert int(dut.space_for_packet.value) == 0             # MPS（16）では入らない
