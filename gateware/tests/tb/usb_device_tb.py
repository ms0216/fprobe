"""usb_device 全体（ループバックハーネス経由）のテストベンチ。PHY モデル上のホストで列挙から転送まで行う。"""

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, FallingEdge, ReadOnly, RisingEdge

from fprobe_gw.usb.fprobe_device import CDC_COMM_INTERFACE as CDC_IF
from fprobe_gw.usb.fprobe_device import (EP_CONTROL, EP_DAP, EP_STREAM, EP_SWO, EP_UART_DATA, EP_UART_NOTIFY,
                                         FPROBE_DEVICE, STR_PRODUCT, STR_SERIAL)
from tests.models import usb_packets as up
from tests.models.ulpi_phy import LINE_J, UlpiPhy
from tests.models.usb_host import (HS_RESPONSE_LIMIT, BulkPipe, UsbHost, control_out,
                                   enumerate_like_windows, reset_and_negotiate, setup_packet)

LINE_STABLE = 10        # ハーネスの T_LINE_STABLE
ADDRESS = 7

async def bring_up(dut, *, high_speed=True, throttle=0.2, seed=3):
    dut.stream_on.value = 0
    phy = UlpiPhy(dut, dut.clk, rng=random.Random(seed), nxt_throttle=throttle)
    phy.set_line_state(LINE_J)                   # 接続済み・FS アイドル
    host = UsbHost(phy, dut.clk)
    cocotb.start_soon(Clock(dut.clk, 16, unit="ns").start())
    dut.rst.value = 1
    await ClockCycles(dut.clk, 3)
    dut.rst.value = 0
    while not phy.ready:
        await FallingEdge(dut.clk)
    await ClockCycles(dut.clk, 100)              # PHY 初期設定（OTG / Function Control）の書き込み
    await reset_and_negotiate(host, line_stable=LINE_STABLE, high_speed=high_speed)
    await ClockCycles(dut.clk, 20)
    return host


@cocotb.test()
async def enumerates_at_high_speed_like_windows(dut):
    host = await bring_up(dut)
    got = await enumerate_like_windows(host, address=ADDRESS)
    strings = FPROBE_DEVICE.strings()
    assert got["device"] == FPROBE_DEVICE.device_descriptor()
    assert got["config"] == FPROBE_DEVICE.configuration(high_speed=True)
    assert got["product"] == strings[STR_PRODUCT] and got["serial"] == strings[STR_SERIAL]
    assert got["bos"] == FPROBE_DEVICE.bos() and got["msos20"] == FPROBE_DEVICE.msos20_set()
    assert max(host.latencies) <= HS_RESPONSE_LIMIT, max(host.latencies)
    await ClockCycles(dut.clk, 10)
    await ReadOnly()
    assert int(dut.high_speed.value) == 1 and int(dut.configured.value) == 1


@cocotb.test()
async def control_channel_echoes_messages(dut):
    host = await bring_up(dut)
    await enumerate_like_windows(host, address=ADDRESS)
    out = BulkPipe(host, EP_CONTROL, max_packet=512)
    back = BulkPipe(host, EP_CONTROL, max_packet=512)
    rng = random.Random(1)
    for size in (1, 64, 511, 512):
        message = bytes(rng.randrange(256) for _ in range(size))
        await out.write(message)
        assert await back.read(4096) == message, size


@cocotb.test()
async def uart_channel_survives_crc_errors_and_lost_acks(dut):
    host = await bring_up(dut)
    await enumerate_like_windows(host, address=ADDRESS)
    rng = random.Random(2)
    out = BulkPipe(host, EP_UART_DATA, max_packet=512, rng=rng, corrupt_prob=0.15)
    back = BulkPipe(host, EP_UART_DATA, max_packet=512, rng=rng, drop_ack_prob=0.15)
    data = bytes(rng.randrange(256) for _ in range(4096))
    received = bytearray()
    for i in range(0, len(data), 1024):          # 送受信のバッファ（各 1 KB）に収まる単位で往復
        await out.write(data[i:i + 1024])
        received += await back.read(1024)
    assert bytes(received) == data


@cocotb.test()
async def stream_endpoint_delivers_continuous_counter(dut):
    host = await bring_up(dut)
    await enumerate_like_windows(host, address=ADDRESS)
    await RisingEdge(dut.clk)
    dut.stream_on.value = 1
    data = await BulkPipe(host, EP_STREAM, max_packet=512).read(4096)
    assert len(data) == 4096
    assert all((b - a) % 256 == 1 for a, b in zip(data, data[1:]))


@cocotb.test()
async def cdc_line_coding_reaches_the_gateware(dut):
    host = await bring_up(dut)
    await enumerate_like_windows(host, address=ADDRESS)
    coding = (921_600).to_bytes(4, "little") + bytes([0, 0, 8])
    await control_out(host, setup_packet(0x21, 0x20, 0, CDC_IF, 7), coding)
    await ClockCycles(dut.clk, 10)
    await ReadOnly()
    assert int(dut.line_rate.value) == 921_600


async def notification(host) -> bytes | None:
    """通知のエンドポイントを 1 回読む（NAK なら None）。"""
    r = await host.in_(EP_UART_NOTIFY)
    return r.packet.payload if isinstance(r.packet, up.Data) else None


@cocotb.test()
async def cdc_serial_state_and_control_lines(dut):
    """U52: SERIAL_STATE（DCD = 1、DSR = CTS の状態）を構成の直後と変化のたびに 1 回ずつ送る。RTS・ブレークの要求。"""
    dut.serial_dsr.value = 1
    host = await bring_up(dut)
    await enumerate_like_windows(host, address=ADDRESS)
    head = bytes([0xA1, 0x20, 0, 0, CDC_IF, 0, 2, 0])
    assert await notification(host) == head + bytes([0x03, 0])
    assert await notification(host) is None                        # 変わらなければ送らない
    dut.serial_dsr.value = 0
    await ClockCycles(dut.clk, 4)
    assert await notification(host) == head + bytes([0x01, 0])
    assert await notification(host) is None
    await control_out(host, setup_packet(0x21, 0x22, 0b10, CDC_IF))   # RTS
    await ClockCycles(dut.clk, 4)
    await ReadOnly()
    assert int(dut.uart_rts.value) == 1
    await RisingEdge(dut.clk)
    await control_out(host, setup_packet(0x21, 0x23, 0xFFFF, CDC_IF))  # ブレーク（止めるまで）
    await ClockCycles(dut.clk, 4)
    await ReadOnly()
    assert int(dut.uart_brk.value) == 1
    await RisingEdge(dut.clk)
    await control_out(host, setup_packet(0x21, 0x23, 0, CDC_IF))
    await ClockCycles(dut.clk, 4)
    await ReadOnly()
    assert int(dut.uart_brk.value) == 0


@cocotb.test()
async def falls_back_to_full_speed(dut):
    host = await bring_up(dut, high_speed=False)
    got = await enumerate_like_windows(host, address=ADDRESS)
    assert got["config"] == FPROBE_DEVICE.configuration(high_speed=False)
    out = BulkPipe(host, EP_UART_DATA, max_packet=64)
    back = BulkPipe(host, EP_UART_DATA, max_packet=64)
    data = bytes(range(200))
    await out.write(data)
    assert await back.read(200) == data
    await ReadOnly()
    assert int(dut.high_speed.value) == 0


@cocotb.test()
async def bus_reset_discards_data_of_previous_session(dut):
    # 読まれずに残った応答と、受け取られずに残った要求を、再列挙後のやりとりに持ち込まない。
    # HS からのリセット検出は 3 ms のアイドルを要するため、すぐ検出できる FS で行う
    host = await bring_up(dut, high_speed=False)
    await enumerate_like_windows(host, address=ADDRESS)
    await BulkPipe(host, EP_CONTROL, max_packet=64).write(bytes(640))    # 読まないので IN と OUT に残る
    await reset_and_negotiate(host, line_stable=LINE_STABLE, high_speed=False)
    host.address = 0
    await enumerate_like_windows(host, address=ADDRESS)
    message = bytes(range(1, 51))
    await BulkPipe(host, EP_CONTROL, max_packet=64).write(message)
    assert await BulkPipe(host, EP_CONTROL, max_packet=64).read(4096) == message


async def swo_flush(dut, cycles: int = 20):
    await RisingEdge(dut.clk)
    dut.swo_flush.value = 1
    await ClockCycles(dut.clk, cycles)
    dut.swo_flush.value = 0


@cocotb.test()
async def swo_flush_discards_unread_bytes(dut):
    """B10: ホストが読まずに残った SWO のバイトを、受信の開始（swo_flush）で捨てる。後のバイトはそのまま届く。"""
    from tests.tb.common import stream_send
    host = await bring_up(dut)
    await enumerate_like_windows(host, address=ADDRESS)
    await RisingEdge(dut.clk)
    await stream_send(dut, "swo", [0xAA] * 1500)                 # 読まれずに残る
    await swo_flush(dut)
    fresh = bytes(range(100))
    await RisingEdge(dut.clk)
    await stream_send(dut, "swo", fresh)
    assert await BulkPipe(host, EP_SWO, max_packet=512).read(4096) == fresh


@cocotb.test()
async def swo_flush_while_host_reads_keeps_packets_whole(dut):
    """読み出しの最中（送り途中・ACK を落とされた再送待ち）に捨てても、パケットを壊さず、新しいバイトは欠けずに順に届く。"""
    from tests.tb.common import stream_send
    host = await bring_up(dut)
    await enumerate_like_windows(host, address=ADDRESS)
    pipe = BulkPipe(host, EP_SWO, max_packet=512, rng=random.Random(4), drop_ack_prob=0.3)
    fresh = bytes((i % 0x80) for i in range(1200))
    received = bytearray()

    async def reader():
        while not received.endswith(fresh):
            received.extend(await pipe.read(512))

    task = cocotb.start_soon(reader())
    await RisingEdge(dut.clk)
    await stream_send(dut, "swo", [0xAA] * 3000)
    await swo_flush(dut)
    await RisingEdge(dut.clk)
    await stream_send(dut, "swo", fresh)
    await task
    stale = len(received) - len(fresh)
    assert set(received[:stale]) <= {0xAA}
    assert bytes(received[stale:]) == fresh


@cocotb.test()
async def dap_packets_echo_without_zlp(dut):
    # CMSIS-DAP: 1 パケット = 1 応答。MPS ちょうどでも ZLP を付けない（ホストが空の応答と取り違える）
    from tests.models import usb_packets as up
    host = await bring_up(dut)
    await enumerate_like_windows(host, address=ADDRESS)
    out = BulkPipe(host, EP_DAP, max_packet=512)
    back = BulkPipe(host, EP_DAP, max_packet=512)
    for size in (2, 512, 64):
        command = bytes((i * 3 + size) & 0xFF for i in range(size))
        # BulkPipe.write は MPS ちょうどなら ZLP を足すので、パケットは直接送る
        assert (await host.out(EP_DAP, out.toggle, command)).packet in (up.Handshake(up.ACK),
                                                                         up.Handshake(up.NYET))
        out._flip()
        assert await back.read(size) == command, size
        await ClockCycles(dut.clk, 20)
        assert (await host.in_(EP_DAP)).packet == up.Handshake(up.NAK)     # 続く ZLP は無い
