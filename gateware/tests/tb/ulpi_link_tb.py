"""ulpi_link のテストベンチ（UlpiPhy モデルと接続）。"""

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, FallingEdge, ReadOnly, RisingEdge

from tests.models import usb_packets as up
from tests.models.ulpi_phy import (LINE_J, LINE_K, REG_FUNC_CTRL, REG_SCRATCH, REG_VID_LOW,
                                   UlpiPhy)


async def settle(dut):
    """link が次の立ち上がりで見る値が確定した時点へ進む。

    link は PHY の出力を立ち下がりで取り込み、tx_ready はそれに依存するので、立ち上がり直後ではなく
    立ち下がり後の ReadOnly 相で読む（そこから次の立ち上がりまで値は変わらない）。
    """
    await FallingEdge(dut.clk)
    await ReadOnly()


async def setup(dut, *, throttle=0.0, seed=1) -> UlpiPhy:
    dut.reg_req.value = 0
    dut.reg_we.value = 0
    dut.reg_addr.value = 0
    dut.reg_wdata.value = 0
    dut.tx_valid.value = 0
    dut.tx_data.value = 0
    dut.tx_last.value = 0
    dut.chirp_k.value = 0
    phy = UlpiPhy(dut, dut.clk, rng=random.Random(seed), nxt_throttle=throttle)
    cocotb.start_soon(Clock(dut.clk, 16, unit="ns").start())
    dut.rst.value = 1
    await ClockCycles(dut.clk, 3)
    dut.rst.value = 0
    await wait_for(dut, dut.phy_ready, timeout=500)
    return phy


async def wait_for(dut, signal, *, timeout=2000, value=1):
    for _ in range(timeout):
        await settle(dut)
        if int(signal.value) == value:
            await RisingEdge(dut.clk)
            return
    raise AssertionError(f"timeout waiting for {signal._name}={value}")


async def register(dut, addr, *, write=None) -> int:
    dut.reg_addr.value = addr
    dut.reg_we.value = 1 if write is not None else 0
    dut.reg_wdata.value = write or 0
    dut.reg_req.value = 1
    for _ in range(2000):
        await settle(dut)
        if int(dut.reg_done.value):
            value = int(dut.reg_rdata.value)
            await RisingEdge(dut.clk)
            dut.reg_req.value = 0
            await RisingEdge(dut.clk)        # 4 フェーズ: 要求が一度下がったことを link に見せる
            return value
    raise AssertionError("register access timeout")


async def transmit(dut, packet: bytes, *, timeout=2000, phy=None):
    for i, byte in enumerate(packet):
        dut.tx_valid.value = 1
        dut.tx_data.value = byte
        dut.tx_last.value = int(i == len(packet) - 1)
        for _ in range(timeout):
            await settle(dut)
            accepted = int(dut.tx_ready.value)
            await RisingEdge(dut.clk)
            if accepted:
                break
        else:
            detail = "" if phy is None else (
                f" (phy: transmissions={[(t.pid, len(t.data), t.aborted) for t in phy.transmissions]}, "
                f"cycle={phy.cycle})")
            raise AssertionError(f"transmit stalled at byte {i}{detail}")
    dut.tx_valid.value = 0
    dut.tx_last.value = 0


class RxCollector:
    """rx_active で区切られた受信バイト列を集める。"""

    def __init__(self, dut):
        self.packets: list[bytes] = []
        self._dut = dut
        cocotb.start_soon(self._run())

    async def _run(self):
        current = None
        while True:
            await settle(self._dut)
            active = int(self._dut.rx_active.value)
            if active and current is None:
                current = bytearray()
            if current is not None and int(self._dut.rx_valid.value):
                current.append(int(self._dut.rx_data.value))
            if not active and current is not None:
                self.packets.append(bytes(current))
                current = None
            await RisingEdge(self._dut.clk)


@cocotb.test()
async def becomes_ready_after_phy_startup_and_releases_stp(dut):
    phy = await setup(dut)
    assert phy.ready
    await ReadOnly()
    assert int(dut.stp.value) == 0


@cocotb.test()
async def scratch_write_and_read_back(dut):
    phy = await setup(dut, throttle=0.3)
    await register(dut, REG_SCRATCH, write=0x5A)
    assert phy.regs[REG_SCRATCH] == 0x5A
    assert await register(dut, REG_SCRATCH) == 0x5A


@cocotb.test()
async def reads_vendor_id(dut):
    await setup(dut)
    assert await register(dut, REG_VID_LOW) == 0x24


@cocotb.test()
async def rx_cmd_updates_line_state_and_vbus(dut):
    phy = await setup(dut)
    phy.set_line_state(LINE_K)
    await ClockCycles(dut.clk, 10)
    await ReadOnly()
    assert int(dut.line_state.value) == LINE_K
    assert int(dut.vbus_valid.value) == 1
    await RisingEdge(dut.clk)
    phy.set_line_state(LINE_J)
    await ClockCycles(dut.clk, 10)
    await ReadOnly()
    assert int(dut.line_state.value) == LINE_J


@cocotb.test()
async def receives_packets_with_throttling(dut):
    phy = await setup(dut, throttle=0.4)
    rx = RxCollector(dut)
    packets = [up.token(up.OUT, 3, 1), up.data_packet(up.DATA0, bytes(range(40))),
               up.handshake(up.ACK)]
    for p in packets:
        phy.receive(p)
    await ClockCycles(dut.clk, 400)
    assert rx.packets == packets


@cocotb.test()
async def transmits_packets_with_throttling(dut):
    phy = await setup(dut, throttle=0.4)
    data = up.data_packet(up.DATA1, bytes(range(100)))
    await transmit(dut, data, phy=phy)
    await transmit(dut, up.handshake(up.NAK), phy=phy)
    await ClockCycles(dut.clk, 20)
    sent = [t.packet for t in phy.transmissions]
    assert sent == [data, up.handshake(up.NAK)]
    assert not any(t.aborted for t in phy.transmissions)


@cocotb.test()
async def drives_idle_when_owning_the_bus(dut):
    await setup(dut)
    for _ in range(20):
        await RisingEdge(dut.clk)
        await ReadOnly()
        if int(dut.data_oe.value):
            assert int(dut.data_o.value) == 0
            assert int(dut.stp.value) == 0


@cocotb.test()
async def register_write_preempted_by_dir_is_retried(dut):
    phy = await setup(dut)
    phy.preempt_next_command = True
    phy.receive(up.handshake(up.ACK))
    await register(dut, REG_SCRATCH, write=0xC3)
    assert phy.regs[REG_SCRATCH] == 0xC3


@cocotb.test()
async def transmit_preempted_before_acceptance_is_retried(dut):
    phy = await setup(dut)
    rx = RxCollector(dut)
    phy.preempt_next_command = True
    phy.receive(up.token(up.IN, 0, 0))
    await transmit(dut, up.handshake(up.STALL), phy=phy)
    await ClockCycles(dut.clk, 30)
    assert rx.packets == [up.token(up.IN, 0, 0)]
    assert [t.packet for t in phy.transmissions] == [up.handshake(up.STALL)]


@cocotb.test()
async def transmit_aborted_mid_packet_is_reported(dut):
    phy = await setup(dut)
    aborted = []

    async def watch():
        while True:
            await settle(dut)
            if int(dut.tx_aborted.value):
                aborted.append(phy.cycle)
            await RisingEdge(dut.clk)

    cocotb.start_soon(watch())
    phy.abort_transmit_after = 5
    dut.tx_valid.value = 1
    payload = up.data_packet(up.DATA0, bytes(range(30)))
    index = 0
    for _ in range(200):
        dut.tx_data.value = payload[index]
        dut.tx_last.value = int(index == len(payload) - 1)
        await settle(dut)
        if int(dut.tx_aborted.value):
            break
        ready = int(dut.tx_ready.value)
        await RisingEdge(dut.clk)
        if ready:
            index += 1
    await RisingEdge(dut.clk)
    dut.tx_valid.value = 0
    await ClockCycles(dut.clk, 10)
    assert len(aborted) == 1
    assert phy.transmissions[-1].aborted
    # 中断したパケットの残りが次のパケットに混ざらない
    await transmit(dut, up.handshake(up.NAK), phy=phy)
    await ClockCycles(dut.clk, 20)
    assert [t.packet for t in phy.transmissions[1:]] == [up.handshake(up.NAK)]


@cocotb.test()
async def transmit_underrun_aborts_and_next_packet_is_clean(dut):
    phy = await setup(dut)
    dut.tx_valid.value = 1
    dut.tx_data.value = up.data_packet(up.DATA0, b"")[0]
    dut.tx_last.value = 0
    for _ in range(200):                     # PID だけ渡してデータを途切れさせる
        await settle(dut)
        ready = int(dut.tx_ready.value)
        await RisingEdge(dut.clk)
        if ready:
            break
    dut.tx_valid.value = 0
    await ClockCycles(dut.clk, 20)
    assert len(phy.transmissions) == 1 and phy.transmissions[0].aborted
    await transmit(dut, up.handshake(up.ACK), phy=phy)
    await ClockCycles(dut.clk, 20)
    assert [t.packet for t in phy.transmissions[1:]] == [up.handshake(up.ACK)]


@cocotb.test()
async def chirp_k_is_nopid_zeros_terminated_by_ff(dut):
    phy = await setup(dut)
    dut.chirp_k.value = 1
    await ClockCycles(dut.clk, 50)
    dut.chirp_k.value = 0
    await ClockCycles(dut.clk, 10)
    (chirp,) = phy.transmissions
    assert chirp.pid is None
    assert chirp.aborted                      # 0xFF + STP で終了
    assert len(chirp.data) >= 40 and set(chirp.data) == {0}


@cocotb.test()
async def register_read_data_is_not_decoded_as_rx_cmd(dut):
    phy = await setup(dut)
    phy.regs[REG_FUNC_CTRL] = 0x7F            # RX CMD として読むと LineState=11 / RxActive に見える値
    await ClockCycles(dut.clk, 5)
    before = int(dut.line_state.value)
    assert await register(dut, REG_FUNC_CTRL) == 0x7F
    await ReadOnly()
    assert int(dut.line_state.value) == before
    assert int(dut.rx_active.value) == 0
