"""2026-11-03 のレビュー（ホストの通信）で確かめた不具合の試験（docs/reports/2026-11-03-cloud-A.md）。直す前はどれも落ちた。"""

import socket
import threading
import time

import pytest

from fprobe.eth_link import CH_STREAM, EthLink
import random

from fprobe import generated as g
from fprobe.frame_link import StreamBuffer
from fprobe.la import DeepAnalyzer, LaStream, StreamLinkLoss, _stream_buffer
from fprobe.session import link_buffer_bytes
from test_la_stream import FakeStreamDevice
from test_la_stream_link_loss import PEER, Board, LossyUdp, raw


def _wait(cond, timeout_s=2.0):
    deadline = time.monotonic() + timeout_s
    while not cond():
        if time.monotonic() > deadline:
            raise AssertionError("待ちきれない")
        time.sleep(0.005)


def test_a_datagram_lost_between_the_start_request_and_lastream_is_reported_not_shifted():
    # 前は DeepAnalyzer.stream が、開始の書き（la_deep.control = 1）の後に LaStream を作り、そこで StreamBuffer の基準を取った。
    # その間（lanes > 1 なら read32 の往復を含む）に届いて seq が飛んだデータグラムが基準に入り、見出しの無い流れでも
    # StreamLinkLoss にならず、ずれたサンプルを返し続けた。今は流れを頼む前に取った基準を渡す（buffer_start）
    data = raw(2000)
    board = Board(data, drop={1})
    baseline = _stream_buffer(board, reset_peak=True)     # 開始の書きの前（DeepAnalyzer.stream と同じ順）
    board._send_some(3)                                   # 0・2・3 が届く（1 は落ちた）: 流れを始めた直後の分
    _wait(lambda: board.link.lost_frames == 1 and board.link.stream_buffer().used_bytes == 3 * 512)
    stream = LaStream(board, False, 1e-8, 16, buffer_start=baseline)
    got = bytearray()
    try:
        with pytest.raises(StreamLinkLoss):
            for chunk in stream.chunks(0.0, timeout_ms=300):
                got += bytes(chunk.data)
    finally:
        board.close()
    # 仕様（host-link-correctness §3）: 「どれも、それまでに返した分は正しい」。返した分は流れの先頭の部分列でなければならない
    assert data.startswith(bytes(got)), f"欠けの後のずれたサンプルを {len(got)} バイト返した"


class _OrderDevice(FakeStreamDevice):
    """stream_buffer（欠けの数えの基準）と la_deep.control の書きの順を記録する。"""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.order: list[str] = []

    def stream_buffer(self, reset_peak=False):
        self.order.append("stream_buffer")
        return StreamBuffer(1 << 20, 0, 0, 0, 0.0, False, 0, 0, 0)

    def write32(self, address, value):
        if address == g.LA_DEEP_CONTROL and value & 1:
            self.order.append("start")
        super().write32(address, value)


def test_deep_analyzer_takes_the_loss_baseline_before_it_starts_the_stream():
    dev = _OrderDevice(bytes(16 * 64), rng=random.Random(1))
    stream = DeepAnalyzer(dev).stream(rle=False)
    stream.stop()
    assert dev.order[:2] == ["stream_buffer", "start"], dev.order


def test_lost_udp_frames_are_reported_while_the_stream_channel_takes_frames_after_tcp_opened():
    # TCP を開いた後に窓経由（frames）の道を選ぶと、ch 4 は UDP のフレームで届くのに、buffer() は backpressure を見て
    # lost_frames を 0 にする（UDP の seq の飛びが見えない。見出しの無い流れ（ps_read=False）で欠けを見逃す）
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    udp = LossyUdp()
    link = EthLink(udp, PEER, buffer_bytes=link_buffer_bytes())
    try:
        link.open_stream_tcp(port=srv.getsockname()[1], buffer_bytes=1 << 20)
        conn, _ = srv.accept()
        link.select_source(CH_STREAM, "frames")            # Device.select_stream_path(False)（窓経由）と同じ
        for seq in (0, 2):                                 # seq 1 が落ちた
            udp.q.put(bytes([CH_STREAM, seq]) + bytes(512))
        _wait(lambda: link.stream_buffer().used_bytes == 1024)
        assert link.lost_frames == 1
        assert link.stream_buffer().lost_frames == 1, "フレームで受けている道の落ちを 0 と返した"
    finally:
        link.close()
        srv.close()


class _SendFails(LossyUdp):
    """送れない（LAN のリンクが落ちている・経路が無い）UDP。受信のスレッドを止めるため stop で recv が失敗する。"""

    def __init__(self):
        super().__init__()
        self.stop = threading.Event()
        self.closed = False

    def sendto(self, data, address):
        raise OSError(101, "Network is unreachable")

    def recvfrom_into(self, buffer, nbytes=0):
        if self.stop.is_set():
            raise OSError("試験の後始末")
        return super().recvfrom_into(buffer, nbytes)

    def close(self):
        self.closed = True


def test_a_failing_first_send_does_not_leave_the_receive_thread_and_socket():
    sock = _SendFails()
    before = {t.ident for t in threading.enumerate()}
    try:
        with pytest.raises(OSError):
            EthLink(sock, PEER, buffer_bytes=link_buffer_bytes())
        time.sleep(0.1)
        left = [t for t in threading.enumerate() if t.ident not in before and t.name.startswith("fprobe-eth")]
        assert not left and sock.closed, f"受信のスレッド {[t.name for t in left]} とソケット（closed={sock.closed}）が残った"
    finally:
        sock.stop.set()


def test_reading_control_after_close_is_a_connection_error_not_an_unknown_execution_timeout():
    # close の後の CONTROL の受け取りは、2 秒待って TimeoutError（ControlChannel は「実行されたかは分からない」ControlTimeout に
    # する）になる。閉じたのは PC 自身なので、閉じた事実（ConnectionError）で返すべき
    link = EthLink(LossyUdp(), PEER, buffer_bytes=link_buffer_bytes())
    link.close()
    t0 = time.monotonic()
    with pytest.raises(ConnectionError):
        link.receive_packet(0, timeout_s=0.3)
    assert time.monotonic() - t0 < 0.2


def test_after_the_tcp_stream_dies_the_window_path_can_still_be_selected():
    # TCP の流れが切れると stream_tcp_open が False になり、Device.select_stream_path は何もしない。受け口は "stream" のまま
    # なので、窓経由（UDP のフレーム）で届くイベントのリングの読み出しを捨て、読み手は「TCP が止まった」で失敗し続ける
    from fprobe.device import Device
    from fprobe.transport import LinkTransport
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    udp = LossyUdp()
    link = EthLink(udp, PEER, buffer_bytes=link_buffer_bytes())
    try:
        link.open_stream_tcp(port=srv.getsockname()[1], buffer_bytes=1 << 20)
        conn, _ = srv.accept()
        conn.close()                                       # 中継が TCP を閉じた
        _wait(lambda: not link.stream_tcp_open)
        dev = object.__new__(Device)
        dev._transport = LinkTransport(link)
        dev.select_stream_path(False)                      # イベントのリングの読み出しの前（events.EventLog.read）
        udp.q.put(bytes([CH_STREAM, 0]) + bytes(64))       # 窓経由で届いたイベント
        time.sleep(0.2)
        assert link.read_bytes(CH_STREAM, 64, 0.1) == bytes(64)
    finally:
        link.close()
        srv.close()
