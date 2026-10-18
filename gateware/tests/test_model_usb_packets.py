"""USB パケットモデルの自己検証（技術メモ §4 の既知ベクタ）。"""

import pytest

from tests.models import usb_packets as up


@pytest.mark.parametrize("pid, wire", [
    (up.SETUP, 0x2D), (up.IN, 0x69), (up.OUT, 0xE1), (up.SOF, 0xA5), (up.PING, 0xB4),
    (up.DATA0, 0xC3), (up.DATA1, 0x4B), (up.ACK, 0xD2), (up.NAK, 0x5A), (up.STALL, 0x1E),
    (up.NYET, 0x96),
])
def test_pid_byte(pid, wire):
    assert up.pid_byte(pid) == wire


@pytest.mark.parametrize("pid, addr, endp, wire", [
    (up.SETUP, 0x00, 0x0, bytes.fromhex("2D0010")),
    (up.IN, 0x15, 0xE, bytes.fromhex("6915EF")),
    (up.IN, 0x01, 0x1, bytes.fromhex("698158")),
])
def test_token_vectors(pid, addr, endp, wire):
    assert up.token(pid, addr, endp) == wire


def test_sof_vector():
    assert up.sof(0x710) == bytes.fromhex("A5102F")


def test_crc16_check_value():
    assert up.crc16(b"123456789") == 0xB4C8


def test_data_packet_vectors():
    assert up.data_packet(up.DATA0, b"") == bytes.fromhex("C30000")
    assert up.data_packet(up.DATA1, b"") == bytes.fromhex("4B0000")
    assert up.data_packet(up.DATA0, bytes([1, 2, 3, 4]))[-2:] == bytes.fromhex("5ED4")


def test_receive_residuals():
    assert up.crc5_residual(up.token(up.IN, 0x15, 0xE)) == up.CRC5_RESIDUAL == 0b01100
    packet = up.data_packet(up.DATA1, bytes(range(100)))
    assert up.crc16_register(packet[1:]) == up.CRC16_RESIDUAL == 0xB001


def test_parse_round_trip():
    assert up.parse(up.token(up.OUT, 5, 3)) == up.Token(up.OUT, 5, 3)
    assert up.parse(up.sof(1234)) == up.Sof(1234)
    assert up.parse(up.data_packet(up.DATA1, b"abc")) == up.Data(up.DATA1, b"abc")
    assert up.parse(up.handshake(up.NAK)) == up.Handshake(up.NAK)


@pytest.mark.parametrize("corrupt", [
    bytes.fromhex("2D0011"),             # CRC5 不一致
    bytes.fromhex("2C0010"),             # PID チェックニブル不一致
    bytes.fromhex("C3010000"),           # CRC16 不一致
])
def test_parse_rejects_corruption(corrupt):
    with pytest.raises(up.PacketError):
        up.parse(corrupt)
