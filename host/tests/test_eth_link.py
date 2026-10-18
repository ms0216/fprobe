"""ホストライブラリ: EthLink（UDP 上のフレーム・seq の飛びの検出）。相手はループバックの UDP ソケット（FPGA 役）。"""

import socket

import pytest

from fprobe.eth_link import CH_CONTROL, CH_DAP, CH_STREAM, EthLink


@pytest.fixture
def fpga():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(2)
    yield sock
    sock.close()


def test_frames_carry_the_channel_and_responses_are_dispatched(fpga):
    with EthLink.open("127.0.0.1", fpga.getsockname()[1]) as link:
        hello, peer = fpga.recvfrom(2048)
        assert hello == bytes([CH_CONTROL, 0])                     # 送信先を知らせる空の CONTROL
        link.send(CH_DAP, b"\x00\x01")
        assert fpga.recvfrom(2048)[0] == bytes([CH_DAP, 0, 0, 1])
        fpga.sendto(bytes([CH_CONTROL, 0]) + bytes(512), peer)       # 512 の倍数の応答は空のフレームで終わる
        fpga.sendto(bytes([CH_CONTROL, 1]), peer)
        fpga.sendto(bytes([CH_DAP, 2, 7]), peer)
        assert link.receive_packet(CH_CONTROL) == bytes(512)
        assert link.receive_packet(CH_DAP) == b"\x07"
        assert link.lost_frames == 0


def test_gaps_in_the_sequence_are_counted(fpga):
    with EthLink.open("127.0.0.1", fpga.getsockname()[1]) as link:
        _, peer = fpga.recvfrom(2048)
        for seq in (250, 251, 254, 255, 2):                        # 252, 253, 0, 1 が落ちた
            fpga.sendto(bytes([CH_STREAM, seq, seq]), peer)
        data = b""
        while len(data) < 5:
            data += link.read_bytes(CH_STREAM, 5, timeout_s=2)
        assert data == bytes([250, 251, 254, 255, 2])
        assert link.lost_frames == 4
