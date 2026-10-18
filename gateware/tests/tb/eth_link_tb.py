"""eth_link（UDP のホストリンク）のテストベンチ。sys = 100 MHz、ref_clk = 50 MHz（RMII、100 Mb/s）。
Ethernet・IPv4・UDP・ARP・ICMP の組み立てと RMII の PHY はここで独立に書く（ホスト側の実装に依らない）。"""

import random
import struct
import zlib

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, FallingEdge, ReadOnly, RisingEdge

from tests.tb.serial_link_tb import collect, feed

MAC = bytes.fromhex("026670720001")
IP = bytes([192, 168, 77, 2])
PORT = 63920
PC_MAC = bytes.fromhex("0a0b0c0d0e0f")
PC_IP = bytes([192, 168, 77, 1])
PC_PORT = 50000


def checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\x00"
    s = sum(struct.unpack(f"!{len(data) // 2}H", data))
    while s >> 16:
        s = (s & 0xFFFF) + (s >> 16)
    return ~s & 0xFFFF


def ipv4(proto: int, payload: bytes, *, src=PC_IP, dst=IP) -> bytes:
    h = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(payload), 0x1234, 0x4000, 64, proto, 0, src, dst)
    return h[:10] + struct.pack("!H", checksum(h)) + h[12:] + payload


def ether(etype: int, payload: bytes, *, dst=MAC, src=PC_MAC) -> bytes:
    body = dst + src + struct.pack("!H", etype) + payload
    body += bytes(max(0, 60 - len(body)))
    return body + struct.pack("<I", zlib.crc32(body))


def udp(ch: int, payload: bytes, *, port=PORT, seq=0) -> bytes:
    data = bytes([ch, seq]) + payload
    return ether(0x0800, ipv4(17, struct.pack("!HHHH", PC_PORT, port, 8 + len(data), 0) + data))


def parse(frame: bytes):
    """送られてきたフレーム（プリアンブルの後〜FCS）を検査して (etype, payload) を返す。"""
    assert len(frame) >= 64, len(frame)
    assert struct.unpack("<I", frame[-4:])[0] == zlib.crc32(frame[:-4]), "FCS"
    return struct.unpack("!H", frame[12:14])[0], frame[:-4]


class Phy:
    """RMII の PHY: rxd / crs_dv を駆動し、txd / tx_en からフレームを集める。"""

    def __init__(self, dut):
        self.dut = dut
        self.frames: list[bytes] = []

    async def send(self, frame: bytes, *, toggle: int = 4):
        """プリアンブル + SFD + frame をジビットで送る。最後の toggle ニブルは CRS_DV を振る（RMII 1.2）。"""
        dibits = [1] * 31 + [3]
        for b in frame:
            dibits += [(b >> s) & 3 for s in (0, 2, 4, 6)]
        n = len(dibits)
        for k, d in enumerate(dibits):
            await FallingEdge(self.dut.ref_clk)
            tail = k >= n - 2 * toggle
            self.dut.crs_dv.value = (k % 2) if tail else 1             # 末尾: ニブルの 1 つ目 0、2 つ目 1
            self.dut.rxd.value = d
        await FallingEdge(self.dut.ref_clk)
        self.dut.crs_dv.value = 0
        self.dut.rxd.value = 0
        await ClockCycles(self.dut.ref_clk, 48)                        # フレーム間隔

    async def watch(self):
        dibits = None
        while True:
            await RisingEdge(self.dut.ref_clk)
            await ReadOnly()
            if self.dut.tx_en.value == 1:
                if dibits is None:
                    dibits = []
                dibits.append(int(self.dut.txd.value))
            elif dibits is not None:
                assert dibits[:32] == [1] * 31 + [3], dibits[:32]
                data = dibits[32:]
                assert len(data) % 4 == 0
                self.frames.append(bytes(sum(data[i + j] << (2 * j) for j in range(4)) for i in range(0, len(data), 4)))
                dibits = None

    async def next_frame(self, timeout=20_000) -> bytes:
        for _ in range(timeout):
            if self.frames:
                return self.frames.pop(0)
            await RisingEdge(self.dut.ref_clk)
        raise TimeoutError("frame")


async def setup(dut):
    for name in ("ctrl_in_valid", "ctrl_in_last", "dap_in_valid", "dap_in_last", "swo_in_valid", "stream_in_valid",
                 "uart_in_valid", "ctrl_in_data", "dap_in_data", "swo_in_data", "stream_in_data", "uart_in_data"):
        getattr(dut, name).value = 0
    for name in ("ctrl_out_ready", "dap_out_ready", "uart_out_ready"):
        getattr(dut, name).value = 1
    dut.crs_dv.value = 0
    dut.rxd.value = 0
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    cocotb.start_soon(Clock(dut.ref_clk, 20, unit="ns").start())
    dut.rst.value = 1
    await ClockCycles(dut.clk, 10)
    dut.rst.value = 0
    await ClockCycles(dut.clk, 10)
    phy = Phy(dut)
    cocotb.start_soon(phy.watch())
    return phy


@cocotb.test()
async def answers_arp_requests_for_its_address(dut):
    phy = await setup(dut)
    arp = struct.pack("!HHBBH6s4s6s4s", 1, 0x0800, 6, 4, 1, PC_MAC, PC_IP, bytes(6), IP)
    await phy.send(ether(0x0806, arp, dst=b"\xff" * 6))
    etype, f = parse(await phy.next_frame())
    assert etype == 0x0806 and f[:6] == PC_MAC and f[6:12] == MAC
    assert f[14:42] == struct.pack("!HHBBH6s4s6s4s", 1, 0x0800, 6, 4, 2, MAC, IP, PC_MAC, PC_IP)
    # 他の IP 宛ての要求には答えない
    await phy.send(ether(0x0806, arp[:24] + bytes([192, 168, 77, 9]), dst=b"\xff" * 6))
    await ClockCycles(dut.ref_clk, 2000)
    assert not phy.frames


@cocotb.test()
async def answers_ping(dut):
    phy = await setup(dut)
    data = bytes(range(56))
    icmp = struct.pack("!BBHHH", 8, 0, 0, 0x77, 3) + data
    icmp = icmp[:2] + struct.pack("!H", checksum(icmp)) + icmp[4:]
    await phy.send(ether(0x0800, ipv4(1, icmp)))
    etype, f = parse(await phy.next_frame())
    assert etype == 0x0800 and f[:6] == PC_MAC
    ip = f[14:34]
    assert checksum(ip) == 0 and ip[9] == 1 and ip[12:16] == IP and ip[16:20] == PC_IP
    reply = f[34:34 + struct.unpack("!H", ip[2:4])[0] - 20]
    assert reply[0] == 0 and reply[4:] == icmp[4:] and checksum(reply) == 0


@cocotb.test()
async def udp_packets_reach_their_channel_and_responses_return(dut):
    phy = await setup(dut)
    payload = bytes(random.Random(1).randrange(256) for _ in range(300))
    task = cocotb.start_soon(collect(dut, "ctrl"))
    await phy.send(udp(0, payload))
    assert await task == payload
    assert dut.peer_known.value == 1
    response = bytes(random.Random(2).randrange(256) for _ in range(100))
    await feed(dut, "ctrl", response)
    etype, f = parse(await phy.next_frame())
    ip = f[14:34]
    assert etype == 0x0800 and f[:6] == PC_MAC and checksum(ip) == 0 and ip[16:20] == PC_IP
    sport, dport, length, _ = struct.unpack("!HHHH", f[34:42])
    assert (sport, dport, length) == (PORT, PC_PORT, 8 + 2 + len(response))
    assert f[42] == 0 and f[44:44 + len(response)] == response
    # DAP（別チャンネル）。seq は送るごとに進む
    task = cocotb.start_soon(collect(dut, "dap"))
    await phy.send(udp(1, b"\x00\x01"))
    assert await task == b"\x00\x01"
    await feed(dut, "dap", b"\x00\x02\x34\x12")
    _, g = parse(await phy.next_frame())
    assert g[42] == 1 and g[43] == (f[43] + 1) & 0xFF and g[44:48] == b"\x00\x02\x34\x12"


@cocotb.test()
async def bad_frames_and_other_destinations_are_ignored(dut):
    phy = await setup(dut)
    task = cocotb.start_soon(collect(dut, "ctrl"))
    bad = bytearray(udp(0, b"bad"))
    bad[-1] ^= 1                                                       # FCS 誤り
    await phy.send(bytes(bad))
    await phy.send(udp(0, b"other port", port=PORT + 1))
    await phy.send(ether(0x0800, ipv4(17, struct.pack("!HHHH", PC_PORT, PORT, 11, 0) + b"\x00\x00x",
                                       dst=bytes([192, 168, 77, 3]))))  # 他の IP
    await phy.send(udp(0, b"good"))
    assert await task == b"good"


@cocotb.test()
async def streams_go_out_in_datagrams_of_at_most_512_bytes(dut):
    phy = await setup(dut)
    task = cocotb.start_soon(collect(dut, "ctrl"))
    await phy.send(udp(0, b"\x01"))                                    # 送信先を決める
    await task
    stream = bytes(random.Random(3).randrange(256) for _ in range(1200))
    await feed(dut, "stream", stream, last=False)
    got = b""
    while len(got) < len(stream):
        _, f = parse(await phy.next_frame())
        n = struct.unpack("!H", f[38:40])[0] - 10
        assert f[42] == 4 and n <= 512
        got += f[44:44 + n]
    assert got == stream
