"""eth_link + DAP 処理（eth_dap_harness）のテストベンチ。UDP で CMSIS-DAP のコマンドを送り、SWD ターゲットモデルが答える。
実機の Ethernet で DAP が止まった（応答が返らず、その後 ping にも答えなくなった）ときの要求の並びを再現する。"""

import struct

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles

from tests.models.swd_target import SwdTarget
from tests.tb.eth_link_tb import Phy, checksum, ether, ipv4, parse, udp

W_SEL = bytes.fromhex("0800000000")


async def setup(dut):
    dut.crs_dv.value = 0
    dut.rxd.value = 0
    dut.nreset_ext.value = "Z"
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    cocotb.start_soon(Clock(dut.ref_clk, 20, unit="ns").start())
    dut.rst.value = 1
    await ClockCycles(dut.clk, 10)
    dut.rst.value = 0
    await ClockCycles(dut.clk, 10)
    cocotb.start_soon(SwdTarget(dut).run())
    phy = Phy(dut)
    cocotb.start_soon(phy.watch())
    return phy


async def dap(phy, command: bytes, timeout=200_000) -> bytes:
    await phy.send(udp(1, command))
    _, f = parse(await phy.next_frame(timeout))
    n = struct.unpack("!H", f[38:40])[0] - 10
    assert f[42] == 1, f[42]
    return f[44:44 + n]


async def ping(phy):
    icmp = struct.pack("!BBHHH", 8, 0, 0, 1, 1) + bytes(8)
    icmp = icmp[:2] + struct.pack("!H", checksum(icmp)) + icmp[4:]
    await phy.send(ether(0x0800, ipv4(1, icmp)))
    etype, f = parse(await phy.next_frame())
    assert etype == 0x0800 and f[34] == 0


@cocotb.test()
async def dap_commands_over_ethernet_including_malformed_ones(dut):
    phy = await setup(dut)
    assert (await dap(phy, bytes.fromhex("0200")))[:2] == bytes([0x02, 1])
    assert await dap(phy, bytes.fromhex("1233ffffffffffffff")) == bytes([0x12, 0])
    assert await dap(phy, bytes.fromhex("120800")) == bytes([0x12, 0])
    assert (await dap(phy, bytes.fromhex("0600010002")))[:4] == bytes([0x06, 1, 0, 1])
    r = await dap(phy, bytes.fromhex("050004001e000000080000000004000f005006"))
    assert r[:3] == bytes([0x05, 4, 1]) and len(r) == 7, r.hex()
    # 実機の試験で誤って送った形の崩れた要求（データの足りない書き込み）。応答が返り、その後も動き続けること
    for cmd in ("0500010006", "0500020406", "05000208000000000006", "050003080000000008000000000006",
                "050004001e000000080000000004000f00500606", "05000406060606", "050004" + W_SEL.hex() + "060606"):
        r = await dap(phy, bytes.fromhex(cmd))
        assert r[0] == 0x05, (cmd, r.hex())
    await ping(phy)
    assert (await dap(phy, bytes.fromhex("05000106")))[:3] == bytes([0x05, 1, 1])
