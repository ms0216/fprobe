"""udp_rx（UDP の見出しと payload のバッファへの書き込み）のテストベンチ。PORT_NUM = 63920、MAX_PAYLOAD = 16、
RAM_BITS = 5（試験のラッパで与える）。sel・ip_total は他の層の出力の代わりにテストベンチが与える。ok はフレームの
最後のバイトの周期に見る。"""

import cocotb

from tests.tb.eth_rx_frames import PC_PORT, PORT, WriteMonitor, ether, feed, ipv4, start, udp

MAX_PAYLOAD = 16


def ok(dut):
    return int(dut.ok.value)


def frame_of(segment: bytes) -> tuple[bytes, int]:
    return ether(0x0800, ipv4(17, segment)), 20 + len(segment)


@cocotb.test()
async def payload_goes_to_the_buffer_without_seq(dut):
    await start(dut)
    mon = WriteMonitor(dut)
    for size in (0, 1, 5, MAX_PAYLOAD):
        data = bytes(0xA0 + k for k in range(size))
        frame, total = frame_of(udp(bytes([0x03, 0x77]) + data))
        mon.clear()
        assert await feed(dut, frame, at_last=ok, sel=1, ip_total=total) == 1, size
        assert int(dut.src_port.value) == PC_PORT
        assert int(dut.data_len.value) == 1 + size
        # 番地 0 = ch、1〜 = data（seq は書かない）。UDP の長さの後の埋め草は書かない
        assert mon.writes == [(0, 0x03), *((1 + k, b) for k, b in enumerate(data))], size


@cocotb.test()
async def other_ports_and_protocols_write_nothing(dut):
    await start(dut)
    mon = WriteMonitor(dut)
    frame, total = frame_of(udp(bytes([1, 0, 9, 9]), dst_port=PORT + 1))
    assert await feed(dut, frame, at_last=ok, sel=1, ip_total=total) == 0
    frame, total = frame_of(udp(bytes([1, 0, 9, 9])))
    assert await feed(dut, frame, at_last=ok, sel=0, ip_total=total) == 0
    assert mon.writes == []


@cocotb.test()
async def lengths_are_checked(dut):
    await start(dut)
    mon = WriteMonitor(dut)
    # data が MAX_PAYLOAD を超える: 答えず、書くのはバッファの範囲（ch + MAX_PAYLOAD）まで
    frame, total = frame_of(udp(bytes([1, 0]) + bytes(range(MAX_PAYLOAD + 4))))
    assert await feed(dut, frame, at_last=ok, sel=1, ip_total=total) == 0
    assert max(a for a, _ in mon.writes) == MAX_PAYLOAD
    # ch と seq が無い
    frame, total = frame_of(udp(bytes([1])))
    assert await feed(dut, frame, at_last=ok, sel=1, ip_total=total) == 0
    # UDP の長さが IP の全長に収まらない
    frame, total = frame_of(udp(bytes([1, 0, 5])))
    assert await feed(dut, frame, at_last=ok, sel=1, ip_total=total - 1) == 0
    assert await feed(dut, frame, at_last=ok, sel=1, ip_total=total) == 1
    # UDP の長さが見出しより短い（そこから先の payload は書かない）
    mon.clear()
    frame, total = frame_of(udp(bytes([1, 0, 5, 6]), length=8 + 3))
    assert await feed(dut, frame, at_last=ok, sel=1, ip_total=total) == 1
    assert mon.writes == [(0, 1), (1, 5)]
    frame, total = frame_of(udp(bytes([1, 0, 5, 6]), length=8 + 1))
    assert await feed(dut, frame, at_last=ok, sel=1, ip_total=total) == 0
