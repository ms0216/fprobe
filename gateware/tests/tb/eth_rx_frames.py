"""受信の各層（eth_rx_header・arp_rx・ipv4_rx・icmp_rx・udp_rx）のテストベンチの共通部品。

フレーム（プリアンブル・SFD・FCS を除く Ethernet のフレーム）の組み立てはここで独立に書く（ホスト側の実装に依らない）。
層の DUT には 1 周期 1 バイトで valid・data・pos（フレーム内の位置）を与える。入力は立ち下がりで変え、
「最後のバイトの周期」に見る出力（ok 等）はその周期の立ち上がりの前に読む。
"""

import struct

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, ReadOnly, RisingEdge, Timer

MAC = bytes.fromhex("026670720001")
IP = bytes([192, 168, 77, 2])
PORT = 63920
PC_MAC = bytes.fromhex("0a0b0c0d0e0f")
PC_IP = bytes([192, 168, 77, 1])
PC_PORT = 50000

MAC_GENERIC = f"{int.from_bytes(MAC, 'big'):048b}"
IP_GENERIC = f"{int.from_bytes(IP, 'big'):032b}"


def checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\x00"
    s = sum(struct.unpack(f"!{len(data) // 2}H", data))
    while s >> 16:
        s = (s & 0xFFFF) + (s >> 16)
    return ~s & 0xFFFF


def ether(etype: int, payload: bytes, *, dst=MAC, src=PC_MAC) -> bytes:
    """Ethernet のフレーム（60 バイトまで 0 で埋める。FCS は付けない）。"""
    body = dst + src + struct.pack("!H", etype) + payload
    return body + bytes(max(0, 60 - len(body)))


def ipv4_header(proto: int, payload_len: int, *, src=PC_IP, dst=IP, flags=0x4000, ver_ihl=0x45,
                total=None, good_checksum=True) -> bytes:
    total = 20 + payload_len if total is None else total
    h = struct.pack("!BBHHHBBH4s4s", ver_ihl, 0, total, 0x1234, flags, 64, proto, 0, src, dst)
    s = checksum(h) ^ (0 if good_checksum else 0x0101)
    return h[:10] + struct.pack("!H", s) + h[12:]


def ipv4(proto: int, payload: bytes, **kw) -> bytes:
    return ipv4_header(proto, len(payload), **kw) + payload


def icmp_echo(data: bytes, *, type_=8, ident=0x1234, seq=1) -> bytes:
    h = struct.pack("!BBHHH", type_, 0, 0, ident, seq)
    s = checksum(h + data)
    return h[:2] + struct.pack("!H", s) + h[4:] + data


def udp(payload: bytes, *, src_port=PC_PORT, dst_port=PORT, length=None) -> bytes:
    length = 8 + len(payload) if length is None else length
    return struct.pack("!HHHH", src_port, dst_port, length, 0) + payload


def arp(oper: int = 1, *, sha=PC_MAC, spa=PC_IP, tha=bytes(6), tpa=IP, head=None) -> bytes:
    head = struct.pack("!HHBBH", 1, 0x0800, 6, 4, oper) if head is None else head
    return head + sha + spa + tha + tpa


async def start(dut):
    """clk を起動し、入力を空にする（rst があれば数周期かける）。"""
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.valid.value = 0
    dut.data.value = 0
    if hasattr(dut, "rst"):
        dut.rst.value = 1
        for _ in range(3):
            await RisingEdge(dut.clk)
        await FallingEdge(dut.clk)
        dut.rst.value = 0
    await FallingEdge(dut.clk)


async def feed(dut, frame: bytes, *, at_last=None, drive_pos: bool = True, first: int = 0, gap: int = 0, **inputs):
    """frame の first 番目以降を 1 周期 1 バイトで与える（gap > 0 なら各バイトの後に valid = 0 を gap 周期）。inputs は
    DUT の他の入力に与える値。at_last(dut) を最後のバイトの周期（立ち上がりの前）に呼び、その戻り値を返す。
    立ち下がりで呼び、立ち下がりで返る。"""
    for name, value in inputs.items():
        getattr(dut, name).value = value
    result = None
    for k in range(first, len(frame)):
        b = frame[k]
        dut.valid.value = 1
        dut.data.value = b
        if drive_pos:
            dut.pos.value = k
        if hasattr(dut, "last"):
            dut.last.value = int(k == len(frame) - 1)
        if k == len(frame) - 1 and at_last is not None:
            await Timer(1, unit="ns")
            result = at_last(dut)
        await FallingEdge(dut.clk)
        for _ in range(gap):
            dut.valid.value = 0
            await FallingEdge(dut.clk)
    dut.valid.value = 0
    if hasattr(dut, "last"):
        dut.last.value = 0
    return result


class WriteMonitor:
    """buf_we / buf_waddr / buf_wdata の書き込みを各立ち上がりで集める（番地 → 最後に書いた値、と書いた順）。"""

    def __init__(self, dut):
        self.dut = dut
        self.writes: list[tuple[int, int]] = []
        cocotb.start_soon(self._run())

    async def _run(self):
        while True:
            await RisingEdge(self.dut.clk)
            await ReadOnly()
            if int(self.dut.buf_we.value):
                self.writes.append((int(self.dut.buf_waddr.value), int(self.dut.buf_wdata.value)))

    def clear(self):
        self.writes.clear()

    def memory(self) -> dict[int, int]:
        return dict(self.writes)
