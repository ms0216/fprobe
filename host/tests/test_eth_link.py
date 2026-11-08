"""ホストライブラリ: EthLink（UDP 上のフレーム・seq の飛びの検出）。相手はループバックの UDP ソケット（FPGA 役）。"""

import socket
import threading
import time

import pytest

from fprobe.eth_link import CH_BUNDLE, CH_CONTROL, CH_DAP, CH_RELAY_STATUS, CH_STREAM, CH_SWO, EthLink

from fprobe.session import link_buffer_bytes

BUF = link_buffer_bytes()


@pytest.fixture
def fpga():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(2)
    yield sock
    sock.close()


def test_frames_carry_the_channel_and_responses_are_dispatched(fpga):
    with EthLink.open("127.0.0.1", fpga.getsockname()[1], buffer_bytes=BUF) as link:
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
    with EthLink.open("127.0.0.1", fpga.getsockname()[1], buffer_bytes=BUF) as link:
        _, peer = fpga.recvfrom(2048)
        for seq in (250, 251, 254, 255, 2):                        # 252, 253, 0, 1 が落ちた
            fpga.sendto(bytes([CH_STREAM, seq, seq]), peer)
        data = b""
        while len(data) < 5:
            data += link.read_bytes(CH_STREAM, 5, timeout_s=2)
        assert data == bytes([250, 251, 254, 255, 2])
        assert link.lost_frames == 4


def read_all(link, ch, n):
    data = b""
    while len(data) < n:
        data += link.read_bytes(ch, n - len(data), timeout_s=2)
    return data


def bundle(seq, *frames):
    return bytes([CH_BUNDLE, seq]) + b"".join(bytes([ch]) + len(p).to_bytes(2, "little") + p for ch, p in frames)


def test_a_bundle_is_split_into_its_frames_with_consecutive_seq(fpga):
    with EthLink.open("127.0.0.1", fpga.getsockname()[1], buffer_bytes=BUF) as link:
        _, peer = fpga.recvfrom(2048)
        fpga.sendto(bundle(10, (CH_STREAM, b"abc"), (CH_SWO, b"xy"), (CH_STREAM, b"def")), peer)
        fpga.sendto(bytes([CH_STREAM, 13]) + b"g", peer)                 # 中の 3 つの次の seq は 13
        assert read_all(link, CH_STREAM, 7) == b"abcdefg"
        assert read_all(link, CH_SWO, 2) == b"xy"
        assert link.lost_frames == 0 and link.bad_frames == 0


def test_a_lost_bundle_counts_every_frame_in_it(fpga):
    with EthLink.open("127.0.0.1", fpga.getsockname()[1], buffer_bytes=BUF) as link:
        _, peer = fpga.recvfrom(2048)
        fpga.sendto(bundle(0, (CH_STREAM, b"a"), (CH_STREAM, b"b")), peer)
        fpga.sendto(bundle(5, (CH_STREAM, b"c"), (CH_STREAM, b"d")), peer)    # seq 2, 3, 4 が落ちた
        assert read_all(link, CH_STREAM, 4) == b"abcd"
        assert link.lost_frames == 3


def test_a_broken_bundle_keeps_the_frames_before_the_break_and_counts_one_bad(fpga):
    with EthLink.open("127.0.0.1", fpga.getsockname()[1], buffer_bytes=BUF) as link:
        _, peer = fpga.recvfrom(2048)
        broken = bundle(0, (CH_STREAM, b"ok")) + bytes([CH_STREAM, 9, 0]) + b"short"   # 長さ 9 と書いて 5 バイトしかない
        fpga.sendto(broken, peer)
        assert read_all(link, CH_STREAM, 2) == b"ok"
        assert link.bad_frames == 1


def test_request_bundle_sends_the_limit_to_the_board(fpga):
    with EthLink.open("127.0.0.1", fpga.getsockname()[1], buffer_bytes=BUF) as link:
        fpga.recvfrom(2048)
        link.request_bundle(1400)
        assert fpga.recvfrom(2048)[0] == bytes([CH_BUNDLE, 0]) + (1400).to_bytes(2, "little")
        link.request_bundle(0)
        assert fpga.recvfrom(2048)[0] == bytes([CH_BUNDLE, 0, 0, 0])
        with pytest.raises(ValueError):
            link.request_bundle(65536)


def test_a_bundle_larger_than_one_ethernet_frame_is_received(fpga):
    with EthLink.open("127.0.0.1", fpga.getsockname()[1], buffer_bytes=BUF) as link:
        _, peer = fpga.recvfrom(2048)
        frames = [(CH_STREAM, bytes([i]) * 500) for i in range(60)]              # 約 30 KB（IP の分割で届く大きさ）
        fpga.sendto(bundle(0, *frames), peer)
        assert read_all(link, CH_STREAM, 60 * 500) == b"".join(p for _, p in frames)
        assert link.lost_frames == 0 and link.bad_frames == 0


def test_open_with_bundle_bytes_asks_the_board_right_after_the_hello(fpga):
    with EthLink.open("127.0.0.1", fpga.getsockname()[1], bundle_bytes=1400, buffer_bytes=BUF):
        assert fpga.recvfrom(2048)[0] == bytes([CH_CONTROL, 0])
        assert fpga.recvfrom(2048)[0] == bytes([CH_BUNDLE, 0]) + (1400).to_bytes(2, "little")


def test_open_without_bundle_bytes_sends_only_the_hello(fpga):
    fpga.settimeout(0.3)
    with EthLink.open("127.0.0.1", fpga.getsockname()[1], buffer_bytes=BUF):
        fpga.recvfrom(2048)
        with pytest.raises(socket.timeout):
            fpga.recvfrom(2048)


class CountingCondition:
    """notify / notify_all の回数を数える Condition の包み（試験用）。"""

    def __init__(self, inner):
        self._inner, self.notifies = inner, 0

    def notify_all(self):
        self.notifies += 1
        self._inner.notify_all()

    def notify(self, n=1):
        self.notifies += 1
        self._inner.notify(n)

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def __enter__(self):
        return self._inner.__enter__()

    def __exit__(self, *a):
        return self._inner.__exit__(*a)


def test_stream_data_does_not_wake_a_reader_waiting_for_a_control_response(fpga):
    # 毎秒 1 万以上のストリームのデータグラムが届く間、CSR の応答を待つスレッドを毎回起こすと、起こされては眠るのを繰り返して
    # GIL を奪い合い、受信が詰まって制御の応答が時間切れになる（2026-10-30 に PYNQ の PS 直読みの 8 MS/s で実測）
    with EthLink.open("127.0.0.1", fpga.getsockname()[1], buffer_bytes=BUF) as link:
        _, peer = fpga.recvfrom(2048)
        link._chan_cond[CH_CONTROL] = control = CountingCondition(link._chan_cond[CH_CONTROL])
        got = []
        t = threading.Thread(target=lambda: got.append(link.receive_packet(CH_CONTROL, 5.0)))
        t.start()
        time.sleep(0.2)                                                  # 制御の応答を待ち始める
        for seq in range(200):
            fpga.sendto(bytes([CH_STREAM, seq]) + b"x" * 100, peer)
        data = b""
        deadline = time.monotonic() + 3
        while len(data) < 200 * 100 and time.monotonic() < deadline:
            time.sleep(0.01)
            with link._cond:
                data = b"x" * link.buffer(CH_STREAM).used_bytes
        assert len(data) == 200 * 100
        assert control.notifies == 0, control.notifies                   # ストリームでは制御の待ち人を起こさない
        fpga.sendto(bytes([CH_CONTROL, 200]), peer)                      # 制御の応答は、待ち人を起こす
        t.join(3)
        assert got == [b""]


def test_stream_data_does_not_notify_when_nobody_waits_for_the_stream(fpga):
    with EthLink.open("127.0.0.1", fpga.getsockname()[1], buffer_bytes=BUF) as link:
        _, peer = fpga.recvfrom(2048)
        link._chan_cond[CH_STREAM] = stream = CountingCondition(link._chan_cond[CH_STREAM])
        for seq in range(100):
            fpga.sendto(bytes([CH_STREAM, seq]) + b"x" * 100, peer)
        deadline = time.monotonic() + 3
        while link.buffer(CH_STREAM).used_bytes < 100 * 100 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert link.buffer(CH_STREAM).used_bytes == 100 * 100 and stream.notifies == 0


def test_a_waiting_reader_is_woken_by_arriving_data(fpga):
    with EthLink.open("127.0.0.1", fpga.getsockname()[1], buffer_bytes=BUF) as link:
        _, peer = fpga.recvfrom(2048)
        got = []
        t = threading.Thread(target=lambda: got.append(link.read_bytes(CH_STREAM, 10, timeout_s=3)))
        t.start()
        time.sleep(0.2)                                                  # 読み手が待ち始める
        fpga.sendto(bytes([CH_STREAM, 0]) + b"abc", peer)
        t.join(2)
        assert got == [b"abc"]
        fpga.sendto(bytes([CH_CONTROL, 1]), peer)                        # 制御の応答を待つ側も起こす
        assert link.receive_packet(CH_CONTROL, 2.0) == b""


def test_a_receive_failure_wakes_every_waiter(fpga):
    # 受信が止まったら（切断等）、どのチャンネルを待つ利用者にも伝える
    with EthLink.open("127.0.0.1", fpga.getsockname()[1], buffer_bytes=BUF) as link:
        fpga.recvfrom(2048)
        errors = []

        def wait(read):
            try:
                read()
            except ConnectionError as e:
                errors.append(e)

        threads = [threading.Thread(target=wait, args=(lambda: link.receive_packet(CH_CONTROL, 5.0),)),
                   threading.Thread(target=wait, args=(lambda: link.read_bytes(CH_STREAM, 10, 5.0),))]
        for t in threads:
            t.start()
        time.sleep(0.2)
        link._sock.close()                                               # 受信スレッドの recvfrom が失敗する（切断と同じ）
        for t in threads:
            t.join(3)
        assert len(errors) == 2


# --- PS 直読みのストリームを TCP で受ける（中継の 63921。制御と DAP は UDP のまま）------------------------------

@pytest.fixture
def tcp_board():
    """中継の TCP の口の役（ループバックの TCP の待ち受け）。"""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(3)
    yield listener
    listener.close()


def read_exactly(link, ch, n, timeout_s=5.0):
    data, deadline = b"", time.monotonic() + timeout_s
    while len(data) < n and time.monotonic() < deadline:
        data += link.read_bytes(ch, n - len(data), timeout_s=0.5)
    return data


def test_stream_bytes_over_tcp_arrive_in_order_through_the_same_read_bytes(fpga, tcp_board):
    with EthLink.open("127.0.0.1", fpga.getsockname()[1], buffer_bytes=BUF) as link:
        fpga.recvfrom(2048)
        link.open_stream_tcp(port=tcp_board.getsockname()[1], buffer_bytes=8 << 20)
        conn, _ = tcp_board.accept()
        payload = bytes(range(256)) * 8192                               # 2 MB
        sender = threading.Thread(target=lambda: [conn.sendall(payload[i:i + 65536]) for i in range(0, len(payload), 65536)])
        sender.start()
        assert read_exactly(link, CH_STREAM, len(payload)) == payload
        sender.join(3)
        conn.close()


def test_udp_frames_and_the_tcp_stream_do_not_disturb_each_other(fpga, tcp_board):
    with EthLink.open("127.0.0.1", fpga.getsockname()[1], buffer_bytes=BUF) as link:
        _, peer = fpga.recvfrom(2048)
        link.open_stream_tcp(port=tcp_board.getsockname()[1], buffer_bytes=8 << 20)
        conn, _ = tcp_board.accept()
        conn.sendall(b"TCP-BYTES")
        fpga.sendto(bytes([CH_CONTROL, 0]) + b"\x01\x02", peer)           # 制御の応答は UDP
        assert link.receive_packet(CH_CONTROL, 2.0) == b"\x01\x02"
        assert read_exactly(link, CH_STREAM, 9) == b"TCP-BYTES"
        conn.close()


def test_when_the_tcp_peer_closes_buffered_bytes_are_delivered_then_the_stream_read_fails(fpga, tcp_board):
    with EthLink.open("127.0.0.1", fpga.getsockname()[1], buffer_bytes=BUF) as link:
        fpga.recvfrom(2048)
        link.open_stream_tcp(port=tcp_board.getsockname()[1], buffer_bytes=8 << 20)
        conn, _ = tcp_board.accept()
        conn.sendall(b"last bytes")
        conn.close()
        assert read_exactly(link, CH_STREAM, 10) == b"last bytes"          # 届いた分は読める
        with pytest.raises(ConnectionError, match="TCP"):
            link.read_bytes(CH_STREAM, 1, timeout_s=3.0)
        # 制御（UDP）は、TCP が切れても使える
        link.send(CH_DAP, b"\x00")
        assert fpga.recvfrom(2048)[0] == bytes([CH_DAP, 0, 0])


def test_opening_the_tcp_stream_without_a_listener_is_an_error(fpga):
    unused = socket.socket()
    unused.bind(("127.0.0.1", 0))
    port = unused.getsockname()[1]
    unused.close()
    with EthLink.open("127.0.0.1", fpga.getsockname()[1], buffer_bytes=BUF) as link:
        fpga.recvfrom(2048)
        with pytest.raises(ConnectionError, match="TCP"):
            link.open_stream_tcp(port=port, timeout_s=0.5, buffer_bytes=8 << 20)


def test_closing_the_link_stops_the_tcp_reader_promptly(fpga, tcp_board):
    link = EthLink.open("127.0.0.1", fpga.getsockname()[1], buffer_bytes=BUF)
    fpga.recvfrom(2048)
    link.open_stream_tcp(port=tcp_board.getsockname()[1], buffer_bytes=8 << 20)
    conn, _ = tcp_board.accept()
    started = time.monotonic()
    link.close()
    assert time.monotonic() - started < 2.0
    assert not any(t.name.startswith("fprobe-eth-tcp") and t.is_alive() for t in threading.enumerate())
    conn.close()


def test_the_session_opens_the_tcp_stream_only_for_a_device_that_can_be_read_by_the_ps():
    from types import SimpleNamespace

    from caps_fakes import caps_for
    from fprobe.session import _maybe_open_stream_tcp

    calls = []
    link = SimpleNamespace(open_stream_tcp=lambda **kw: calls.append(kw))
    _maybe_open_stream_tcp(link, caps_for(la_stream_ps=True), True, 1234)
    assert calls == [{"buffer_bytes": 1234}]                               # 溜める上限は呼び手の値をそのまま渡す
    _maybe_open_stream_tcp(link, caps_for(la_stream_ps=False), True, 1234)
    _maybe_open_stream_tcp(link, caps_for(), True, 1234)                   # 申告（FEATURES の bit）が無い古い機器
    _maybe_open_stream_tcp(link, caps_for(la_stream_ps=True), False, 1234)   # 呼び手が断った
    assert calls == [{"buffer_bytes": 1234}]


def test_relay_status_takes_only_the_reply_to_its_own_request(fpga):
    # 問い合わせごとに番号を変え、その番号を写した返事だけを受ける。前の問い合わせの返事が遅れて届いても次の問い合わせの返事に
    # しない（A10。前は番号が無く、遅れた返事を次の返事にした）
    def reply(token, connections):
        return bytes([CH_RELAY_STATUS, token, 2, 0, 1, 1, 1, 0]) + connections.to_bytes(8, "little")

    with EthLink.open("127.0.0.1", fpga.getsockname()[1], buffer_bytes=BUF) as link:
        _, peer = fpga.recvfrom(2048)
        assert link.relay_status(timeout_s=0.2) is None                   # 返事なし（番号 1）
        assert fpga.recvfrom(2048)[0] == bytes([CH_RELAY_STATUS, 1])
        result = {}
        t = threading.Thread(target=lambda: result.update(link.relay_status(timeout_s=2) or {}))
        t.start()
        request = fpga.recvfrom(2048)[0]
        assert request == bytes([CH_RELAY_STATUS, 2])
        fpga.sendto(reply(1, 111), peer)                                  # 前の問い合わせの遅れた返事
        fpga.sendto(reply(0, 222), peer)                                  # 版 1 の中継（番号を写さない）
        fpga.sendto(reply(2, 333), peer)
        t.join()
        assert result["token"] == 2 and result["connections"] == 333
        assert link.lost_frames == 0                                      # 中継の返事は PL のフレームの通し番号に数えない


def test_a_large_tcp_stream_sent_in_odd_sized_pieces_arrives_whole_and_in_order(fpga, tcp_board):
    """受信の 1 回の読み（_TCP_RECV = 1 MB）より大きな流れを、読みの境目と合わない大きさ（素数）で送っても、欠け・重なり・順序の乱れが無い。"""
    import random
    rng = random.Random(7)
    payload = rng.randbytes(9 << 20)                                     # 9 MB
    with EthLink.open("127.0.0.1", fpga.getsockname()[1], buffer_bytes=BUF) as link:
        fpga.recvfrom(2048)
        link.open_stream_tcp(port=tcp_board.getsockname()[1], buffer_bytes=2 << 20)    # 流れより小さい上限: 止めながら受ける
        conn, _ = tcp_board.accept()

        def send():
            pos = 0
            while pos < len(payload):
                n = rng.choice((1, 4093, 65537, 1 << 20, 1048583))
                conn.sendall(payload[pos:pos + n])
                pos += n
        sender = threading.Thread(target=send)
        sender.start()
        received = bytearray()
        deadline = time.monotonic() + 20
        while len(received) < len(payload) and time.monotonic() < deadline:
            received += link.read_bytes(CH_STREAM, 3 << 20, timeout_s=0.5)
        sender.join(3)
        conn.close()
        assert len(received) == len(payload)
        assert received == payload


def test_stream_segments_split_across_tcp_reads_are_found_at_their_boundaries(fpga, tcp_board):
    """見出し付きのかたまりが TCP の読みの境目（1 バイトずつ・かたまりの途中）をまたいで届いても、SegmentSplitter が境目を違えない。"""
    import struct

    from fprobe import generated as g
    from fprobe.la_segments import SegmentSplitter
    segments = [struct.pack("<HHIQ", g.STREAM_SEG_LA, 256, i * 256, g.STREAM_SEG_MAGIC) + bytes([i & 0xFF]) * 4096 for i in range(300)]
    stream = b"".join(segments)
    with EthLink.open("127.0.0.1", fpga.getsockname()[1], buffer_bytes=BUF) as link:
        fpga.recvfrom(2048)
        link.open_stream_tcp(port=tcp_board.getsockname()[1], buffer_bytes=8 << 20)
        conn, _ = tcp_board.accept()

        def send():
            conn.sendall(stream[:5])                                     # 見出しの途中で切る
            conn.sendall(stream[5:4112 + 7])                              # 次の見出しの途中まで
            time.sleep(0.05)
            for i in range(4112 + 7, len(stream), 6151):
                conn.sendall(stream[i:i + 6151])
        sender = threading.Thread(target=send)
        sender.start()
        splitter, seen, got = SegmentSplitter(), [], 0
        deadline = time.monotonic() + 10
        while got < len(stream) and time.monotonic() < deadline:
            data = link.read_bytes(CH_STREAM, 1 << 20, timeout_s=0.2)
            got += len(data)
            seen += [(s.seq, bytes(s.data)) for s in splitter.feed(data)]
        sender.join(3)
        conn.close()
        assert got == len(stream) and not splitter.pending
        assert seen == [(i * 256, bytes([i & 0xFF]) * 4096) for i in range(300)]
