"""SWO の端から端（swo_events_harness: 端子 → swo_receiver → event_collector → イベント → ホストの解読）。
最高速度（divisor = 4 = 25 Mbit/s）の UART・Manchester で、続けて流す・間を空ける・差の範囲の前後で流す。
古い形（BYTE_PACK = false、種類 5）と詰めた形（true、種類 15）のどちらでも、解読したバイトと時刻が同じ:
時刻 = 受信器が付けた時刻（UART 方式は開始ビットの辺を見つけた周期、Manchester 方式はバイトの最初のビットの中央の変化を見つけた周期（R4）。時刻の正しさの R3:
docs/superpowers/specs/2026-11-01-time-accuracy-design.md。集約の 1 段レジスタは時刻を変えない）。
詰めた形では、続けて流した区間は 1 件 4 バイト（イベントの帯域が 1/4）。"""
import os

import cocotb
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge, Timer

from fprobe.events import EVENT_BYTES, KIND_SWO, KIND_SWO_PACKED, PACK_DELTA_BITS, decode_events
from tests.models.swo import manchester_halves, segments
from tests.models.uart import uart_bits
from tests.tb.common import start_clock_and_reset

PACK = os.environ.get("SWO_EVENTS_PACK") == "1"
DIVISOR = 4
CLK_NS = 10
GAP_LIMIT = 1 << PACK_DELTA_BITS


async def watch(dut, fired, records, manchester=False):
    while True:
        await RisingEdge(dut.clk)
        await ReadOnly()
        if dut.swo_valid.value == 1:
            fired.append(int(dut.swo_time.value))
            if manchester:                                  # Manchester: 最初のビットの中央（R4。バイトを出すのは最後のビットの中央 + 1）
                assert 7 * DIVISOR <= int(dut.time_count.value) - int(dut.swo_time.value) <= 7 * DIVISOR + 2
            else:                                           # UART: 開始ビットの辺（停止ビットの中央 = 約 9.5 ビット前）
                assert 9 * DIVISOR < int(dut.time_count.value) - int(dut.swo_time.value) <= 11 * DIVISOR
        if dut.event_valid.value == 1:
            records.append(int(dut.event_data.value))


async def send_uart(dut, data: bytes):
    for byte in data:
        for bit in uart_bits(byte):
            dut.swo.value = bit
            await Timer(DIVISOR * CLK_NS, unit="ns")


async def send_manchester(dut, frame: bytes):
    for level, length in segments(manchester_halves(frame), DIVISOR * CLK_NS / 2):
        dut.swo.value = level
        await Timer(round(length), unit="ns")


async def idle(dut, cycles):
    await ClockCycles(dut.clk, cycles)


def expected_records(times):
    """切る条件（4 バイト・差が 12 ビットに収まらない）で、この時刻の列が何件になるか。"""
    n, size, last = 0, 0, None
    for t in times:
        if size == 4 or last is None or t - last >= GAP_LIMIT:
            n, size = n + 1, 0
        size += 1
        last = t
    return n


async def setup(dut, manchester):
    dut.swo.value = 0 if manchester else 1
    dut.run.value = 0
    dut.manchester.value = int(manchester)
    dut.divisor.value = DIVISOR
    dut.events_enable.value = 0
    await start_clock_and_reset(dut)
    dut.events_enable.value = 1
    dut.run.value = 1
    await ClockCycles(dut.clk, 40)                          # 受信の開始の窓（FLUSH_CYCLES）


async def check(dut, fired, records, sent):
    await idle(dut, 2 * GAP_LIMIT)                          # 最後の件は差の範囲を過ぎて出る
    kinds = {w >> 120 for w in records}
    assert kinds == {KIND_SWO_PACKED if PACK else KIND_SWO}, kinds
    events = decode_events(b"".join(w.to_bytes(EVENT_BYTES, "little") for w in records))
    assert bytes(e.data for e in events) == sent
    assert [e.time for e in events] == fired
    assert int(dut.byte_lost.value) == 0
    assert len(records) == (expected_records([e.time for e in events]) if PACK else len(sent))
    return events


@cocotb.test()
async def uart_swo_at_25_mbit(dut):
    await setup(dut, manchester=False)
    fired, records = [], []
    cocotb.start_soon(watch(dut, fired, records))
    burst = bytes(range(0x30, 0x30 + 36))
    await send_uart(dut, burst)                             # 続けて流す（1 バイト 40 周期）
    await idle(dut, GAP_LIMIT + 1000)                       # 差の範囲を超えて空ける
    spaced = b"\x01\x02\x03"
    for b in spaced:                                        # 範囲の内側の間隔（3000 周期）
        await send_uart(dut, bytes([b]))
        await idle(dut, 3000)
    await idle(dut, GAP_LIMIT)                              # 上の 3 バイトの件と分ける
    late = b"\xA0\xA1"
    for b in late:                                          # 範囲の外の間隔（5000 周期）: 1 バイトずつ別の件
        await send_uart(dut, bytes([b]))
        await idle(dut, 5000)
    await check(dut, fired, records, burst + spaced + late)
    if PACK:
        assert len(records) == 9 + 1 + 2                    # 36 バイトは 9 件（1 件 4 バイト）


@cocotb.test()
async def manchester_swo_at_25_mbit(dut):
    await setup(dut, manchester=True)
    fired, records = [], []
    cocotb.start_soon(watch(dut, fired, records, manchester=True))
    frame = bytes(range(0x80, 0x80 + 20))
    await send_manchester(dut, frame)                       # 1 フレームに 20 バイト（1 バイト 32 周期）
    await idle(dut, GAP_LIMIT + 1000)
    await send_manchester(dut, b"\x55\xAA\x0F")
    await check(dut, fired, records, frame + b"\x55\xAA\x0F")
    if PACK:
        assert len(records) == 5 + 1
