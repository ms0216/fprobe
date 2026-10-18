"""usb_control_ep（ハーネス経由）のテストベンチ。期待値は Python のディスクリプタ定義から取る。"""

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, FallingEdge, ReadOnly, RisingEdge

from fprobe_gw.usb import descriptors as d
from fprobe_gw.usb.fprobe_device import CDC_COMM_INTERFACE as CDC_IF
from fprobe_gw.usb.fprobe_device import FPROBE_DEVICE, MSOS20_VENDOR_CODE
from tests.models import usb_packets as up
from tests.models.ulpi_phy import UlpiPhy
from tests.models.usb_host import BulkPipe, Stalled, UsbHost, control_in, control_out, setup_packet
from tests.tb.common import StreamCollector, stream_send

GET_DESCRIPTOR, SET_ADDRESS, SET_CONFIGURATION, GET_CONFIGURATION = 6, 5, 9, 8
GET_STATUS, CLEAR_FEATURE, SET_FEATURE, GET_INTERFACE, SET_INTERFACE = 0, 1, 3, 10, 11

def get_descriptor(dtype, index=0, length=255):
    return setup_packet(0x80, GET_DESCRIPTOR, (dtype << 8) | index, 0, length)


class Handler:
    """クラス要求ハンドラの代役。IN 要求には in_reply を返し、OUT データは out_data に溜める。"""

    def __init__(self, dut):
        self.dut = dut
        self.requests = []
        self.in_reply = b""
        self.stall = False
        self.out_data = bytearray()
        self.out_done = 0
        dut.req_ack.value = 0
        dut.req_stall.value = 0
        dut.req_in_data.value = 0
        dut.req_in_length.value = 0
        cocotb.start_soon(self._run())

    async def _run(self):
        d_ = self.dut
        while True:
            await RisingEdge(d_.clk)
            await ReadOnly()
            if int(d_.req_out_valid.value):
                self.out_data.append(int(d_.req_out_data.value))
            self.out_done += int(d_.req_out_done.value)
            if int(d_.req_valid.value):
                self.requests.append((int(d_.req_type.value), int(d_.req_request.value),
                                      int(d_.req_value.value), int(d_.req_index.value),
                                      int(d_.req_length.value)))
                await RisingEdge(d_.clk)
                await ClockCycles(d_.clk, 3)
                d_.req_in_data.value = int.from_bytes(self.in_reply.ljust(8, b"\0"), "little")
                d_.req_in_length.value = len(self.in_reply)
                (d_.req_stall if self.stall else d_.req_ack).value = 1
                await RisingEdge(d_.clk)
                d_.req_ack.value = 0
                d_.req_stall.value = 0


async def setup(dut, *, high_speed=True):
    for name in ("ep1_out_ready", "ep1_in_valid", "ep1_in_data", "ep1_in_last"):
        getattr(dut, name).value = 0
    dut.high_speed.value = int(high_speed)
    phy = UlpiPhy(dut, dut.clk, rng=random.Random(5), nxt_throttle=0.2)
    host = UsbHost(phy, dut.clk, address=0)
    cocotb.start_soon(Clock(dut.clk, 16, unit="ns").start())
    dut.rst.value = 1
    await ClockCycles(dut.clk, 3)
    dut.rst.value = 0
    await RisingEdge(dut.clk)
    handler = Handler(dut)
    while not phy.ready:
        await FallingEdge(dut.clk)
    await ClockCycles(dut.clk, 20)
    return host, handler


@cocotb.test()
async def device_descriptor(dut):
    host, _ = await setup(dut)
    assert await control_in(host, get_descriptor(d.DEVICE, length=64)) == FPROBE_DEVICE.device_descriptor()


@cocotb.test()
async def configuration_short_and_full_multi_packet(dut):
    host, _ = await setup(dut)
    full = FPROBE_DEVICE.configuration(high_speed=True)
    assert await control_in(host, get_descriptor(d.CONFIGURATION, length=9)) == full[:9]
    assert await control_in(host, get_descriptor(d.CONFIGURATION, length=255)) == full
    assert await control_in(host, get_descriptor(d.CONFIGURATION, length=64)) == full[:64]


@cocotb.test()
async def full_speed_reports_full_speed_configuration(dut):
    host, _ = await setup(dut, high_speed=False)
    assert await control_in(host, get_descriptor(d.CONFIGURATION)) == \
        FPROBE_DEVICE.configuration(high_speed=False)
    assert await control_in(host, get_descriptor(d.OTHER_SPEED_CONFIGURATION)) == \
        FPROBE_DEVICE.other_speed_configuration(high_speed=False)


@cocotb.test()
async def strings_qualifier_bos_and_msos20(dut):
    host, _ = await setup(dut)
    for i, s in enumerate(FPROBE_DEVICE.strings()):
        assert await control_in(host, get_descriptor(d.STRING, i)) == s
    assert await control_in(host, get_descriptor(d.DEVICE_QUALIFIER)) == FPROBE_DEVICE.device_qualifier()
    assert await control_in(host, get_descriptor(d.BOS)) == FPROBE_DEVICE.bos()
    msos = setup_packet(0xC0, MSOS20_VENDOR_CODE, 0, 7, len(FPROBE_DEVICE.msos20_set()))
    assert await control_in(host, msos) == FPROBE_DEVICE.msos20_set()


@cocotb.test()
async def missing_descriptor_stalls_and_next_setup_recovers(dut):
    host, _ = await setup(dut)
    try:
        await control_in(host, get_descriptor(d.STRING, 99))
        raise AssertionError("STALL expected")
    except Stalled:
        pass
    assert await control_in(host, get_descriptor(d.DEVICE, length=18)) == FPROBE_DEVICE.device_descriptor()


@cocotb.test()
async def set_address_takes_effect_after_status(dut):
    host, _ = await setup(dut)
    await control_out(host, setup_packet(0x00, SET_ADDRESS, 23))
    await ClockCycles(dut.clk, 5)
    assert int(dut.address.value) == 23
    host.address = 23
    assert await control_in(host, get_descriptor(d.DEVICE, length=18)) == FPROBE_DEVICE.device_descriptor()
    host.address = 0
    assert (await host.setup(get_descriptor(d.DEVICE))).packet is None   # 旧アドレスには応答しない


@cocotb.test()
async def set_and_get_configuration(dut):
    host, _ = await setup(dut)
    assert await control_in(host, setup_packet(0x80, GET_CONFIGURATION, 0, 0, 1)) == b"\x00"
    await control_out(host, setup_packet(0x00, SET_CONFIGURATION, 1))
    await ClockCycles(dut.clk, 5)                  # ステータス段の ACK が処理されるまで
    assert int(dut.config_value.value) == 1
    assert await control_in(host, setup_packet(0x80, GET_CONFIGURATION, 0, 0, 1)) == b"\x01"
    try:
        await control_out(host, setup_packet(0x00, SET_CONFIGURATION, 2))
        raise AssertionError("STALL expected")
    except Stalled:
        pass


@cocotb.test()
async def endpoint_halt_feature_and_status(dut):
    host, _ = await setup(dut)
    await control_out(host, setup_packet(0x00, SET_CONFIGURATION, 1))
    assert await control_in(host, setup_packet(0x80, GET_STATUS, 0, 0, 2)) == b"\x00\x00"
    await control_out(host, setup_packet(0x02, SET_FEATURE, 0, 0x81))
    assert await control_in(host, setup_packet(0x82, GET_STATUS, 0, 0x81, 2)) == b"\x01\x00"
    assert (await host.in_(1)).packet == up.Handshake(up.STALL)
    await control_out(host, setup_packet(0x02, CLEAR_FEATURE, 0, 0x81))
    assert await control_in(host, setup_packet(0x82, GET_STATUS, 0, 0x81, 2)) == b"\x00\x00"
    assert (await host.in_(1)).packet == up.Handshake(up.NAK)


@cocotb.test()
async def interfaces_alt_zero_only(dut):
    host, _ = await setup(dut)
    await control_out(host, setup_packet(0x00, SET_CONFIGURATION, 1))
    assert await control_in(host, setup_packet(0x81, GET_INTERFACE, 0, 2, 1)) == b"\x00"
    await control_out(host, setup_packet(0x01, SET_INTERFACE, 0, 1))
    try:
        await control_out(host, setup_packet(0x01, SET_INTERFACE, 1, 0))
        raise AssertionError("STALL expected")
    except Stalled:
        pass


@cocotb.test()
async def unsupported_standard_request_stalls(dut):
    host, handler = await setup(dut)
    try:
        await control_out(host, setup_packet(0x00, 0x30))          # 未定義の標準要求
        raise AssertionError("STALL expected")
    except Stalled:
        pass
    assert handler.requests == []                                  # ハンドラには渡さない


@cocotb.test()
async def class_requests_are_delegated(dut):
    host, handler = await setup(dut)
    line_coding = (115200).to_bytes(4, "little") + bytes([0, 0, 8])
    await control_out(host, setup_packet(0x21, 0x20, 0, CDC_IF, 7), line_coding)   # SET_LINE_CODING
    assert bytes(handler.out_data) == line_coding and handler.out_done == 1
    handler.in_reply = line_coding
    assert await control_in(host, setup_packet(0xA1, 0x21, 0, CDC_IF, 7)) == line_coding
    await control_out(host, setup_packet(0x21, 0x22, 3, CDC_IF, 0))                 # SET_CONTROL_LINE_STATE
    assert handler.requests[-1] == (0x21, 0x22, 3, CDC_IF, 0)
    handler.stall = True
    try:
        await control_out(host, setup_packet(0x21, 0x23, 0, CDC_IF, 0))
        raise AssertionError("STALL expected")
    except Stalled:
        pass


@cocotb.test()
async def class_out_request_spanning_several_packets(dut):
    # データ段の次のパケットが、前のパケットをハンドラへ渡している最中に届いても失われない
    host, handler = await setup(dut)
    data = bytes(range(130))                                   # 64 + 64 + 2 バイト
    await control_out(host, setup_packet(0x21, 0x20, 0, CDC_IF, len(data)), data)
    assert bytes(handler.out_data) == data and handler.out_done == 1


@cocotb.test()
async def clearing_in_halt_keeps_out_toggle(dut):
    # CLEAR_FEATURE(ENDPOINT_HALT) はその方向のトグルだけを DATA0 に戻す（レビューで発見: 双方向を戻していた）
    host, _ = await setup(dut)
    out = StreamCollector(dut, "ep1_out")
    await control_out(host, setup_packet(0x00, SET_CONFIGURATION, 1))
    assert (await host.out(1, up.DATA0, b"a")).packet == up.Handshake(up.ACK)   # 次は DATA1
    await control_out(host, setup_packet(0x02, SET_FEATURE, 0, 0x81))
    await control_out(host, setup_packet(0x02, CLEAR_FEATURE, 0, 0x81))
    assert (await host.out(1, up.DATA1, b"b")).packet == up.Handshake(up.ACK)
    await ClockCycles(dut.clk, 30)
    assert bytes(out.items) == b"ab"                                          # "b" も受理された


@cocotb.test()
async def interface_and_configuration_reinitialize_endpoints(dut):
    # 同じ値の再設定でも、対象エンドポイントの HALT 解除・トグル DATA0・バッファを空にする
    host, _ = await setup(dut)
    out = StreamCollector(dut, "ep1_out", always_ready=False)
    await control_out(host, setup_packet(0x00, SET_CONFIGURATION, 1))
    # EP1 は IF1（fprobe 拡張）
    for request in (setup_packet(0x01, SET_INTERFACE, 0, 1), setup_packet(0x00, SET_CONFIGURATION, 1)):
        await BulkPipe(host, 1, max_packet=512).write(b"stale")    # 利用側が受け取らず OUT に残る
        await stream_send(dut, "ep1_in", b"stale")                 # 小休止で IN パケットになる
        await control_out(host, setup_packet(0x02, SET_FEATURE, 0, 0x81))
        await control_out(host, request)
        response = await host.in_(1)
        assert response.packet == up.Handshake(up.NAK), (request, response)  # HALT 解除・IN は空
        await BulkPipe(host, 1, max_packet=512).write(b"new")      # DATA0 から受理される
        await RisingEdge(dut.clk)                                   # 収集側の規約: 入力は立ち上がり直後に変える
        dut.ep1_out_ready.value = 1
        await ClockCycles(dut.clk, 20)
        assert bytes(out.items) == b"new", (request, bytes(out.items))  # OUT の古いデータは捨てられた
        dut.ep1_out_ready.value = 0
        out.items.clear()
