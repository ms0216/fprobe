"""usb_protocol（ハーネス経由）のテストベンチ。デバイスアドレス 5、HS 動作。"""

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, FallingEdge, ReadOnly, RisingEdge

from tests.models import usb_packets as up
from tests.models.ulpi_phy import UlpiPhy
from tests.models.usb_host import HS_RESPONSE_LIMIT, UsbHost
from tests.tb.common import StreamCollector, stream_send

ADDR = 5
SETUP_GET_DESCRIPTOR = bytes.fromhex("8006000100004000")


class Device:
    def __init__(self, dut):
        self.dut = dut
        self.phy = UlpiPhy(dut, dut.clk, rng=random.Random(11), nxt_throttle=0.2)
        self.host = UsbHost(self.phy, dut.clk, address=ADDR)
        self.setups = 0

    def start_monitors(self):
        """リセット解除後に呼ぶ（それ以前の出力は未定義）。"""
        self.ep0_out = StreamCollector(self.dut, "ep0_out")
        self.ep1_out = StreamCollector(self.dut, "ep1_out")
        cocotb.start_soon(self._count_setups())

    async def _count_setups(self):
        while True:
            await RisingEdge(self.dut.clk)
            await ReadOnly()
            self.setups += int(self.dut.setup_seen.value)


async def setup(dut) -> Device:
    for name in ("bus_reset", "out_halt", "in_halt", "toggle_reset", "ep0_in_valid",
                 "ep1_in_valid", "ep0_in_data", "ep1_in_data", "ep0_in_last", "ep1_in_last"):
        getattr(dut, name).value = 0
    dut.address.value = ADDR
    dut.high_speed.value = 1
    device = Device(dut)
    cocotb.start_soon(Clock(dut.clk, 16, unit="ns").start())
    dut.rst.value = 1
    await ClockCycles(dut.clk, 3)
    dut.rst.value = 0
    await RisingEdge(dut.clk)
    device.start_monitors()
    while not device.phy.ready:
        await FallingEdge(dut.clk)
    await ClockCycles(dut.clk, 20)
    return device


def assert_handshake(response, pid):
    assert response.packet == up.Handshake(pid), response
    assert response.latency <= HS_RESPONSE_LIMIT, response.latency


@cocotb.test()
async def setup_is_acked_and_delivered(dut):
    dev = await setup(dut)
    assert_handshake(await dev.host.setup(SETUP_GET_DESCRIPTOR), up.ACK)
    await ClockCycles(dut.clk, 20)
    assert bytes(dev.ep0_out.items) == SETUP_GET_DESCRIPTOR
    assert dev.setups == 1


@cocotb.test()
async def malformed_setup_gets_no_response(dut):
    dev = await setup(dut)
    await dev.host.send(up.token(up.SETUP, ADDR, 0))
    await dev.host.send(up.data_packet(up.DATA0, b"short"))
    assert (await dev.host.response()).packet is None
    assert dev.setups == 0


@cocotb.test()
async def bulk_out_alternates_toggles(dut):
    dev = await setup(dut)
    assert_handshake(await dev.host.out(1, up.DATA0, b"first"), up.ACK)
    assert_handshake(await dev.host.out(1, up.DATA1, b"second"), up.ACK)
    await ClockCycles(dut.clk, 30)
    assert bytes(dev.ep1_out.items) == b"firstsecond"


@cocotb.test()
async def resent_out_packet_is_acked_but_not_duplicated(dut):
    dev = await setup(dut)
    assert_handshake(await dev.host.out(1, up.DATA0, b"once"), up.ACK)
    assert_handshake(await dev.host.out(1, up.DATA0, b"once"), up.ACK)   # ACK を取りこぼした想定
    await ClockCycles(dut.clk, 30)
    assert bytes(dev.ep1_out.items) == b"once"


@cocotb.test()
async def corrupted_out_packet_gets_no_response(dut):
    dev = await setup(dut)
    assert (await dev.host.out(1, up.DATA0, b"bad", corrupt=True)).packet is None
    assert_handshake(await dev.host.out(1, up.DATA0, b"good"), up.ACK)   # トグルは進んでいない
    await ClockCycles(dut.clk, 30)
    assert bytes(dev.ep1_out.items) == b"good"


@cocotb.test()
async def other_address_and_missing_endpoint_are_ignored(dut):
    dev = await setup(dut)
    dev.host.address = ADDR + 1
    assert (await dev.host.out(1, up.DATA0, b"x")).packet is None
    dev.host.address = ADDR
    assert (await dev.host.in_(7)).packet is None


@cocotb.test()
async def full_buffer_naks_and_ping_reports_space(dut):
    dev = await setup(dut)
    dut.ep1_out_ready.value = 0
    dev.ep1_out.items.clear()
    toggle = up.DATA0
    # 1024 バイトのバッファに 512 バイトを 1 回入れると、残りはちょうど 1 パケット分 → まだ ACK
    assert_handshake(await dev.host.out(1, toggle, bytes(512)), up.ACK)
    toggle = up.DATA1
    # 2 回目で満杯。受理はするが次の分の空きが無いので NYET
    assert_handshake(await dev.host.out(1, toggle, bytes(512)), up.NYET)
    assert_handshake(await dev.host.ping(1), up.NAK)
    assert_handshake(await dev.host.out(1, up.DATA0, bytes(512)), up.NAK)
    dut.ep1_out_ready.value = 1                     # 読み出して空ける
    await ClockCycles(dut.clk, 1200)
    assert_handshake(await dev.host.ping(1), up.ACK)


@cocotb.test()
async def in_naks_when_empty_and_sends_with_toggles(dut):
    dev = await setup(dut)
    assert_handshake(await dev.host.in_(1), up.NAK)
    await stream_send(dut, "ep1_in", b"abc")
    dut.ep1_in_last.value = 0
    await ClockCycles(dut.clk, 40)                  # 小休止で短パケットとして閉じる
    r = await dev.host.in_(1)
    assert r.packet == up.Data(up.DATA0, b"abc") and r.latency <= HS_RESPONSE_LIMIT
    await stream_send(dut, "ep1_in", b"def")
    await ClockCycles(dut.clk, 40)
    assert (await dev.host.in_(1)).packet == up.Data(up.DATA1, b"def")


@cocotb.test()
async def missing_ack_resends_same_packet(dut):
    dev = await setup(dut)
    await stream_send(dut, "ep1_in", b"xyz")
    await ClockCycles(dut.clk, 40)
    first = await dev.host.in_(1, ack=False)        # ACK を返さない
    again = await dev.host.in_(1)                   # 次の IN（ACK 喪失）
    assert first.packet == again.packet == up.Data(up.DATA0, b"xyz")
    assert_handshake(await dev.host.in_(1), up.NAK)  # ACK 後は空


@cocotb.test()
async def halted_endpoints_stall(dut):
    dev = await setup(dut)
    dut.out_halt.value = 0b10
    dut.in_halt.value = 0b10
    assert_handshake(await dev.host.out(1, up.DATA0, b"x"), up.STALL)
    assert_handshake(await dev.host.ping(1), up.STALL)
    assert_handshake(await dev.host.in_(1), up.STALL)
    assert_handshake(await dev.host.setup(SETUP_GET_DESCRIPTOR), up.ACK)  # EP0 は影響なし


@cocotb.test()
async def toggle_reset_restarts_at_data0(dut):
    dev = await setup(dut)
    assert_handshake(await dev.host.out(1, up.DATA0, b"a"), up.ACK)
    dut.toggle_reset.value = 0b10
    await RisingEdge(dut.clk)
    dut.toggle_reset.value = 0
    assert_handshake(await dev.host.out(1, up.DATA0, b"b"), up.ACK)
    await ClockCycles(dut.clk, 30)
    assert bytes(dev.ep1_out.items) == b"ab"        # DATA0 の再送とみなされず受理された
