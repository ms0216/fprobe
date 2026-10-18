"""usb_ep_in（DEPTH=64, MPS=16, FLUSH_CYCLES=20）のテストベンチ。

プロトコル側（パケットの取り出し・ACK・再送）はテストベンチが演じる。
"""

import random

import cocotb
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge

from tests.tb.common import start_clock_and_reset

DEPTH, MPS, FLUSH = 64, 16, 20


async def setup(dut):
    dut.packet_size.value = MPS
    dut.in_valid.value = 0
    dut.in_data.value = 0
    dut.in_last.value = 0
    dut.pl_ready.value = 0
    dut.ack.value = 0
    dut.retry.value = 0
    await start_clock_and_reset(dut)


async def write(dut, data: bytes, *, last: bool):
    for i, byte in enumerate(data):
        dut.in_valid.value = 1
        dut.in_data.value = byte
        dut.in_last.value = int(last and i == len(data) - 1)
        while True:
            await ReadOnly()
            accepted = int(dut.in_ready.value)
            await RisingEdge(dut.clk)
            if accepted:
                break
    dut.in_valid.value = 0
    dut.in_last.value = 0


async def take_packet(dut, rng, *, finish="ack", timeout=500):
    """パケットが用意されるのを待ち、内容を読み出し、ack か retry で締める。"""
    for _ in range(timeout):
        await ReadOnly()
        if int(dut.pkt_ready.value):
            break
        await RisingEdge(dut.clk)
    else:
        return None
    empty = int(dut.pkt_empty.value)
    await RisingEdge(dut.clk)
    data = bytearray()
    if not empty:
        while True:
            dut.pl_ready.value = int(rng.random() < 0.7)
            await ReadOnly()
            if int(dut.pl_valid.value) and int(dut.pl_ready.value):
                data.append(int(dut.pl_data.value))
                if int(dut.pl_last.value):
                    await RisingEdge(dut.clk)
                    break
            await RisingEdge(dut.clk)
        dut.pl_ready.value = 0
    (dut.ack if finish == "ack" else dut.retry).value = 1
    await RisingEdge(dut.clk)
    dut.ack.value = 0
    dut.retry.value = 0
    await RisingEdge(dut.clk)
    return bytes(data)


@cocotb.test()
async def splits_transfer_into_max_size_packets(dut):
    await setup(dut)
    rng = random.Random(1)
    data = bytes(range(40))
    writer = cocotb.start_soon(write(dut, data, last=True))
    packets = [await take_packet(dut, rng) for _ in range(3)]
    await writer
    assert packets == [data[:16], data[16:32], data[32:]]


@cocotb.test()
async def transfer_ending_on_boundary_gets_zlp(dut):
    await setup(dut)
    rng = random.Random(2)
    data = bytes(range(32))
    writer = cocotb.start_soon(write(dut, data, last=True))
    packets = [await take_packet(dut, rng) for _ in range(3)]
    await writer
    assert packets == [data[:16], data[16:], b""]


@cocotb.test()
async def boundary_without_zlp(dut):
    # ZLP = false のときだけ実行する（tests/test_usb_endpoints.py）
    await setup(dut)
    rng = random.Random(2)
    data = bytes(range(32))
    writer = cocotb.start_soon(write(dut, data, last=True))
    packets = [await take_packet(dut, rng) for _ in range(2)]
    await writer
    assert packets == [data[:16], data[16:]]
    assert await take_packet(dut, rng, timeout=100) is None       # 続く ZLP は無い


@cocotb.test()
async def retry_resends_the_same_packet(dut):
    await setup(dut)
    rng = random.Random(3)
    await write(dut, b"hello", last=True)
    first = await take_packet(dut, rng, finish="retry")
    again = await take_packet(dut, rng, finish="retry")
    final = await take_packet(dut, rng, finish="ack")
    assert first == again == final == b"hello"
    assert await take_packet(dut, rng, timeout=50) is None


@cocotb.test()
async def stalled_stream_is_flushed_as_short_packet(dut):
    await setup(dut)
    rng = random.Random(4)
    await write(dut, b"abc", last=False)
    await ClockCycles(dut.clk, FLUSH // 2)
    await ReadOnly()
    assert int(dut.pkt_ready.value) == 0         # まだ溜めている
    await RisingEdge(dut.clk)
    assert await take_packet(dut, rng) == b"abc"


@cocotb.test()
async def backpressure_when_full_and_no_loss(dut):
    await setup(dut)
    rng = random.Random(5)
    data = bytes(rng.randrange(256) for _ in range(DEPTH * 3))
    writer = cocotb.start_soon(write(dut, data, last=True))
    received = bytearray()
    while len(received) < len(data):
        packet = await take_packet(dut, rng)
        assert packet is not None
        received += packet
    await writer
    assert bytes(received) == data


@cocotb.test()
async def runtime_packet_size_for_full_speed(dut):
    # FS にフォールバックしたときはバルクの MPS が小さくなる（実行時に packet_size で指定）
    await setup(dut)
    dut.packet_size.value = 8
    rng = random.Random(6)
    data = bytes(range(20))
    writer = cocotb.start_soon(write(dut, data, last=True))
    packets = [await take_packet(dut, rng) for _ in range(3)]
    await writer
    assert packets == [data[:8], data[8:16], data[16:]]


@cocotb.test()
async def backpressure_does_not_trigger_flush(dut):
    # 実機 HIL で発覚: HS ネゴシエーション前（FS のパケットサイズ）に小さなパケットでバッファが埋まり、
    # その後、満杯で待たされている間の「小休止フラッシュ」が短パケットを作り続けてスループットが落ちた。
    # データは常にあるので、待たされている間は小休止ではない。小さいパケットが掃けた後は MPS ちょうどになること
    await setup(dut)
    dut.packet_size.value = 4
    rng = random.Random(8)
    data = bytes(rng.randrange(256) for _ in range(DEPTH * 4))
    writer = cocotb.start_soon(write(dut, data, last=False))
    await ClockCycles(dut.clk, 200)                # FS 相当の小さなパケットで埋まる
    dut.packet_size.value = MPS                    # HS になった
    packets = []
    while sum(map(len, packets)) < len(data):
        await ClockCycles(dut.clk, FLUSH * 3)      # ホストがなかなか読みに来ない
        packets.append(await take_packet(dut, rng))
    await writer
    sizes = [len(p) for p in packets]
    small = sizes.index(MPS)
    assert all(n <= 4 for n in sizes[:small]), sizes
    assert sizes[small:-1] == [MPS] * (len(sizes) - small - 1), sizes
    assert b"".join(packets) == data


async def _reset(dut):
    dut.rst.value = 1
    await ClockCycles(dut.clk, 2)
    dut.rst.value = 0
    await RisingEdge(dut.clk)


@cocotb.test()
async def flush_does_not_race_with_new_data_while_full(dut):
    # レビューで発覚: バッファ満杯・途中のパケットあり の状態で、小休止の判定がちょうど成立する周期に
    # 新しいデータが来ると、長さだけが余分に積まれてデータが重複し、やがて書き込めなくなっていた
    await setup(dut)
    for offset in range(4):
        await _reset(dut)
        rng = random.Random(10 + offset)
        await write(dut, bytes(range(48)), last=False)            # 16 バイト × 3 パケット
        await write(dut, bytes(range(48, 56)), last=False)
        await ClockCycles(dut.clk, FLUSH + 5)                     # 8 バイトの短パケットとして閉じる
        await write(dut, bytes(range(56, 64)), last=False)        # メモリ満杯、途中のパケット 8 バイト
        await ClockCycles(dut.clk, FLUSH - 1 + offset)
        dut.in_valid.value = 1                                    # 満杯なので受け付けられない新データ
        dut.in_data.value = 0xEE
        await ClockCycles(dut.clk, 2)
        dut.in_valid.value = 0
        received = bytearray()
        for _ in range(12):
            packet = await take_packet(dut, rng, timeout=200)
            if packet is None:
                break
            received += packet
        assert bytes(received) == bytes(range(64)), f"offset {offset}: {received.hex()}"


@cocotb.test()
async def lone_bytes_are_each_flushed(dut):
    # ゆっくり 1 バイトずつ届くストリーム（UART の受信）でも、各バイトが小休止で送られる
    await setup(dut)
    rng = random.Random(9)
    for byte in b"AB\n":
        await write(dut, bytes([byte]), last=False)
        assert await take_packet(dut, rng, timeout=FLUSH + 50) == bytes([byte])
