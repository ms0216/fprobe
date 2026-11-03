"""CSR events の written（下位）/ written_hi（上位）: 書いたイベントの数が 2**32 を超えても化けないこと。
EVENT_COUNT_START_BELOW_4G = 3 なので、記録を始めた周期の PHY の設定（種類 16）1 件と UART のバイト 3 件で 2**32 をまたぐ。"""
import cocotb
from cocotb.triggers import ClockCycles

from fprobe import generated as g
from tests.tb.common import stream_send
from tests.tb.fprobe_core_tb import setup


async def events_words(port):
    """written, lost, capacity, written_hi を 1 つの連続読み出しで（written_hi は written を読んだ時点の上位）。"""
    status, _, words = await port.read(g.EVENTS_WRITTEN, 4)
    assert status == g.STATUS_OK
    return words


@cocotb.test()
async def written_is_64_bits_and_hi_follows_the_low_read(dut):
    port = await setup(dut)
    assert (await port.read(g.EVENTS_CAPACITY, 1))[2][0] == 64
    assert g.EVENTS_WRITTEN_HI == g.EVENTS_WRITTEN + 12
    await port.write(g.EVENTS_CONTROL, [1])
    await stream_send(dut, "uart_in", [0x41])
    for _ in range(100):
        await ClockCycles(dut.clk, 1000)
        written, _, _, hi = await events_words(port)
        if written != (1 << 32) - 2:
            break
    assert (hi, written) == (0, (1 << 32) - 1), (hi, written)              # 1 件目: まだ 2**32 の手前

    await stream_send(dut, "uart_in", [0x42, 0x43])
    for _ in range(100):
        await ClockCycles(dut.clk, 1000)
        words = await events_words(port)
        if words[0] == 1:
            break
    written, lost, capacity, hi = words
    assert (hi << 32 | written) == (1 << 32) + 1 and lost == 0 and capacity == 64     # 3 件目: 2**32 を超えた

    # written_hi は written を読んだときの上位を保持する: written を読まずに読み直しても同じ
    assert (await port.read(g.EVENTS_WRITTEN_HI, 1))[2][0] == 1
