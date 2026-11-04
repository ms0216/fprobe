"""arp_rx（ARP の欄）のテストベンチ。IP = 192.168.77.2。"""

import struct

import cocotb

from tests.tb.eth_rx_frames import IP, PC_IP, PC_MAC, arp, ether, feed, start


def fields(dut):
    return (int(dut.request.value), int(dut.tpa_me.value), int(dut.sha.value).to_bytes(6, "big"),
            int(dut.spa.value).to_bytes(4, "big"))


@cocotb.test()
async def request_for_my_ip(dut):
    await start(dut)
    await feed(dut, ether(0x0806, arp(1), dst=b"\xff" * 6))
    assert fields(dut) == (1, 1, PC_MAC, PC_IP)
    other_mac, other_ip = bytes.fromhex("102030405060"), bytes([10, 0, 0, 9])
    await feed(dut, ether(0x0806, arp(1, sha=other_mac, spa=other_ip), dst=b"\xff" * 6))
    assert fields(dut) == (1, 1, other_mac, other_ip)


@cocotb.test()
async def reply_is_not_a_request(dut):
    await start(dut)
    await feed(dut, ether(0x0806, arp(2)))
    assert int(dut.request.value) == 0


@cocotb.test()
async def every_fixed_byte_is_checked(dut):
    await start(dut)
    head = struct.pack("!HHBBH", 1, 0x0800, 6, 4, 1)
    for k in range(8):
        bad = bytearray(head)
        bad[k] ^= 0x40
        await feed(dut, ether(0x0806, arp(head=bytes(bad))))
        assert int(dut.request.value) == 0, k
        await feed(dut, ether(0x0806, arp()))                     # 次のフレームでは改めて判定する
        assert int(dut.request.value) == 1, k


@cocotb.test()
async def every_target_ip_byte_is_checked(dut):
    await start(dut)
    for k in range(4):
        tpa = bytearray(IP)
        tpa[k] ^= 0x01
        await feed(dut, ether(0x0806, arp(tpa=bytes(tpa))))
        assert int(dut.request.value) == 1 and int(dut.tpa_me.value) == 0, k
        await feed(dut, ether(0x0806, arp()))
        assert int(dut.tpa_me.value) == 1, k
