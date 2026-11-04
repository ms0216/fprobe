"""ipv4_rx（IPv4 の見出し）のテストベンチ。IP = 192.168.77.2。ok はフレームの最後のバイトの周期に見る。"""

import cocotb

from tests.tb.eth_rx_frames import IP, PC_IP, ether, feed, ipv4, ipv4_header, start


def ok(dut):
    return int(dut.ok.value)


@cocotb.test()
async def accepts_a_header_for_me(dut):
    await start(dut)
    payload = bytes(range(30))
    assert await feed(dut, ether(0x0800, ipv4(17, payload)), at_last=ok) == 1
    assert int(dut.total.value) == 20 + len(payload)
    assert int(dut.proto.value) == 17
    assert int(dut.src.value).to_bytes(4, "big") == PC_IP
    other = bytes([10, 1, 2, 3])
    assert await feed(dut, ether(0x0800, ipv4(1, bytes(8), src=other, flags=0)), at_last=ok) == 1   # DF なしも可
    assert int(dut.proto.value) == 1 and int(dut.src.value).to_bytes(4, "big") == other


@cocotb.test()
async def rejects_a_bad_checksum(dut):
    await start(dut)
    assert await feed(dut, ether(0x0800, ipv4(17, bytes(30), good_checksum=False)), at_last=ok) == 0
    assert await feed(dut, ether(0x0800, ipv4(17, bytes(30))), at_last=ok) == 1


@cocotb.test()
async def rejects_another_destination(dut):
    await start(dut)
    for k in range(4):
        dst = bytearray(IP)
        dst[k] ^= 0x80
        assert await feed(dut, ether(0x0800, ipv4(17, bytes(30), dst=bytes(dst))), at_last=ok) == 0, k


@cocotb.test()
async def rejects_options_and_fragments(dut):
    await start(dut)
    assert await feed(dut, ether(0x0800, ipv4(17, bytes(30), ver_ihl=0x46)), at_last=ok) == 0     # オプションあり
    assert await feed(dut, ether(0x0800, ipv4(17, bytes(30), ver_ihl=0x65)), at_last=ok) == 0     # 版 6
    assert await feed(dut, ether(0x0800, ipv4(17, bytes(30), flags=0x2000)), at_last=ok) == 0     # MF
    assert await feed(dut, ether(0x0800, ipv4(17, bytes(30), flags=0x0001)), at_last=ok) == 0     # 位置 1
    assert await feed(dut, ether(0x0800, ipv4(17, bytes(30), flags=0x0100)), at_last=ok) == 0     # 位置の上位
    assert await feed(dut, ether(0x0800, ipv4(17, bytes(30))), at_last=ok) == 1


@cocotb.test()
async def needs_the_whole_datagram(dut):
    await start(dut)
    payload = bytes(range(60))
    frame = ether(0x0800, ipv4(17, payload))
    assert len(frame) == 14 + 20 + 60
    assert await feed(dut, frame, at_last=ok) == 1
    assert await feed(dut, frame[:-1], at_last=ok) == 0                    # 全長の 1 バイト手前で終わった
    # 全長が見出しより短い・長いのは見出しの値どおりに判定する（チェックサムは合わせる）
    assert await feed(dut, ether(0x0800, ipv4_header(17, 0, total=200) + payload), at_last=ok) == 0
