"""bringup_core のテストベンチ。

generics: SYS_CLK_HZ=10_000_000, BAUD=100_000, REVISION=3714, ECHO_FIFO_DEPTH=8
→ 受信分周比 100、送信分周比 99（公称より約 1% 速い）。
"""

import cocotb
from cocotb.triggers import ClockCycles, ReadOnly

from tests.models.uart import decode_uart_line, rx_divisor, tx_divisor, uart_bits
from tests.tb.common import LevelRecorder, start_clock_and_reset

SYS_CLK_HZ, BAUD = 10_000_000, 100_000
RX_DIV = rx_divisor(SYS_CLK_HZ, BAUD)
TX_DIV = tx_divisor(SYS_CLK_HZ, BAUD)
BANNER = b"fprobe-bringup sync_hz=10000000 rev=3714\r\n"


async def converse(dut, send: bytes, *, wait_bytes: int, host_div: int = RX_DIV) -> bytes:
    """ホストが分周比 host_div で隙間なく送り、FPGA の送信を TX_DIV で復号する。"""
    dut.rx.value = 1
    await start_clock_and_reset(dut)
    line = LevelRecorder(dut, dut.tx)
    for byte in send:
        for level in uart_bits(byte):
            dut.rx.value = level
            await ClockCycles(dut.clk, host_div)
    await ClockCycles(dut.clk, TX_DIV * 10 * wait_bytes)
    return decode_uart_line(line.levels, TX_DIV)


def plain_payload(n_rounds: int) -> bytes:
    return bytes(b for b in range(32, 127) if b != ord("?")) * n_rounds  # '?' はバナー要求


@cocotb.test()
async def tx_is_faster_than_nominal(dut):
    assert TX_DIV < RX_DIV


@cocotb.test()
async def echoes_bytes(dut):
    assert await converse(dut, b"hi", wait_bytes=3) == b"hi"


@cocotb.test()
async def question_mark_reports_frequency_and_revision(dut):
    assert await converse(dut, b"?", wait_bytes=len(BANNER) + 2) == BANNER


@cocotb.test()
async def echo_continues_after_banner(dut):
    assert await converse(dut, b"?x", wait_bytes=len(BANNER) + 3) == BANNER + b"x"


@cocotb.test()
async def banner_while_echo_traffic_continues(dut):
    payload = b"?" + bytes(range(65, 65 + 6))      # バナー送出中に届く分が FIFO（8 段）に収まる量
    reply = await converse(dut, payload, wait_bytes=len(BANNER) + 4)
    assert reply == BANNER + payload[1:]


@cocotb.test()
async def sustained_echo_at_nominal_host_rate_is_lossless(dut):
    payload = plain_payload(3)
    assert await converse(dut, payload, wait_bytes=4) == payload
    await ReadOnly()
    assert int(dut.rx_error.value) == 0


@cocotb.test()
async def sustained_echo_is_lossless_while_host_is_no_faster_than_tx(dut):
    # 公称より約 1% 速いホストでも、送信がそれ以上速ければ FIFO は溜まらない
    payload = plain_payload(3)
    assert await converse(dut, payload, wait_bytes=4, host_div=TX_DIV) == payload
    await ReadOnly()
    assert int(dut.rx_error.value) == 0


@cocotb.test()
async def loss_is_flagged_when_host_outpaces_tx(dut):
    # 送信より 3% 速いホストが送り続けると、いずれ FIFO（8 段）が溢れる。黙って捨てずにエラーを立てる
    payload = plain_payload(6)
    reply = await converse(dut, payload, wait_bytes=20, host_div=RX_DIV - 4)
    assert reply != payload
    await ReadOnly()
    assert int(dut.rx_error.value) == 1
