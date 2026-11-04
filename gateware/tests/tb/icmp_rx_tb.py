"""icmp_rx（ICMP の echo 要求とバッファへの書き込み）のテストベンチ。ICMP_MAX = 128（既定）。
sel・hold・ip_total は他の層の出力の代わりにテストベンチが与える。echo はフレームの最後のバイトの周期に見る。"""

import struct

import cocotb

from tests.tb.eth_rx_frames import WriteMonitor, ether, feed, icmp_echo, ipv4, start

ICMP_MAX = 128


def echo(dut):
    return int(dut.echo.value)


def frame_of(icmp: bytes) -> tuple[bytes, int]:
    return ether(0x0800, ipv4(1, icmp)), 20 + len(icmp)


@cocotb.test()
async def echo_request_is_written_to_the_buffer(dut):
    await start(dut)
    mon = WriteMonitor(dut)
    for size in (0, 56, ICMP_MAX - 8):
        icmp = icmp_echo(bytes((7 * k) & 0xFF for k in range(size)))
        frame, total = frame_of(icmp)
        mon.clear()
        assert await feed(dut, frame, at_last=echo, sel=1, hold=0, ip_total=total) == 1, size
        assert int(dut.checksum.value) == struct.unpack("!H", icmp[2:4])[0]
        memory = mon.memory()
        assert [memory[a] for a in range(len(icmp))] == list(icmp), size
        assert max(memory) < ICMP_MAX                                     # 埋め草も ICMP_MAX までしか書かない


@cocotb.test()
async def other_types_and_sizes_are_not_answered(dut):
    await start(dut)
    icmp = icmp_echo(bytes(16), type_=0)                                  # echo 応答
    frame, total = frame_of(icmp)
    assert await feed(dut, frame, at_last=echo, sel=1, hold=0, ip_total=total) == 0
    icmp = icmp_echo(bytes(ICMP_MAX - 7))                                 # ICMP_MAX を 1 バイト超える
    frame, total = frame_of(icmp)
    assert await feed(dut, frame, at_last=echo, sel=1, hold=0, ip_total=total) == 0
    frame, _ = frame_of(icmp_echo(bytes(16)))
    assert await feed(dut, frame, at_last=echo, sel=1, hold=0, ip_total=20 + 7) == 0   # 見出しの 8 バイトに足りない
    assert await feed(dut, frame, at_last=echo, sel=1, hold=0, ip_total=20 + 8) == 1


@cocotb.test()
async def not_icmp_writes_nothing(dut):
    await start(dut)
    mon = WriteMonitor(dut)
    frame, total = frame_of(icmp_echo(bytes(16)))
    assert await feed(dut, frame, at_last=echo, sel=0, hold=0, ip_total=total) == 0
    assert mon.writes == []


@cocotb.test()
async def hold_keeps_the_buffer(dut):
    await start(dut)
    mon = WriteMonitor(dut)
    frame, total = frame_of(icmp_echo(bytes(16)))
    assert await feed(dut, frame, at_last=echo, sel=1, hold=1, ip_total=total) == 0
    assert mon.writes == []
    # 型の位置で hold だったフレームは、途中で hold が外れても書かず、答えない
    icmp = icmp_echo(bytes(16))
    frame, total = frame_of(icmp)
    dut.sel.value = 1
    dut.ip_total.value = total
    dut.hold.value = 1
    await feed(dut, frame[:40])
    dut.hold.value = 0
    rest = await feed(dut, frame, first=40, at_last=echo)
    assert rest == 0 and mon.writes == []
    # hold が外れた後のフレームは書いて答える
    assert await feed(dut, frame, at_last=echo) == 1
    assert len(mon.writes) > 0

