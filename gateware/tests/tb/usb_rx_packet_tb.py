"""usb_rx_packet のテストベンチ。ulpi_link の受信側出力（rx_active/rx_valid/rx_data/rx_error）を直接駆動する。"""

import random

import cocotb
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge

from tests.models import usb_packets as up
from tests.tb.common import start_clock_and_reset


class Events:
    """DUT の出力イベントを記録する。"""

    def __init__(self, dut):
        self.tokens, self.sofs, self.handshakes, self.data = [], [], [], []
        self._dut = dut
        self._payload = None
        cocotb.start_soon(self._run())

    async def _run(self):
        d = self._dut
        while True:
            await RisingEdge(d.clk)
            await ReadOnly()
            if int(d.token_valid.value):
                self.tokens.append(up.Token(int(d.token_pid.value), int(d.token_addr.value),
                                            int(d.token_endp.value)))
            if int(d.sof_valid.value):
                self.sofs.append(int(d.sof_frame.value))
            if int(d.handshake_valid.value):
                self.handshakes.append(int(d.handshake_pid.value))
            if int(d.data_start.value):
                self._payload = (int(d.data_pid.value), bytearray())
            if int(d.data_valid.value):
                self._payload[1].append(int(d.data_byte.value))
            if int(d.data_end.value):
                pid, payload = self._payload
                self.data.append((pid, bytes(payload), bool(int(d.data_ok.value)),
                                  int(d.data_length.value)))
                self._payload = None


async def setup(dut):
    dut.rx_active.value = 0
    dut.rx_valid.value = 0
    dut.rx_data.value = 0
    dut.rx_error.value = 0
    await start_clock_and_reset(dut)
    return Events(dut)


async def send(dut, packet: bytes, *, rng=None, error_at=None):
    """ulpi_link と同じ形で 1 パケットを流す（バイト間に間隔を空けることがある）。"""
    rng = rng or random.Random(0)
    dut.rx_active.value = 1
    await RisingEdge(dut.clk)
    for i, byte in enumerate(packet):
        if error_at is not None and i == error_at:
            dut.rx_error.value = 1
            await RisingEdge(dut.clk)
            break
        for _ in range(rng.choice([0, 0, 0, 1, 2])):
            await RisingEdge(dut.clk)
        dut.rx_valid.value = 1
        dut.rx_data.value = byte
        await RisingEdge(dut.clk)
        dut.rx_valid.value = 0
    await RisingEdge(dut.clk)
    dut.rx_active.value = 0
    dut.rx_error.value = 0
    await ClockCycles(dut.clk, 3)


@cocotb.test()
async def decodes_tokens(dut):
    ev = await setup(dut)
    for pid, addr, endp in [(up.SETUP, 0, 0), (up.IN, 0x15, 0xE), (up.OUT, 127, 15), (up.PING, 5, 2)]:
        await send(dut, up.token(pid, addr, endp))
    assert ev.tokens == [up.Token(up.SETUP, 0, 0), up.Token(up.IN, 0x15, 0xE),
                         up.Token(up.OUT, 127, 15), up.Token(up.PING, 5, 2)]


@cocotb.test()
async def decodes_sof(dut):
    ev = await setup(dut)
    await send(dut, up.sof(0x710))
    await send(dut, up.sof(0))
    assert ev.sofs == [0x710, 0] and ev.tokens == []


@cocotb.test()
async def rejects_bad_tokens(dut):
    ev = await setup(dut)
    good = up.token(up.IN, 3, 1)
    await send(dut, bytes([good[0], good[1], good[2] ^ 0x08]))      # CRC5 誤り
    await send(dut, bytes([good[0] ^ 0x10, good[1], good[2]]))      # PID チェック誤り
    await send(dut, good + b"\x00")                                 # 長すぎる
    await send(dut, good[:2])                                       # 短すぎる
    assert ev.tokens == [] and ev.data == [] and ev.handshakes == []


@cocotb.test()
async def streams_data_payload_without_crc(dut):
    ev = await setup(dut)
    rng = random.Random(3)
    payloads = [b"", bytes(range(8)), bytes(rng.randrange(256) for _ in range(64)),
                bytes(rng.randrange(256) for _ in range(512))]
    for i, payload in enumerate(payloads):
        await send(dut, up.data_packet(up.DATA1 if i % 2 else up.DATA0, payload), rng=rng)
    assert [(p, d, ok, n) for p, d, ok, n in ev.data] == [
        (up.DATA1 if i % 2 else up.DATA0, payload, True, len(payload))
        for i, payload in enumerate(payloads)]


@cocotb.test()
async def flags_crc16_errors(dut):
    ev = await setup(dut)
    packet = bytearray(up.data_packet(up.DATA0, bytes(range(20))))
    packet[5] ^= 0x01
    await send(dut, bytes(packet))
    (pid, _, ok, _), = ev.data
    assert pid == up.DATA0 and not ok


@cocotb.test()
async def rx_error_marks_data_bad(dut):
    ev = await setup(dut)
    await send(dut, up.data_packet(up.DATA0, bytes(range(20))), error_at=10)
    (_, _, ok, _), = ev.data
    assert not ok


@cocotb.test()
async def decodes_handshakes(dut):
    ev = await setup(dut)
    for pid in (up.ACK, up.NAK, up.STALL, up.NYET):
        await send(dut, up.handshake(pid))
    await send(dut, up.handshake(up.ACK) + b"\x00")                 # 長すぎるハンドシェイクは無視
    assert ev.handshakes == [up.ACK, up.NAK, up.STALL, up.NYET]


@cocotb.test()
async def reports_end_of_every_packet(dut):
    await setup(dut)
    ends = []

    async def watch():
        while True:
            await RisingEdge(dut.clk)
            await ReadOnly()
            ends.append(int(dut.packet_end.value))

    cocotb.start_soon(watch())
    await send(dut, up.token(up.IN, 1, 1))
    await send(dut, up.data_packet(up.DATA0, b"abc"))
    await send(dut, up.handshake(up.ACK))
    assert sum(ends) == 3
