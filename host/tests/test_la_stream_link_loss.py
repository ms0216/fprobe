"""リンク（UDP）で流れが欠けたとき、LaStream は黙ってサンプルの位置をずらさず誤りにする（A4・D3）。

相手はメモリの上の偽の UDP ソケット（データグラムを落とせる）と偽の機器の CSR。本物の EthLink・LinkTransport・LaStream を通す。
ネットワーク・ハードウェア不要。"""

import queue
import random
import socket
from array import array

import pytest

from fprobe import generated as g
from fprobe.eth_link import CH_STREAM, EthLink
from fprobe.la import LaStream, StreamFormatError, StreamLinkLoss
from fprobe.session import link_buffer_bytes
from fprobe.transport import LinkTransport
from test_la_stream_events import header
from caps_fakes import FakeDeviceBase, caps_for

PEER = ("127.0.0.1", 63920)
BEAT = 16


class LossyUdp:
    """機器の役の UDP: deliver で並べたデータグラムを recvfrom_into で返す（drop の番号のデータグラムは落とす）。"""

    def __init__(self):
        self.q: queue.Queue = queue.Queue()

    def sendto(self, data, address):
        return len(data)

    def recvfrom_into(self, buffer, nbytes=0):
        try:
            data = self.q.get(timeout=0.05)
        except queue.Empty:
            raise socket.timeout() from None
        buffer[:len(data)] = data
        return len(data), PEER

    def settimeout(self, _):
        pass

    def close(self):
        pass


class Board(FakeDeviceBase):
    """LaStream の相手: 流れ全体を 512 バイトのフレームにして UDP で送る（seq 付き）。CSR の stream_beats は全体。
    status の reading は、送るべきデータグラムを全部並べるまで 1。"""

    def __init__(self, stream: bytes, *, drop=(), buffer_bytes=None, events=False):
        self.udp = LossyUdp()
        self.link = EthLink(self.udp, PEER, buffer_bytes=buffer_bytes or link_buffer_bytes())
        self.transport = LinkTransport(self.link)
        self.stream, self.events = stream, events
        self.frames = [stream[i:i + 512] for i in range(0, len(stream), 512)]
        self.drop = set(drop)
        self.sent = 0
        self.running = True

    def close(self):
        self.link.close()

    def _send_some(self, n):
        """n 個のデータグラムを並べる（落とすものは数えない: 見回りのたびに何かは届く）。"""
        while n and self.sent < len(self.frames):
            k = self.sent
            self.sent += 1
            if k not in self.drop:
                self.udp.q.put(bytes([CH_STREAM, k & 0xFF]) + self.frames[k])
                n -= 1

    # --- Device の代わり ----------------------------------------------------------------
    def capabilities(self):
        return caps_for(mem_bytes=128 << 20, la_channels=16, la_sample_hz=100_000_000, la_stream_events=self.events)

    def read(self, address, count, *, timeout_retries=0):
        out = []
        for a in range(address, address + 4 * count, 4):
            if a == g.LA_DEEP_STATUS:
                self._send_some(40)
                out.append(int(self.sent < len(self.frames)) << g.LA_DEEP_STATUS_READING[0])
            elif a == g.LA_DEEP_STREAM_BEATS:
                out.append(len(self.stream) // BEAT)
            else:
                out.append(0)
        return out

    def write32(self, address, value):
        if address == g.LA_DEEP_CONTROL and not value & 1:
            self.running = False

    def read_stream_some(self, size, timeout_ms=2000):
        return self.transport.read_stream_some(size, timeout_ms)

    def stream_buffer(self, reset_peak=False):
        return self.link.stream_buffer(reset_peak=reset_peak)


def raw(beats, seed=1):
    rng = random.Random(seed)
    return array("H", (rng.randrange(1 << 16) for _ in range(beats * 8))).tobytes()


def run(board, events=None, timeout_ms=300, duration_s=0.0):
    stream = LaStream(board, False, 1e-8, 16, events=events)
    got = bytearray()
    try:
        for chunk in stream.chunks(duration_s, timeout_ms=timeout_ms):
            got += bytes(chunk.data)
    finally:
        board.close()
    return stream, bytes(got)


def test_a_stream_without_losses_arrives_whole_over_udp():
    data = raw(2000)
    stream, got = run(Board(data))
    assert got == data
    lb = stream.stats().link_buffer
    assert (lb.dropped_frames, lb.lost_frames, lb.backpressure) == (0, 0, False)


def test_a_dropped_datagram_in_a_raw_stream_is_an_error_not_a_silent_shift():
    data = raw(2000)
    board = Board(data, drop={17})
    with pytest.raises(StreamLinkLoss, match="落ちた") as info:
        run(board)
    assert info.value.lost["lost_frames"] == 1


def test_a_dropped_datagram_in_a_headed_stream_is_caught_by_the_headers():
    rng = random.Random(2)
    out, la = bytearray(), raw(3000, 2)
    for i in range(0, 3000, 100):
        out += header(g.STREAM_SEG_LA, 100, i) + la[i * BEAT:(i + 100) * BEAT]
    board = Board(bytes(out), drop={rng.randrange(10, 80)}, events=True)
    with pytest.raises(StreamFormatError):
        run(board, events=(0, 1 << 19))


def test_frames_dropped_because_the_link_buffer_is_full_stop_the_stream():
    data = raw(4000)
    board = Board(data, buffer_bytes={**link_buffer_bytes(), CH_STREAM: 4096})
    for _ in range(3):
        board._send_some(40)                                            # 読み手より先に 20 KB が届く（4 KB の上限を超える）
    with pytest.raises(StreamLinkLoss, match="捨てた") as info:
        run(board)
    assert info.value.lost["dropped_frames"] > 0


def test_256_lost_datagrams_that_the_8_bit_seq_cannot_see_are_caught_at_the_end():
    # seq は 8 ビット: ちょうど 256 個続けて落ちると飛びが見えない（lost_frames は増えない）。出し切った後、機器が書いたと言う量まで
    # 届かないことで欠けを知る
    data = raw(20000)
    board = Board(data, drop=set(range(100, 356)))
    with pytest.raises(StreamLinkLoss, match="届かない"):
        run(board, timeout_ms=200)
    assert board.link.lost_frames == 0


def test_a_failing_stop_write_does_not_hide_the_original_error():
    class Broken(Board):
        def write32(self, address, value):
            raise OSError("切れた")
    board = Broken(raw(2000), drop={5})
    with pytest.raises(StreamLinkLoss) as info:
        run(board, duration_s=30.0)                                     # 止める前に欠けに気づく
    assert any("止める書き込み" in note for note in info.value.__notes__)
