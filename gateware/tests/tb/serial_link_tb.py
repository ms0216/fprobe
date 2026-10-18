"""serial_link（UART のホストリンク）のテストベンチ。CLK_HZ = 8 MHz、BAUD = 1 Mbaud（1 ビット = 8 周期）。
フレームの符号化（COBS + ch + CRC16）はここで独立に書く（ホスト側の実装に依らない）。"""

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge

from tests.models.uart import decode_uart_line, uart_bits

DIVISOR = 8


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = (crc << 1) ^ 0x1021 if crc & 0x8000 else crc << 1
            crc &= 0xFFFF
    return crc


def cobs_encode(data: bytes) -> bytes:
    out, block = bytearray(), bytearray()
    for b in data:
        if b == 0:
            out += bytes([len(block) + 1]) + block
            block.clear()
        else:
            block.append(b)
            if len(block) == 254:
                out += bytes([255]) + block
                block.clear()
    out += bytes([len(block) + 1]) + block
    return bytes(out)


def cobs_decode(data: bytes) -> bytes:
    out, i = bytearray(), 0
    while i < len(data):
        code = data[i]
        out += data[i + 1:i + code]
        i += code
        if code < 255 and i < len(data):
            out.append(0)
    return bytes(out)


def frame(ch: int, payload: bytes, corrupt: bool = False) -> bytes:
    body = bytes([ch]) + payload
    crc = crc16(body) ^ (1 if corrupt else 0)
    return b"\x00" + cobs_encode(body + crc.to_bytes(2, "little")) + b"\x00"


def parse_frames(line: bytes) -> list[tuple[int, bytes]]:
    out = []
    for chunk in line.split(b"\x00"):
        if not chunk:
            continue
        raw = cobs_decode(chunk)
        assert crc16(raw[:-2]) == int.from_bytes(raw[-2:], "little"), raw.hex()
        out.append((raw[0], raw[1:-2]))
    return out


class Link:
    def __init__(self, dut):
        self.dut = dut
        self.tx_levels: list[int] = []

    async def send(self, data: bytes):
        for b in data:
            for level in uart_bits(b):
                self.dut.rx.value = level
                await ClockCycles(self.dut.clk, DIVISOR)

    async def watch_tx(self):
        while True:
            await RisingEdge(self.dut.clk)
            self.tx_levels.append(int(self.dut.tx.value))

    def received(self) -> bytes:
        return decode_uart_line(self.tx_levels, DIVISOR)


async def collect(dut, prefix, n=None, *, timeout=200_000):
    """コア側の出力ストリーム（prefix_out_*）から、last まで（または n バイト）受け取る。"""
    valid, data = getattr(dut, f"{prefix}_out_valid"), getattr(dut, f"{prefix}_out_data")
    ready = getattr(dut, f"{prefix}_out_ready")
    last = getattr(dut, f"{prefix}_out_last", None)
    out = bytearray()
    for _ in range(timeout):
        await ReadOnly()                                               # 立ち上がりの前の値で受け渡しを数える
        if valid.value == 1 and ready.value == 1:
            out.append(int(data.value))
            if (last is not None and last.value == 1) or (n is not None and len(out) == n):
                await RisingEdge(dut.clk)
                return bytes(out)
        await RisingEdge(dut.clk)
    raise TimeoutError(prefix)


async def setup(dut):
    for name in ("ctrl_in_valid", "ctrl_in_last", "dap_in_valid", "dap_in_last", "swo_in_valid", "stream_in_valid",
                 "uart_in_valid", "ctrl_in_data", "dap_in_data", "swo_in_data", "stream_in_data", "uart_in_data"):
        getattr(dut, name).value = 0
    for name in ("ctrl_out_ready", "dap_out_ready", "uart_out_ready"):
        getattr(dut, name).value = 1
    dut.rx.value = 1
    cocotb.start_soon(Clock(dut.clk, 125, unit="ns").start())
    dut.rst.value = 1
    await ClockCycles(dut.clk, 3)
    dut.rst.value = 0
    link = Link(dut)
    cocotb.start_soon(link.watch_tx())
    return link


async def feed(dut, prefix, data: bytes, *, last=True):
    valid, rdy = getattr(dut, f"{prefix}_in_valid"), getattr(dut, f"{prefix}_in_ready")
    lastsig = getattr(dut, f"{prefix}_in_last", None)
    for i, b in enumerate(data):
        valid.value = 1
        getattr(dut, f"{prefix}_in_data").value = b
        if lastsig is not None:
            lastsig.value = int(last and i == len(data) - 1)
        while True:
            await ReadOnly()
            accepted = rdy.value == 1
            await RisingEdge(dut.clk)
            if accepted:
                break
    valid.value = 0


@cocotb.test()
async def packets_from_the_host_reach_their_channel(dut):
    link = await setup(dut)
    payload = bytes(random.Random(1).choice([0, 1, 0x55, 0xFF]) for _ in range(300))   # 0 と 254 超えを含む
    task = cocotb.start_soon(collect(dut, "ctrl"))
    await link.send(frame(0, payload))
    assert await task == payload
    await RisingEdge(dut.clk)                                          # 読み取り専用の相を抜けてから次を送る
    task = cocotb.start_soon(collect(dut, "dap"))
    await link.send(frame(1, b"\x00\x01"))
    assert await task == b"\x00\x01"


@cocotb.test()
async def corrupted_frames_are_dropped_and_counted(dut):
    link = await setup(dut)
    task = cocotb.start_soon(collect(dut, "ctrl"))
    await link.send(frame(0, b"bad", corrupt=True) + frame(0, b"good"))
    assert await task == b"good"
    assert int(dut.bad_frames.value) == 1


@cocotb.test()
async def line_coding_sets_the_target_baud(dut):
    link = await setup(dut)
    await link.send(frame(5, (115_200).to_bytes(4, "little")))
    await ClockCycles(dut.clk, 20)
    assert int(dut.line_rate.value) == 115_200


@cocotb.test()
async def responses_and_streams_go_out_as_frames(dut):
    link = await setup(dut)
    response = bytes(random.Random(2).choice([0, 7, 0xAA]) for _ in range(260))
    await feed(dut, "ctrl", response)
    stream = bytes(random.Random(3).randrange(256) for _ in range(600))
    await feed(dut, "stream", stream, last=False)
    await ClockCycles(dut.clk, DIVISOR * 10 * 1600)                 # 送り切るまで
    frames = parse_frames(link.received())
    assert frames[0] == (0, response)
    assert all(ch == 4 and len(p) <= 512 for ch, p in frames[1:])
    assert b"".join(p for _, p in frames[1:]) == stream


@cocotb.test()
async def a_control_response_of_exactly_512_bytes_ends_with_an_empty_frame(dut):
    link = await setup(dut)
    response = bytes(range(256)) * 2
    await feed(dut, "ctrl", response)
    await ClockCycles(dut.clk, DIVISOR * 10 * 560)
    assert parse_frames(link.received()) == [(0, response), (0, b"")]


@cocotb.test()
async def uart_bytes_from_the_host_are_buffered(dut):
    # ターゲットの UART が受け取らない間も、FIFO に溜めて取りこぼさない
    link = await setup(dut)
    dut.uart_out_ready.value = 0
    data = bytes(random.Random(4).randrange(256) for _ in range(300))
    await link.send(frame(3, data))
    await ClockCycles(dut.clk, 50)
    dut.uart_out_ready.value = 1
    assert await collect(dut, "uart", n=300) == data
