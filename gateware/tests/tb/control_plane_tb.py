"""control_plane（ハーネス経由）のテストベンチ。要求はホストライブラリの符号化で作る。"""

import random
import struct

import cocotb

from fprobe import generated as g
from fprobe.control import decode_response, encode_exchange, encode_read, encode_write
from tests.tb.common import start_clock_and_reset
from tests.tb.control_port import ControlPort

MEMORY, ERROR_WINDOW = 0x000, 0x100          # バイトアドレス（ワード 0..63 / 64..67）
UNMAPPED = 0x200


async def setup(dut, seed=1):
    port = ControlPort(dut, request="in", response="out", rng=random.Random(seed))
    await start_clock_and_reset(dut)
    return port


@cocotb.test()
async def write_then_read_back(dut):
    port = await setup(dut)
    rng = random.Random(2)
    words = [rng.getrandbits(32) for _ in range(64)]
    assert await port.write(MEMORY, words[:40]) == (g.STATUS_OK, 40)
    assert await port.write(MEMORY + 160, words[40:]) == (g.STATUS_OK, 24)
    assert await port.read(MEMORY, 64) == (g.STATUS_OK, 64, words)
    assert await port.read(MEMORY + 4 * 63, 1) == (g.STATUS_OK, 1, [words[63]])


@cocotb.test()
async def bus_errors_stop_at_the_failing_word(dut):
    port = await setup(dut)
    await port.write(MEMORY + 4 * 60, [1, 2, 3, 4])
    assert await port.read(MEMORY + 4 * 60, 8) == (g.STATUS_BUS_ERROR, 4, [1, 2, 3, 4])   # 64 番は窓 1
    assert await port.read(UNMAPPED, 2) == (g.STATUS_BUS_ERROR, 0, [])
    assert await port.write(MEMORY + 4 * 62, [7, 8, 9]) == (g.STATUS_BUS_ERROR, 2)
    assert (await port.read(MEMORY + 4 * 62, 2))[2] == [7, 8]


async def raw(port, message: bytes):
    return decode_response(await port.transact(message))


@cocotb.test()
async def malformed_requests_are_reported_and_drained(dut):
    port = await setup(dut)
    write = encode_write(9, MEMORY, [0x11111111, 0x22222222])
    cases = [
        (b"\x01", (0, g.STATUS_BAD_LENGTH, 0)),                          # tag も無い
        (encode_read(5, MEMORY, 1)[:5], (5, g.STATUS_BAD_LENGTH, 0)),    # ヘッダ途中
        (bytes([0x7F, 6]) + bytes(20), (6, g.STATUS_BAD_OP, 0)),         # 未知の op（残りは読み捨て）
        (encode_read(7, MEMORY, 0), (7, g.STATUS_BAD_LENGTH, 0)),        # count = 0
        (encode_read(8, MEMORY + 2, 1), (8, g.STATUS_BAD_LENGTH, 0)),    # 整列していない
        (encode_read(10, MEMORY, 1) + b"xx", (10, g.STATUS_BAD_LENGTH, 0)),  # READ にデータ
        (write[:8], (9, g.STATUS_BAD_LENGTH, 0)),                        # WRITE にデータ無し
        (write[:-2], (9, g.STATUS_BAD_LENGTH, 1)),                       # 2 ワード目が途中まで
        (encode_write(11, MEMORY, [0x33333333]) + bytes(4), (11, g.STATUS_BAD_LENGTH, 1)),  # 余り
    ]
    for message, expected in cases:
        tag, status, done, data = await raw(port, message)
        assert (tag, status, done, data) == (*expected, []), message
    # 長さ不正でも、それまでに届いた完全なワードは書かれている
    assert await port.read(MEMORY, 1) == (g.STATUS_OK, 1, [0x33333333])


@cocotb.test()
async def long_read_streams_under_backpressure(dut):
    port = await setup(dut, seed=4)
    port.stall = 0.7
    pattern = list(range(1000, 1064))
    await port.write(MEMORY, pattern)
    for _ in range(3):
        assert await port.read(MEMORY, 64) == (g.STATUS_OK, 64, pattern)


@cocotb.test()
async def write_error_with_pending_data_is_drained(dut):
    port = await setup(dut)
    status, done = await port.write(MEMORY + 4 * 63, [5, 6, 7])         # 2 ワード目（64 番）でエラー
    assert (status, done) == (g.STATUS_BUS_ERROR, 1)
    assert await raw(port, bytes([0x7F, 0x21]) + bytes(6)) == (0x21, g.STATUS_BAD_OP, 0, [])   # ちょうど 8 バイト
    assert await port.read(MEMORY + 4 * 63, 1) == (g.STATUS_OK, 1, [5])


@cocotb.test()
async def back_to_back_requests_without_waiting(dut):
    # 次の要求を前の応答の途中から出し続けても、1 つずつ正しく処理される
    port = await setup(dut, seed=7)
    requests = [encode_write(1, MEMORY, [0xAAAA0001, 0xAAAA0002]), encode_read(2, MEMORY, 2),
                bytes([0x55, 3]) + bytes(10), encode_read(4, MEMORY + 4, 1)]
    responses = await port.pipeline(requests)
    assert [decode_response(r) for r in responses] == [
        (1, g.STATUS_OK, 2, []), (2, g.STATUS_OK, 2, [0xAAAA0001, 0xAAAA0002]),
        (3, g.STATUS_BAD_OP, 0, []), (4, g.STATUS_OK, 1, [0xAAAA0002])]


@cocotb.test()
async def reset_in_the_middle_recovers(dut):
    from cocotb.triggers import ClockCycles, RisingEdge
    port = await setup(dut)
    request = encode_write(1, MEMORY, list(range(20)))
    port.req["valid"].value = 1
    for byte in request[:13]:                               # ヘッダとデータの途中まで送ってリセット
        port.req["data"].value = byte
        port.req["last"].value = 0
        await RisingEdge(dut.clk)
        while not port.req["ready"].value:
            await RisingEdge(dut.clk)
    port.req["valid"].value = 0
    dut.rst.value = 1
    await ClockCycles(dut.clk, 2)
    dut.rst.value = 0
    await RisingEdge(dut.clk)
    assert await port.read(MEMORY, 1) == (g.STATUS_OK, 1, [0])          # 新しい要求として正しく扱う


# --- EXCHANGE（同じ番地に書き、書くごとに読む。番地は進めない）-------------------------------------------

@cocotb.test()
async def exchange_writes_then_reads_the_same_word(dut):
    port = await setup(dut)
    await port.write(MEMORY, [0x100, 0x101])
    words = [0xA0000000 + i for i in range(40)]
    # メモリの窓では、書いた直後に読むので書いた値が返る（読んでから書くなら前の値が返る）
    assert await port.exchange(MEMORY, words) == (g.STATUS_OK, 40, words)
    assert await port.read(MEMORY, 2) == (g.STATUS_OK, 2, [words[-1], 0x101])     # 番地は進めない


@cocotb.test()
async def exchange_streams_under_backpressure(dut):
    port = await setup(dut, seed=11)
    port.stall, port.gap = 0.7, 0.5
    words = list(range(7, 7 + 120))
    assert await port.exchange(MEMORY + 4 * 5, words) == (g.STATUS_OK, 120, words)


@cocotb.test()
async def exchange_errors(dut):
    port = await setup(dut)
    request = encode_exchange(3, MEMORY, [1, 2, 3])
    cases = [
        (request[:8], (3, g.STATUS_BAD_LENGTH, 0, [])),                     # データ無し
        (request[:-2], (3, g.STATUS_BAD_LENGTH, 2, [1, 2])),                # 3 語目が途中まで
        (encode_exchange(4, MEMORY, [5]) + bytes(4), (4, g.STATUS_BAD_LENGTH, 1, [5])),   # 余り
        (struct.pack("<BBHI", g.OP_EXCHANGE, 5, 0, MEMORY) + bytes(4), (5, g.STATUS_BAD_LENGTH, 0, [])),  # count = 0
        (encode_exchange(6, MEMORY + 2, [1]), (6, g.STATUS_BAD_LENGTH, 0, [])),   # 整列していない
        (encode_exchange(7, ERROR_WINDOW, [1, 2]), (7, g.STATUS_BUS_ERROR, 0, [])),   # 書きでバスエラー
        (encode_exchange(8, UNMAPPED, [1]), (8, g.STATUS_BUS_ERROR, 0, [])),
    ]
    for message, expected in cases:
        assert await raw(port, message) == expected, message
    assert await port.read(MEMORY, 1) == (g.STATUS_OK, 1, [5])          # 余りの前の語は交換済み


@cocotb.test()
async def exchange_between_other_requests(dut):
    port = await setup(dut, seed=13)
    requests = [encode_exchange(1, MEMORY + 8, [0x11, 0x22]), encode_read(2, MEMORY + 8, 1),
                encode_write(3, MEMORY, [9, 8]), encode_exchange(4, MEMORY + 4, [0x33])]
    responses = await port.pipeline(requests)
    assert [decode_response(r) for r in responses] == [
        (1, g.STATUS_OK, 2, [0x11, 0x22]), (2, g.STATUS_OK, 1, [0x22]), (3, g.STATUS_OK, 2, []),
        (4, g.STATUS_OK, 1, [0x33])]
