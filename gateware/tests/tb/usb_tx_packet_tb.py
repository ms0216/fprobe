"""usb_tx_packet のテストベンチ。テストベンチが ulpi_link（tx_ready をランダムに出す）と
ペイロード供給元の両方を演じる。"""

import random

import cocotb
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge

from tests.models import usb_packets as up
from tests.tb.common import start_clock_and_reset


class FakeLink:
    """tx_valid/tx_ready/tx_data/tx_last を受け、パケット単位で記録する。"""

    def __init__(self, dut, rng, *, ready_prob=0.6):
        self.packets: list[bytes] = []
        self.abort_after: int | None = None
        self._dut, self._rng, self._p = dut, rng, ready_prob
        cocotb.start_soon(self._run())

    async def _run(self):
        d = self._dut
        current = bytearray()
        while True:
            ready = int(self._rng.random() < self._p)
            d.tx_ready.value = ready
            d.tx_aborted.value = 0
            if self.abort_after is not None and len(current) >= self.abort_after:
                self.abort_after = None
                d.tx_ready.value = 0
                d.tx_aborted.value = 1
                current = bytearray()
                await RisingEdge(d.clk)
                continue
            await ReadOnly()
            if ready and int(d.tx_valid.value):
                current.append(int(d.tx_data.value))
                if int(d.tx_last.value):
                    self.packets.append(bytes(current))
                    current = bytearray()
            await RisingEdge(d.clk)


class PayloadSource:
    """pl_valid/pl_ready/pl_data/pl_last で 1 パケット分のペイロードを常に供給可能にする。"""

    def __init__(self, dut):
        self._dut = dut
        self.payload = b""
        self.index = 0
        cocotb.start_soon(self._run())

    def load(self, payload: bytes):
        self.payload, self.index = payload, 0

    async def _run(self):
        d = self._dut
        while True:
            have = self.index < len(self.payload)
            d.pl_valid.value = int(have)
            d.pl_data.value = self.payload[self.index] if have else 0
            d.pl_last.value = int(have and self.index == len(self.payload) - 1)
            await ReadOnly()
            took = have and int(d.pl_ready.value)
            await RisingEdge(d.clk)
            if took:
                self.index += 1


async def setup(dut, seed=1):
    dut.hs_send.value = 0
    dut.hs_pid.value = 0
    dut.data_send.value = 0
    dut.data_pid.value = 0
    dut.data_empty.value = 0
    rng = random.Random(seed)
    await start_clock_and_reset(dut)
    return FakeLink(dut, rng), PayloadSource(dut)


async def wait_done(dut, *, timeout=5000):
    for _ in range(timeout):
        await RisingEdge(dut.clk)
        await ReadOnly()
        if int(dut.done.value) or int(dut.aborted.value):
            result = "done" if int(dut.done.value) else "aborted"
            await RisingEdge(dut.clk)
            return result
    raise AssertionError("no done/aborted")


async def send_handshake(dut, pid):
    dut.hs_pid.value = pid
    dut.hs_send.value = 1
    await RisingEdge(dut.clk)
    dut.hs_send.value = 0
    return await wait_done(dut)


async def send_data(dut, source, pid, payload):
    source.load(payload)
    dut.data_pid.value = pid
    dut.data_empty.value = int(len(payload) == 0)
    dut.data_send.value = 1
    await RisingEdge(dut.clk)
    dut.data_send.value = 0
    return await wait_done(dut)


@cocotb.test()
async def sends_handshakes(dut):
    link, _ = await setup(dut)
    for pid in (up.ACK, up.NAK, up.STALL, up.NYET):
        assert await send_handshake(dut, pid) == "done"
    assert link.packets == [up.handshake(p) for p in (up.ACK, up.NAK, up.STALL, up.NYET)]


@cocotb.test()
async def sends_data_packets_with_crc(dut):
    link, source = await setup(dut)
    rng = random.Random(9)
    payloads = [b"", b"\x01", bytes(range(64)), bytes(rng.randrange(256) for _ in range(512))]
    for i, payload in enumerate(payloads):
        pid = up.DATA1 if i % 2 else up.DATA0
        assert await send_data(dut, source, pid, payload) == "done"
    assert link.packets == [up.data_packet(up.DATA1 if i % 2 else up.DATA0, p)
                            for i, p in enumerate(payloads)]


@cocotb.test()
async def reports_abort_and_recovers(dut):
    link, source = await setup(dut)
    link.abort_after = 10
    assert await send_data(dut, source, up.DATA0, bytes(range(100))) == "aborted"
    await ClockCycles(dut.clk, 5)
    assert await send_data(dut, source, up.DATA1, b"ok") == "done"
    assert link.packets == [up.data_packet(up.DATA1, b"ok")]


@cocotb.test()
async def busy_while_sending(dut):
    _, source = await setup(dut)
    source.load(bytes(range(32)))
    dut.data_pid.value = up.DATA0
    dut.data_send.value = 1
    await RisingEdge(dut.clk)
    dut.data_send.value = 0
    await ReadOnly()
    assert int(dut.busy.value) == 1
    await RisingEdge(dut.clk)
    assert await wait_done(dut) == "done"
    await ReadOnly()
    assert int(dut.busy.value) == 0
