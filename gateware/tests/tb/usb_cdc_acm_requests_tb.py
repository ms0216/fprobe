"""usb_cdc_acm_requests（COMM_INTERFACE=1、CLK_HZ=10 kHz = 1 ms が 10 周期）のテストベンチ。usb_control_ep の要求ハンドラ I/F を直接駆動する。"""

import cocotb
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge

from tests.tb.common import start_clock_and_reset

DEFAULT_CODING = (115200).to_bytes(4, "little") + bytes([0, 0, 8])


async def setup(dut):
    for name in ("req_valid", "req_type", "req_request", "req_value", "req_index", "req_length",
                 "req_out_valid", "req_out_data", "req_out_done"):
        getattr(dut, name).value = 0
    await start_clock_and_reset(dut)


async def request(dut, rtype, req, value=0, index=1, length=0):
    """要求を出し、ack/stall の応答（と IN データ）を返す。"""
    dut.req_type.value = rtype
    dut.req_request.value = req
    dut.req_value.value = value
    dut.req_index.value = index
    dut.req_length.value = length
    dut.req_valid.value = 1
    await RisingEdge(dut.clk)
    dut.req_valid.value = 0
    for _ in range(10):
        await ReadOnly()
        if int(dut.req_ack.value) or int(dut.req_stall.value):
            result = ("ack" if int(dut.req_ack.value) else "stall",
                      int(dut.req_in_data.value).to_bytes(8, "little")[:int(dut.req_in_length.value)])
            await RisingEdge(dut.clk)
            return result
        await RisingEdge(dut.clk)
    raise AssertionError("no response")


async def out_data(dut, data: bytes):
    for b in data:
        dut.req_out_valid.value = 1
        dut.req_out_data.value = b
        await RisingEdge(dut.clk)
    dut.req_out_valid.value = 0
    dut.req_out_done.value = 1
    await RisingEdge(dut.clk)
    dut.req_out_done.value = 0
    await RisingEdge(dut.clk)


@cocotb.test()
async def default_line_coding_is_115200_8n1(dut):
    await setup(dut)
    assert await request(dut, 0xA1, 0x21, length=7) == ("ack", DEFAULT_CODING)
    await ReadOnly()
    assert int(dut.line_rate.value) == 115200


@cocotb.test()
async def set_line_coding_is_stored_and_reported(dut):
    await setup(dut)
    coding = (3_000_000).to_bytes(4, "little") + bytes([2, 2, 7])
    assert (await request(dut, 0x21, 0x20, length=7))[0] == "ack"
    await out_data(dut, coding)
    await ReadOnly()
    assert int(dut.line_rate.value) == 3_000_000
    assert (int(dut.line_stop_bits.value), int(dut.line_parity.value), int(dut.line_data_bits.value)) == (2, 2, 7)
    await RisingEdge(dut.clk)
    assert await request(dut, 0xA1, 0x21, length=7) == ("ack", coding)


@cocotb.test()
async def control_line_state_sets_dtr_rts(dut):
    await setup(dut)
    assert (await request(dut, 0x21, 0x22, value=0b11))[0] == "ack"
    await ReadOnly()
    assert (int(dut.dtr.value), int(dut.rts.value)) == (1, 1)
    await RisingEdge(dut.clk)
    assert (await request(dut, 0x21, 0x22, value=0b01))[0] == "ack"
    await ReadOnly()
    assert (int(dut.dtr.value), int(dut.rts.value)) == (1, 0)


@cocotb.test()
async def send_break_holds_for_the_duration_or_until_cleared(dut):
    await setup(dut)
    assert int(dut.brk.value) == 0
    assert (await request(dut, 0x21, 0x23, value=0xFFFF))[0] == "ack"          # 止めるまで（Windows・Linux の形）
    await ClockCycles(dut.clk, 500)
    assert int(dut.brk.value) == 1
    dut.req_type.value, dut.req_request.value, dut.req_value.value = 0x21, 0x23, 0      # 止める（受理の周期を直接見る）
    dut.req_index.value, dut.req_length.value, dut.req_valid.value = 1, 0, 1
    await RisingEdge(dut.clk)                       # 受理: 残りの長さが 0 に
    dut.req_valid.value = 0
    await ReadOnly()
    assert int(dut.req_ack.value) == 1
    assert int(dut.brk.value) == 1                  # brk はレジスタの出力: 残りの長さが 0 になった次の周期に 0
    await RisingEdge(dut.clk)                       # （組み合わせの論理を別のクロックの同期化の前に置かない。R24）
    await ReadOnly()
    assert int(dut.brk.value) == 0
    await RisingEdge(dut.clk)
    assert (await request(dut, 0x21, 0x23, value=3))[0] == "ack"               # 3 ms = 30 周期
    high = 0
    for _ in range(60):
        await RisingEdge(dut.clk)
        await ReadOnly()
        high += int(dut.brk.value)
    assert 27 <= high <= 30                                                     # 受理から 30 周期（数え始めは 1〜2 周期後）


@cocotb.test()
async def other_interfaces_and_requests_stall(dut):
    await setup(dut)
    assert (await request(dut, 0x21, 0x20, index=0, length=7))[0] == "stall"   # 別の IF
    assert (await request(dut, 0x21, 0x7F))[0] == "stall"                       # 未対応
    assert (await request(dut, 0x40, 0x01))[0] == "stall"                       # ベンダー要求
