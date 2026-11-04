"""eth_rx_header（Ethernet の見出しとフレーム内の位置）のテストベンチ。MAC = 02:66:70:72:00:01。"""

import cocotb
from cocotb.triggers import FallingEdge, Timer

from tests.tb.eth_rx_frames import MAC, PC_MAC, ether, feed, start

FRAME_POS_MAX = 2047


async def positions(dut, frame: bytes, *, last: bool = True) -> list[int]:
    """frame を流し、各バイトの周期の pos を返す（last = False なら最後のバイトに last を付けない）。"""
    seen = []
    for k, b in enumerate(frame):
        dut.valid.value = 1
        dut.data.value = b
        dut.last.value = int(last and k == len(frame) - 1)
        await Timer(1, unit="ns")
        seen.append(int(dut.pos.value))
        await FallingEdge(dut.clk)
    dut.valid.value = 0
    dut.last.value = 0
    return seen


@cocotb.test()
async def reads_the_header_fields(dut):
    await start(dut)
    await feed(dut, ether(0x0800, bytes(46)), drive_pos=False)
    assert int(dut.dst_me.value) == 1 and int(dut.dst_bc.value) == 0
    assert int(dut.src_mac.value) == int.from_bytes(PC_MAC, "big")
    assert int(dut.etype.value) == 0x0800

    await feed(dut, ether(0x0806, bytes(46), dst=b"\xff" * 6, src=bytes(range(6))), drive_pos=False)
    assert int(dut.dst_me.value) == 0 and int(dut.dst_bc.value) == 1
    assert int(dut.src_mac.value) == 0x000102030405
    assert int(dut.etype.value) == 0x0806


@cocotb.test()
async def any_differing_destination_byte_clears_the_match(dut):
    await start(dut)
    for k in range(6):
        dst = bytearray(MAC)
        dst[k] ^= 0x10
        await feed(dut, ether(0x0800, bytes(46), dst=bytes(dst)), drive_pos=False)
        assert int(dut.dst_me.value) == 0 and int(dut.dst_bc.value) == 0, k
        bc = bytearray(b"\xff" * 6)
        bc[k] = 0xFE
        await feed(dut, ether(0x0800, bytes(46), dst=bytes(bc)), drive_pos=False)
        assert int(dut.dst_me.value) == 0 and int(dut.dst_bc.value) == 0, k


@cocotb.test()
async def position_counts_bytes_and_restarts_after_last(dut):
    await start(dut)
    assert await positions(dut, bytes(64)) == list(range(64))
    assert await positions(dut, bytes(5)) == list(range(5))          # last の次は 0 から
    # valid = 0 の周期は数えない
    seen = []
    for k in range(10):
        dut.valid.value = 1
        dut.data.value = k
        dut.last.value = int(k == 9)
        await Timer(1, unit="ns")
        seen.append(int(dut.pos.value))
        await FallingEdge(dut.clk)
        dut.valid.value = 0
        await FallingEdge(dut.clk)
    dut.last.value = 0
    assert seen == list(range(10))


@cocotb.test()
async def position_saturates(dut):
    await start(dut)
    seen = await positions(dut, bytes(FRAME_POS_MAX + 10), last=False)
    assert seen[:FRAME_POS_MAX + 1] == list(range(FRAME_POS_MAX + 1))
    assert seen[FRAME_POS_MAX:] == [FRAME_POS_MAX] * 10
    assert await positions(dut, bytes(2)) == [FRAME_POS_MAX, FRAME_POS_MAX]   # 最後のバイトまで止まったまま
    assert await positions(dut, bytes(3)) == [0, 1, 2]


@cocotb.test()
async def reset_restarts_the_position(dut):
    await start(dut)
    await positions(dut, bytes(20), last=False)
    dut.rst.value = 1
    await FallingEdge(dut.clk)
    dut.rst.value = 0
    assert await positions(dut, bytes(4)) == [0, 1, 2, 3]
