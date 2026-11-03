"""FrameLink（SerialLink・EthLink に共通の振り分け）: バイト列のチャンネルのリング（上限・捨てた数）、DAP の応答と要求の対応づけ
（時間切れの後の遅れた応答を次の要求の応答に読まない）。相手はメモリの上の偽のリンク（ハードウェア・ネットワーク不要）。"""

import random
import threading
import time

import pytest

from fprobe.frame_link import (BYTE_CHANNELS, CH_CONTROL, CH_DAP, CH_STREAM, CH_SWO, CH_UART0, ByteRing, DapDesync,
                               FrameLink)

BUF = {CH_SWO: 4096, CH_UART0: 4096, CH_STREAM: 4096}
FENCE_REPLY = bytes([0x00, 0x02, 0x00, 0x02])


class MemLink(FrameLink):
    """送ったフレームを sent に残し、受信は試験が _dispatch を直接呼ぶ。"""
    name = "mem"

    def __init__(self, buffer_bytes=BUF, on_send=None):
        super().__init__(buffer_bytes=buffer_bytes)
        self.sent: list[tuple[int, bytes]] = []
        self.on_send = on_send
        self._start()

    def _write_frame(self, ch, payload):
        self.sent.append((ch, payload))
        if self.on_send is not None:
            self.on_send(ch, payload)

    def _rx_loop(self):
        self._stop.wait()

    def _close_transport(self):
        pass


# --- ByteRing --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("seed", range(10))
def test_the_ring_keeps_the_byte_order_through_wraps_and_direct_writes(seed):
    rng = random.Random(seed)
    ring = ByteRing(rng.randint(1, 300))
    written, read = bytearray(), bytearray()
    counter = 0
    for _ in range(2000):
        op = rng.random()
        if op < 0.35 and ring.room:
            n = rng.randint(1, ring.room)
            data = bytes((counter + i) & 0xFF for i in range(n))
            counter += n
            ring.write(data)
            written += data
        elif op < 0.7:
            view = ring.reserve(rng.randint(1, 400))
            n = rng.randint(0, len(view))
            if rng.random() < 0.3 and ring.used:                        # 書きかけの間に読み手が読む
                read += ring.read(rng.randint(1, ring.used))
            data = bytes((counter + i) & 0xFF for i in range(n))
            counter += n
            view[:n] = data
            ring.commit(n)
            written += data
        else:
            read += ring.read(rng.randint(1, 500))
        assert 0 <= ring.used <= ring.capacity
    read += ring.read(ring.capacity)
    assert read == written


def test_clearing_while_a_writer_has_reserved_keeps_the_reserved_place():
    ring = ByteRing(16)
    ring.write(b"abcd")
    view = ring.reserve(100)
    ring.clear()                                                         # 読み手が捨てた（discard）
    view[:3] = b"xyz"
    ring.commit(3)
    assert ring.read(10) == b"xyz"


def test_the_ring_refuses_more_than_its_room():
    ring = ByteRing(4)
    ring.write(b"abc")
    with pytest.raises(ValueError):
        ring.write(b"de")
    with pytest.raises(ValueError):
        ByteRing(0)


# --- バイト列のチャンネル -------------------------------------------------------------------------------------

def test_every_byte_channel_limit_must_be_given_explicitly():
    with pytest.raises(TypeError):
        MemLink.__bases__[0]()                                          # 既定の暗黙の上限を持たない
    with pytest.raises(ValueError, match="buffer_bytes"):
        MemLink({CH_SWO: 10, CH_UART0: 10})                             # 1 つでも欠けたら断る


@pytest.mark.parametrize("ch", BYTE_CHANNELS)
def test_a_frame_that_does_not_fit_is_dropped_whole_and_counted(ch):
    with MemLink({**BUF, ch: 10}) as link:
        link._dispatch(ch, b"123456")
        link._dispatch(ch, memoryview(b"abcdefgh")[2:7])                 # 受け取りのバッファの一部（memoryview）も写して入れる
        link._dispatch(ch, b"zz")
        buf = link.buffer(ch)
        assert (buf.used_bytes, buf.dropped_frames, buf.dropped_bytes, buf.peak_bytes) == (8, 1, 5, 8)
        assert link.read_bytes(ch, 100) == b"123456zz"                   # 欠けの前後は順のまま（欠けは数えた事実で分かる）


def test_the_byte_channels_are_independent():
    with MemLink() as link:
        link._dispatch(CH_SWO, b"swo")
        link._dispatch(CH_UART0, b"uart")
        assert link.read_bytes(CH_UART0, 10) == b"uart" and link.read_bytes(CH_SWO, 10) == b"swo"
        assert link.read_bytes(CH_STREAM, 10, timeout_s=0.01) == b""


def test_frames_on_a_channel_received_with_flow_control_are_dropped_from_the_other_path():
    with MemLink() as link:
        link._set_backpressure(CH_STREAM, 1000)                          # TCP で受け始めた
        link._dispatch(CH_STREAM, b"udp")                                # UDP で届いた（TCP の並びのどこにも入らない）
        buf = link.buffer(CH_STREAM)
        assert (buf.used_bytes, buf.dropped_frames, buf.backpressure, buf.capacity_bytes) == (0, 1, True, 1000)


def test_the_reader_selects_which_path_the_stream_comes_from_and_the_other_is_dropped():
    # TCP を開いていても、窓経由の読み方（PYNQ の深いバッファの READ）はフレーム（UDP）で届く。読み手が頼む前に道を選び、
    # 選んだ道だけがリングに入る。選んでいない道は捨てて数える（2026-11-03 実機: 窓経由の読み出しが 0 バイトで止まった）
    def recv(view):
        view[:3] = b"tcp"
        return 3

    with MemLink() as link:
        link._set_backpressure(CH_STREAM, 1000)
        link.select_source(CH_STREAM, "frames")
        link._dispatch(CH_STREAM, b"udp")
        assert link._receive_into(CH_STREAM, recv, 100) == 3              # 受け取るが並びには入れない
        assert link.read_bytes(CH_STREAM, 3, 0.1) == b"udp"
        buf = link.buffer(CH_STREAM)
        assert (buf.dropped_frames, buf.dropped_bytes) == (1, 3)
        link.select_source(CH_STREAM, "stream")
        link._dispatch(CH_STREAM, b"udp")
        assert link._receive_into(CH_STREAM, recv, 100) == 3
        assert link.read_bytes(CH_STREAM, 3, 0.1) == b"tcp"
        buf = link.buffer(CH_STREAM)
        assert (buf.dropped_frames, buf.dropped_bytes) == (2, 6)


def test_the_stream_path_cannot_be_selected_before_it_is_open():
    with MemLink() as link, pytest.raises(RuntimeError):
        link.select_source(CH_STREAM, "stream")


def test_direct_receive_waits_for_room_and_counts_the_time():
    with MemLink() as link:
        link._set_backpressure(CH_STREAM, 8)

        def recv(view):
            view[:len(view)] = b"x" * len(view)
            return len(view)
        assert link._receive_into(CH_STREAM, recv, 100) == 8
        done = []
        t = threading.Thread(target=lambda: done.append(link._receive_into(CH_STREAM, recv, 100)))
        t.start()
        time.sleep(0.2)
        assert not done                                                  # 上限まで溜まっている: 読まれるまで受け取らない
        assert link.read_bytes(CH_STREAM, 3) == b"xxx"
        t.join(2)
        assert done == [3]
        buf = link.buffer(CH_STREAM)
        assert buf.full_count == 1 and buf.full_s >= 0.15 and buf.used_bytes == 8


# --- DAP の応答と要求 -------------------------------------------------------------------------------------

def test_a_late_reply_after_a_timeout_is_never_read_as_the_next_reply():
    with MemLink() as link:
        link.send(CH_DAP, b"\x05\x01")
        with pytest.raises(TimeoutError):
            link.receive_packet(CH_DAP, 0.05)
        with pytest.raises(DapDesync):                                   # 同期していない: 開き直すまで断る
            link.send(CH_DAP, b"\x05\x02")
        link._dispatch(CH_DAP, b"\x05\x00late")                          # 遅れて届いた応答
        with pytest.raises(DapDesync):
            link.receive_packet(CH_DAP, 0.05)
        assert link.dap_stats().stale == 1


def test_discard_fences_and_drops_every_reply_before_the_fence_reply():
    def board(ch, payload):
        if ch == CH_DAP and payload == bytes([0x00, 0xFF]):              # 柵: 前の要求の遅れた応答の後に柵の応答
            threading.Thread(target=lambda: (link._dispatch(CH_DAP, b"\x05\x00old"),
                                             link._dispatch(CH_DAP, FENCE_REPLY))).start()
    link = MemLink(on_send=board)
    try:
        link.send(CH_DAP, b"\x05\x01")
        with pytest.raises(TimeoutError):
            link.receive_packet(CH_DAP, 0.05)
        link.discard(CH_DAP, fence_timeout_s=2.0)
        st = link.dap_stats()
        assert st.synchronized and st.fences == 1 and st.stale == 1 and st.timeouts == 1
        link.send(CH_DAP, b"\x05\x02")
        link._dispatch(CH_DAP, b"\x05\x00new")
        assert link.receive_packet(CH_DAP, 1.0) == b"\x05\x00new"
    finally:
        link.close()


def test_discard_without_outstanding_requests_needs_no_fence():
    with MemLink() as link:
        link.discard(CH_DAP, fence_timeout_s=0.01)
        assert link.sent == [] and link.dap_stats().synchronized
        with pytest.raises(ValueError, match="fence_timeout_s"):
            link.discard(CH_DAP)                                         # 柵を待つ時間は明示する


def test_a_fence_that_gets_no_reply_is_a_timeout_and_stays_unsynchronized():
    with MemLink() as link:
        link.send(CH_DAP, b"\x05\x01")
        with pytest.raises(TimeoutError):
            link.receive_packet(CH_DAP, 0.05)
        with pytest.raises(TimeoutError, match="柵"):
            link.discard(CH_DAP, fence_timeout_s=0.05)
        assert not link.dap_stats().synchronized


def test_unsolicited_replies_are_dropped_so_the_queue_is_bounded_by_the_requests():
    with MemLink() as link:
        for _ in range(100):
            link._dispatch(CH_DAP, b"\x00junk")                          # 頼んでいない応答
        assert len(link._packets[CH_DAP]) == 0 and link.dap_stats().unsolicited == 100
        link.send(CH_DAP, b"\x00\x01")
        link.send(CH_DAP, b"\x00\x02")
        for k in range(5):
            link._dispatch(CH_DAP, bytes([0, k]))
        assert len(link._packets[CH_DAP]) == 2 and link.dap_stats().unsolicited == 103
        assert [link.receive_packet(CH_DAP, 1.0) for _ in range(2)] == [b"\x00\x00", b"\x00\x01"]


def test_control_replies_are_not_paired_by_the_link():
    # CONTROL は要求の tag を ControlChannel が照らす（リンクは組み立てて並べるだけ）
    with MemLink() as link:
        link._dispatch(CH_CONTROL, b"abc")
        assert link.receive_packet(CH_CONTROL, 1.0) == b"abc"
