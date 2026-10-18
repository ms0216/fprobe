"""frame_mux（フレームのリンクに共通のチャンネルの多重化）のテストベンチ。リンク側の受信バッファ・送信バッファの
受け渡しを直接操作する（COBS・UDP 等の運び方には依らない）。"""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge

from tests.tb.serial_link_tb import collect, feed


async def setup(dut):
    for name in ("ctrl_in_valid", "ctrl_in_last", "dap_in_valid", "dap_in_last", "swo_in_valid", "stream_in_valid",
                 "uart_in_valid", "ctrl_in_data", "dap_in_data", "swo_in_data", "stream_in_data", "uart_in_data",
                 "rx_we", "rx_waddr", "rx_wdata", "rx_commit", "rx_len", "tx_raddr", "tx_done"):
        getattr(dut, name).value = 0
    for name in ("ctrl_out_ready", "dap_out_ready", "uart_out_ready"):
        getattr(dut, name).value = 1
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.rst.value = 1
    await ClockCycles(dut.clk, 3)
    dut.rst.value = 0


async def put_frame(dut, frame: bytes):
    """リンク役: 受信バッファに書いて commit する（busy の間は待つ）。"""
    while True:
        await ReadOnly()
        busy = dut.rx_busy.value == 1
        await RisingEdge(dut.clk)
        if not busy:
            break
    for i, b in enumerate(frame):
        dut.rx_we.value, dut.rx_waddr.value, dut.rx_wdata.value = 1, i, b
        await RisingEdge(dut.clk)
    dut.rx_we.value = 0
    dut.rx_commit.value, dut.rx_len.value = 1, len(frame)
    await RisingEdge(dut.clk)
    dut.rx_commit.value = 0


async def take_frame(dut, timeout=5000) -> bytes:
    """リンク役: 送信バッファのフレームを読んで tx_done を返す。"""
    for _ in range(timeout):
        await ReadOnly()
        if dut.tx_valid.value == 1:
            break
        await RisingEdge(dut.clk)
    else:
        raise TimeoutError("tx_valid")
    n = int(dut.tx_len.value)
    await RisingEdge(dut.clk)
    out = bytearray()
    for i in range(n):
        dut.tx_raddr.value = i
        await RisingEdge(dut.clk)
        await RisingEdge(dut.clk)
        await ReadOnly()
        out.append(int(dut.tx_rdata.value))
        await RisingEdge(dut.clk)
    dut.tx_done.value = 1
    await RisingEdge(dut.clk)
    dut.tx_done.value = 0
    return bytes(out)


@cocotb.test()
async def frames_from_the_link_reach_their_channel(dut):
    await setup(dut)
    task = cocotb.start_soon(collect(dut, "ctrl"))
    await put_frame(dut, b"\x00hello")
    assert await task == b"hello"
    task = cocotb.start_soon(collect(dut, "dap"))
    await put_frame(dut, b"\x01\x00\x01")
    assert await task == b"\x00\x01"
    await put_frame(dut, b"\x05" + (9600).to_bytes(4, "little"))     # LINE_CODING
    await ClockCycles(dut.clk, 30)
    assert int(dut.line_rate.value) == 9600
    await put_frame(dut, b"\x09junk")                                 # 未知のチャンネルは捨てる
    task = cocotb.start_soon(collect(dut, "uart", n=3))
    await put_frame(dut, b"\x03abc")
    assert await task == b"abc"


@cocotb.test()
async def channels_are_framed_by_priority_and_control_ends_with_a_short_frame(dut):
    await setup(dut)
    await feed(dut, "ctrl", bytes(range(256)) * 2)                   # ちょうど 512
    assert await take_frame(dut) == b"\x00" + bytes(range(256)) * 2
    assert await take_frame(dut) == b"\x00"                          # 終わりの印の空フレーム
    await feed(dut, "stream", b"xyz", last=False)
    assert await take_frame(dut) == b"\x04xyz"                       # 途切れたら閉じる
