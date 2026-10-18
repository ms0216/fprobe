"""usb_cdc_notify（COMM_INTERFACE = 2）のテストベンチ。usb_protocol の代わりにパケットを読み、ack / retry を返す。"""

import cocotb
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge

from tests.tb.common import start_clock_and_reset

HEAD = bytes([0xA1, 0x20, 0, 0, 2, 0, 2, 0])


async def setup(dut, dcd=0, dsr=0):
    dut.dcd.value = dcd
    dut.dsr.value = dsr
    dut.pl_ready.value = 0
    dut.ack.value = 0
    dut.retry.value = 0
    await start_clock_and_reset(dut)


async def read_packet(dut) -> bytes | None:
    """先頭パケットを読む（無ければ None）。ack / retry はしない。"""
    await ReadOnly()
    if int(dut.pkt_ready.value) == 0:
        await RisingEdge(dut.clk)
        return None
    assert int(dut.pkt_empty.value) == 0
    await RisingEdge(dut.clk)
    data = bytearray()
    dut.pl_ready.value = 1
    while True:
        await ReadOnly()
        assert int(dut.pl_valid.value) == 1
        data.append(int(dut.pl_data.value))
        last = int(dut.pl_last.value)
        await RisingEdge(dut.clk)
        if last:
            break
    dut.pl_ready.value = 0
    return bytes(data)


async def pulse(dut, signal):
    signal.value = 1
    await RisingEdge(dut.clk)
    signal.value = 0
    await RisingEdge(dut.clk)


@cocotb.test()
async def sends_state_changes_once_and_retries(dut):
    await setup(dut)
    await ClockCycles(dut.clk, 3)
    assert await read_packet(dut) is None                         # 全部 0 = 送っていない状態と同じ
    dut.dcd.value = 1
    dut.dsr.value = 1
    await ClockCycles(dut.clk, 2)
    assert await read_packet(dut) == HEAD + bytes([0x03, 0])
    await pulse(dut, dut.retry)                                   # 届かなかった: 同じものを最初から
    dut.dsr.value = 0                                             # 送っている間の変化は次のパケットへ
    assert await read_packet(dut) == HEAD + bytes([0x03, 0])
    await pulse(dut, dut.ack)
    await ClockCycles(dut.clk, 2)
    assert await read_packet(dut) == HEAD + bytes([0x01, 0])
    await pulse(dut, dut.ack)
    await ClockCycles(dut.clk, 2)
    assert await read_packet(dut) is None


@cocotb.test()
async def reset_forgets_what_was_sent(dut):
    """バスリセット・構成の変更（rst）の後は、0 でない状態をもう一度知らせる。"""
    await setup(dut, dcd=1, dsr=1)
    await ClockCycles(dut.clk, 2)
    assert await read_packet(dut) == HEAD + bytes([0x03, 0])
    await pulse(dut, dut.ack)
    dut.rst.value = 1
    await ClockCycles(dut.clk, 2)
    dut.rst.value = 0
    await ClockCycles(dut.clk, 2)
    assert await read_packet(dut) == HEAD + bytes([0x03, 0])
