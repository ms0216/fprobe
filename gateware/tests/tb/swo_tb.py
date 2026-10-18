"""CMSIS-DAP の SWO コマンド（dap_processor）と swo_receiver のテストベンチ（dap_harness、clk 100 MHz）。
pyOCD が送る順（Transport → Mode → Baudrate → Control）で設定し、SWO ピンに UART・Manchester のバイトを流す。
HAS_SWO_MANCHESTER = false の構成（test_dap_processor の test_swo_without_manchester）でも同じモジュールを走らせ、
DAP_Info の Capabilities の bit3（SWO Manchester）で構成を見分ける。"""

import struct

import cocotb
from cocotb.triggers import ReadOnly, RisingEdge, Timer

from fprobe_gw import dap_info
from tests.models.swo import manchester_halves, segments
from tests.models.uart import uart_bits
from tests.tb.dap_processor_tb import setup

SWO_TRANSPORT, SWO_MODE, SWO_BAUDRATE, SWO_CONTROL, SWO_STATUS, SWO_DATA = 0x17, 0x18, 0x19, 0x1A, 0x1B, 0x1C
EP, UART, MANCHESTER = 2, 1, 2


async def send_line(dut, data: bytes, baud: int):
    bit_ns = 1e9 / baud
    for byte in data:
        for bit in uart_bits(byte):
            dut.swo.value = bit
            await Timer(round(bit_ns), unit="ns")


async def collect(dut, out: list):
    while True:
        await RisingEdge(dut.clk)
        await ReadOnly()
        if dut.swo_valid.value == 1 and dut.swo_ready.value == 1:
            out.append(int(dut.swo_data.value))


async def has_manchester(dap) -> bool:
    return bool((await dap.command(bytes([0x00, 0xF0])))[2] & dap_info.CAPABILITY_SWO_MANCHESTER)


@cocotb.test()
async def info_advertises_swo(dut):
    dap, _ = await setup(dut)
    manchester = await has_manchester(dap)
    assert await dap.command(bytes([0x00, 0xF0])) == bytes([0x00, 1, dap_info.capabilities(jtag=True,
                                                                                            swo_manchester=manchester)])
    assert dap_info.CAPABILITIES & 0x44 == 0x44                       # SWO UART + ストリーム
    assert await dap.command(bytes([0x00, 0xFD])) == bytes([0x00, 4]) + struct.pack("<I", 2048)


@cocotb.test()
async def configure_start_receive_and_status(dut):
    dap, _ = await setup(dut)
    dut.swo.value = 1
    received = []
    cocotb.start_soon(collect(dut, received))
    assert await dap.command(bytes([SWO_TRANSPORT, 1])) == bytes([SWO_TRANSPORT, 0xFF])   # DAP_SWO_Data は無い
    assert await dap.command(bytes([SWO_TRANSPORT, EP])) == bytes([SWO_TRANSPORT, 0])
    assert await dap.command(bytes([SWO_MODE, 3])) == bytes([SWO_MODE, 0xFF])             # 方式 3 は無い
    assert await dap.command(bytes([SWO_MODE, UART])) == bytes([SWO_MODE, 0])
    r = await dap.command(bytes([SWO_BAUDRATE]) + struct.pack("<I", 2_000_000))
    assert struct.unpack("<I", r[1:5])[0] == 2_000_000                                  # 100 MHz / 50
    r = await dap.command(bytes([SWO_BAUDRATE]) + struct.pack("<I", 3_000_000))
    assert struct.unpack("<I", r[1:5])[0] == 100_000_000 // 33                           # 最も近い分周
    await dap.command(bytes([SWO_BAUDRATE]) + struct.pack("<I", 2_000_000))
    await send_line(dut, b"\x55", 2_000_000)                                            # 停止中は捨てる
    assert await dap.command(bytes([SWO_CONTROL, 1])) == bytes([SWO_CONTROL, 0])
    assert await dap.command(bytes([SWO_BAUDRATE]) + struct.pack("<I", 1_000_000)) == bytes([SWO_BAUDRATE, 0, 0, 0, 0])
    message = bytes(range(0x01, 0x40)) + b"\x00\xff"
    await send_line(dut, message, 2_000_000)
    await Timer(10, unit="us")
    assert bytes(received) == message
    assert await dap.command(bytes([SWO_STATUS])) == bytes([SWO_STATUS, 0x01, 0, 0, 0, 0])
    assert await dap.command(bytes([SWO_DATA, 0x00, 0x02])) == bytes([SWO_DATA, 0x01, 0, 0])
    # 止まっていない下流 → 取りこぼしを申告
    dut.swo_ready.value = 0
    await send_line(dut, b"\x12", 2_000_000)
    await Timer(5, unit="us")
    assert (await dap.command(bytes([SWO_STATUS])))[1] == 0x81
    dut.swo_ready.value = 1
    assert await dap.command(bytes([SWO_CONTROL, 0])) == bytes([SWO_CONTROL, 0])
    assert (await dap.command(bytes([SWO_STATUS])))[1] == 0x80                          # 停止しても理由は残る
    await dap.command(bytes([SWO_CONTROL, 1]))
    assert (await dap.command(bytes([SWO_STATUS])))[1] == 0x01                          # 開始で消える


@cocotb.test()
async def mode_change_while_running_stops_and_allows_a_new_baudrate(dut):
    """pyOCD は設定し直すとき、止める（Control 0）代わりに Mode OFF・Transport なしを送る（参照実装の SWO_Mode は
    受信を止める）。受信中でも Mode を受け、止めて、新しい速さを設定できること（2026-10-18 に実機で、2 回目の
    swo_start の速さが黙って効かず、最初の 4 Mbaud のまま受けていた）。"""
    dap, _ = await setup(dut)
    dut.swo.value = 1
    received = []
    cocotb.start_soon(collect(dut, received))
    for command in ([SWO_TRANSPORT, EP], [SWO_MODE, UART]):
        await dap.command(bytes(command))
    await dap.command(bytes([SWO_BAUDRATE]) + struct.pack("<I", 4_000_000))
    await dap.command(bytes([SWO_CONTROL, 1]))
    assert await dap.command(bytes([SWO_MODE, 0])) == bytes([SWO_MODE, 0])                # 受信中でも受ける
    assert (await dap.command(bytes([SWO_STATUS])))[1] & 0x01 == 0                         # 止まった
    assert await dap.command(bytes([SWO_TRANSPORT, 0])) == bytes([SWO_TRANSPORT, 0])
    assert await dap.command(bytes([SWO_TRANSPORT, EP])) == bytes([SWO_TRANSPORT, 0])
    assert await dap.command(bytes([SWO_MODE, UART])) == bytes([SWO_MODE, 0])
    r = await dap.command(bytes([SWO_BAUDRATE]) + struct.pack("<I", 2_000_000))
    assert struct.unpack("<I", r[1:5])[0] == 2_000_000
    await dap.command(bytes([SWO_CONTROL, 1]))
    await send_line(dut, b"\x5a\xa5", 2_000_000)
    await Timer(10, unit="us")
    assert bytes(received) == b"\x5a\xa5"


async def send_manchester(dut, data: bytes, baud: int):
    for level, length in segments(manchester_halves(data), 1e9 / baud / 2):
        dut.swo.value = level
        await Timer(round(length * 1000), unit="ps")


@cocotb.test()
async def manchester_mode(dut):
    """DAP_SWO_Mode = 2（Manchester）。回路が無い構成では断り（0xFF）、Capabilities にも出さない。
    UART 方式に戻すと同じ線を NRZ で受ける。"""
    dap, _ = await setup(dut)
    dut.swo.value = 0                                                                  # Manchester の休止は Low
    received = []
    cocotb.start_soon(collect(dut, received))
    await dap.command(bytes([SWO_TRANSPORT, EP]))
    if not await has_manchester(dap):
        assert await dap.command(bytes([SWO_MODE, MANCHESTER])) == bytes([SWO_MODE, 0xFF])
        return
    assert await dap.command(bytes([SWO_MODE, MANCHESTER])) == bytes([SWO_MODE, 0])
    r = await dap.command(bytes([SWO_BAUDRATE]) + struct.pack("<I", 12_500_000))
    assert struct.unpack("<I", r[1:5])[0] == 12_500_000                                  # 100 MHz / 8
    assert await dap.command(bytes([SWO_CONTROL, 1])) == bytes([SWO_CONTROL, 0])
    message = bytes(range(0x00, 0x20)) + bytes([0xff])
    await send_manchester(dut, message[:5], 12_500_000)                                # フレームの長さは任意
    await send_manchester(dut, message[5:], 12_500_000)
    await Timer(5, unit="us")
    assert bytes(received) == message
    assert await dap.command(bytes([SWO_STATUS])) == bytes([SWO_STATUS, 0x01, 0, 0, 0, 0])
    # UART 方式に戻す（Mode は受信を止める）
    assert await dap.command(bytes([SWO_MODE, UART])) == bytes([SWO_MODE, 0])
    assert (await dap.command(bytes([SWO_STATUS])))[1] & 0x01 == 0
    await dap.command(bytes([SWO_BAUDRATE]) + struct.pack("<I", 2_000_000))
    dut.swo.value = 1
    await Timer(2, unit="us")
    await dap.command(bytes([SWO_CONTROL, 1]))
    received.clear()
    await send_line(dut, b"Z", 2_000_000)
    await Timer(2, unit="us")
    assert bytes(received) == b"Z"
