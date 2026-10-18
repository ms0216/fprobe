"""USB 2.0 パケットの参照モデル（テスト専用）。

RTL の期待値計算とホストモデルの両方が使う。値の根拠は docs/reference/usb2-hs-device-on-ulpi.md §4。
"""

from dataclasses import dataclass

# PID（4 ビット値）
OUT, IN, SOF, SETUP = 0x1, 0x9, 0x5, 0xD
DATA0, DATA1, DATA2, MDATA = 0x3, 0xB, 0x7, 0xF
ACK, NAK, STALL, NYET = 0x2, 0xA, 0xE, 0x6
PING = 0x4

TOKEN_PIDS = {OUT, IN, SOF, SETUP, PING}
DATA_PIDS = {DATA0, DATA1, DATA2, MDATA}
HANDSHAKE_PIDS = {ACK, NAK, STALL, NYET}

CRC5_RESIDUAL = 0b01100
CRC16_RESIDUAL = 0xB001     # 反射形レジスタでの剰余（仕様の 0x800D のビット反転）


class PacketError(ValueError):
    """PID・長さ・CRC のいずれかが不正。"""


def pid_byte(pid: int) -> int:
    return ((~pid & 0xF) << 4) | pid


def _crc5_register(bits) -> int:
    crc = 0x1F
    for bit in bits:
        feedback = ((crc >> 4) & 1) ^ bit
        crc = (crc << 1) & 0x1F
        if feedback:
            crc ^= 0x05
    return crc


def crc5(value11: int) -> int:
    """11 ビット（ADDR | ENDP<<7、LSB から送出）の CRC5（反転済み、ビット 4 が最初に送られる）。"""
    return ~_crc5_register((value11 >> i) & 1 for i in range(11)) & 0x1F


def _token_bytes(pid: int, value11: int) -> bytes:
    c = crc5(value11)
    reversed_crc = int(f"{c:05b}"[::-1], 2)        # CRC はビット 4 から送るので LSB 側へ逆順に詰める
    field = value11 | (reversed_crc << 11)
    return bytes([pid_byte(pid), field & 0xFF, field >> 8])


def token(pid: int, addr: int, endp: int) -> bytes:
    return _token_bytes(pid, addr | (endp << 7))


def sof(frame: int) -> bytes:
    return _token_bytes(SOF, frame & 0x7FF)


def crc5_residual(packet: bytes) -> int:
    """トークンの 2 バイト目以降 16 ビットを線上の順に通した CRC5 レジスタ値。"""
    field = packet[1] | (packet[2] << 8)
    return _crc5_register((field >> i) & 1 for i in range(16))


def crc16_register(data: bytes, crc: int = 0xFFFF) -> int:
    """反射形 CRC16 レジスタ（最終 XOR 前）。"""
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0xA001 if crc & 1 else 0)
    return crc


def crc16(data: bytes) -> int:
    return crc16_register(data) ^ 0xFFFF


def data_packet(pid: int, payload: bytes) -> bytes:
    c = crc16(payload)
    return bytes([pid_byte(pid)]) + bytes(payload) + bytes([c & 0xFF, c >> 8])


def handshake(pid: int) -> bytes:
    return bytes([pid_byte(pid)])


@dataclass(frozen=True)
class Token:
    pid: int
    addr: int
    endp: int


@dataclass(frozen=True)
class Sof:
    frame: int


@dataclass(frozen=True)
class Data:
    pid: int
    payload: bytes


@dataclass(frozen=True)
class Handshake:
    pid: int


def parse(packet: bytes):
    if not packet:
        raise PacketError("空のパケット")
    first = packet[0]
    pid = first & 0xF
    if (first >> 4) != (~pid & 0xF):
        raise PacketError(f"PID チェック不一致: {first:#04x}")
    if pid in TOKEN_PIDS:
        if len(packet) != 3:
            raise PacketError(f"トークン長 {len(packet)}")
        if crc5_residual(packet) != CRC5_RESIDUAL:
            raise PacketError("CRC5 不一致")
        field = packet[1] | (packet[2] << 8)
        if pid == SOF:
            return Sof(field & 0x7FF)
        return Token(pid, field & 0x7F, (field >> 7) & 0xF)
    if pid in DATA_PIDS:
        if len(packet) < 3:
            raise PacketError(f"データ長 {len(packet)}")
        if crc16_register(packet[1:]) != CRC16_RESIDUAL:
            raise PacketError("CRC16 不一致")
        return Data(pid, bytes(packet[1:-2]))
    if pid in HANDSHAKE_PIDS:
        if len(packet) != 1:
            raise PacketError(f"ハンドシェイク長 {len(packet)}")
        return Handshake(pid)
    raise PacketError(f"未対応 PID {pid:#x}")
