"""PS 直読みの TCP のストリームの受け側の溜まりすぎ（設計: docs/superpowers/specs/2026-11-01-bandwidth-observation-design.md 1.7.7）。

消費側が遅れたとき、受信のスレッドは溜める上限（open_stream_tcp の buffer_bytes）で recv を止める。カーネルの受信バッファが
詰まり、TCP の窓で送り手が止まる。送り手（中継）が DDR のリングから読めなくなり、リングが溢れたら PL が overrun を立てる
（今の仕組み）。黙って捨てない。溜まり具合は stream_buffer で事実として返す。

相手はループバックの UDP（FPGA の役）と TCP（中継の役）。ハードウェア不要。
"""

import socket
import threading
import time
from array import array

import pytest

from fprobe import eth_link
from fprobe import generated as g
from fprobe.eth_link import CH_STREAM, EthLink
from fprobe.la import DeepAnalyzer
from fprobe.transport import LinkTransport

from fprobe.session import link_buffer_bytes
from caps_fakes import FakeDeviceBase, caps_for

BUF = link_buffer_bytes()

BEAT = 16


@pytest.fixture
def small_kernel_buffer(monkeypatch):
    """PC のカーネルの受信バッファを小さくする（送り手が止まるまでの量を試験の時間に収める）。"""
    monkeypatch.setattr(eth_link, "_RCVBUF", 256 << 10)


@pytest.fixture
def fpga():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(2)
    yield sock
    sock.close()


@pytest.fixture
def tcp_board():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(3)
    yield listener
    listener.close()


def open_link(fpga, tcp_board, buffer_bytes):
    link = EthLink.open("127.0.0.1", fpga.getsockname()[1], buffer_bytes=BUF)
    fpga.recvfrom(2048)
    link.open_stream_tcp(port=tcp_board.getsockname()[1], buffer_bytes=buffer_bytes)
    conn, _ = tcp_board.accept()
    conn.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 64 << 10)
    return link, conn


class Sender:
    """送り手（中継の役）: payload を 64 KB ずつ、止まる（ブロックする）send で送り、送れた量を数える。"""

    def __init__(self, conn, payload):
        self.conn, self.payload, self.sent = conn, payload, 0
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        view = memoryview(self.payload)
        try:
            while self.sent < len(self.payload):
                self.sent += self.conn.send(view[self.sent:self.sent + (64 << 10)])
        except OSError:
            pass


# --- リンク: 上限で受信を止め、送り手が流量制御で止まる -------------------------------------------------------------

def test_a_consumer_that_does_not_read_stops_the_buffer_at_the_limit_and_the_sender_by_flow_control(
        small_kernel_buffer, fpga, tcp_board):
    limit = 1 << 20
    payload = bytes(range(256)) * (128 << 10)                       # 32 MB（上限 + カーネルのバッファより十分大きい）
    link, conn = open_link(fpga, tcp_board, limit)
    try:
        sender = Sender(conn, payload)
        time.sleep(1.0)                                              # 消費側は読まない
        first = sender.sent
        time.sleep(0.5)
        buf = link.stream_buffer()
        assert buf.capacity_bytes == limit
        assert buf.used_bytes == limit                               # 上限まで溜めて、それ以上は受け取らない
        assert buf.peak_bytes == limit
        assert sender.sent == first < len(payload)                   # 送り手は止まっている（TCP の窓が閉じた）
        assert first < 16 << 20                                      # 上限 + カーネルのバッファ + 送りのバッファ程度
        assert buf.full_count >= 1 and buf.full_s > 0.3              # 止めた回数・時間（事実）

        got, deadline = bytearray(), time.monotonic() + 20
        while len(got) < len(payload) and time.monotonic() < deadline:
            got += link.read_bytes(CH_STREAM, 4 << 20, timeout_s=0.5)
        assert bytes(got) == payload                                 # 読み始めれば全部、順に届く（黙って捨てない）
        sender.thread.join(5)
        assert sender.sent == len(payload)
        assert link.stream_buffer().peak_bytes <= limit
    finally:
        conn.close()
        link.close()


def test_the_buffer_limit_is_given_explicitly_by_the_caller(fpga, tcp_board):
    link = EthLink.open("127.0.0.1", fpga.getsockname()[1], buffer_bytes=BUF)
    try:
        fpga.recvfrom(2048)
        with pytest.raises(TypeError):
            link.open_stream_tcp(port=tcp_board.getsockname()[1])          # 既定の暗黙の値を持たない
        for bad in (0, -1):
            with pytest.raises(ValueError):
                link.open_stream_tcp(port=tcp_board.getsockname()[1], buffer_bytes=bad)
    finally:
        link.close()


def test_without_the_tcp_stream_the_buffer_is_bounded_and_drops_are_counted(fpga):
    # UDP の流れは止められない（流量制御が無い）。上限は使う側が開くときに渡した値で、超えたフレームは丸ごと捨てて数える
    small = {**BUF, CH_STREAM: 1000}
    with EthLink.open("127.0.0.1", fpga.getsockname()[1], buffer_bytes=small) as link:
        _, peer = fpga.recvfrom(2048)
        buf = link.stream_buffer()
        assert (buf.capacity_bytes, buf.backpressure) == (1000, False)
        assert (buf.used_bytes, buf.peak_bytes, buf.full_count, buf.full_s, buf.dropped_frames) == (0, 0, 0, 0.0, 0)
        for seq in range(3):
            fpga.sendto(bytes([CH_STREAM, seq]) + bytes([seq]) * 400, peer)
        deadline = time.monotonic() + 3
        while link.frames + link.stream_buffer().dropped_frames < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        buf = link.stream_buffer()
        assert (buf.used_bytes, buf.dropped_frames, buf.dropped_bytes) == (800, 1, 400)
        assert link.read_bytes(CH_STREAM, 2000) == bytes(400) + bytes([1]) * 400     # 入った分は順に、欠けの後ろは捨てた


def test_a_reader_waiting_for_room_does_not_delay_closing(small_kernel_buffer, fpga, tcp_board):
    link, conn = open_link(fpga, tcp_board, 64 << 10)
    sender = Sender(conn, bytes(8 << 20))
    time.sleep(0.5)
    assert link.stream_buffer().used_bytes == 64 << 10
    started = time.monotonic()
    link.close()
    assert time.monotonic() - started < 2.0
    assert not any(t.name.startswith("fprobe-eth-tcp") and t.is_alive() for t in threading.enumerate())
    conn.close()
    sender.thread.join(2)


def test_the_peak_can_be_restarted_for_a_new_measurement(small_kernel_buffer, fpga, tcp_board):
    link, conn = open_link(fpga, tcp_board, 1 << 20)
    try:
        conn.sendall(bytes(300 << 10))
        deadline = time.monotonic() + 3
        while link.stream_buffer().used_bytes < 300 << 10 and time.monotonic() < deadline:
            time.sleep(0.01)
        got = 0
        while got < 300 << 10:
            got += len(link.read_bytes(CH_STREAM, 1 << 20, timeout_s=0.5))
        assert link.stream_buffer().peak_bytes == 300 << 10
        restarted = link.stream_buffer(reset_peak=True)
        assert restarted.peak_bytes == 300 << 10                     # 戻す前の値を返す
        assert link.stream_buffer().peak_bytes == 0                  # 今の使用量（0）から測り直す
    finally:
        conn.close()
        link.close()


# --- 端から端: 遅い消費者 → 中継が止まる → リングが溢れて overrun（今の仕組み）--------------------------------------

_PATTERN = array("H", range(1 << 16)).tobytes() * 2                 # 8192 拍で一巡り。2 巡り分を切り出しに使う


def counting_beats(first, count):
    """拍 first から count 拍（1 拍 = 8 サンプル、サンプル i の値は i の下位 16 ビット。count ≤ 8192）。"""
    start = first % 8192 * BEAT
    return _PATTERN[start:start + count * BEAT]


class RingAndRelay(FakeDeviceBase):
    """PL（DDR のリングに書く）と、PS の中継（リングを読んで TCP で送る）の役。CSR は Device の代わりに返す。

    - PL: 走っている間、1 ms ごとに per_ms 拍を書く。未読（書いた − 中継が読んだ）が ring − 256 拍を超えそうなら、
      入るだけ書いて overrun を立て、書き込みを止める（今の PL と同じ）。
    - 中継: リングから最大 768 拍を読み、出すと決めた拍の総数を STREAM_BEATS に書いてから、止まる send で TCP に送る。
      TCP の窓が閉じれば send で止まり、リングを読めない。send で止まっていた時間を数える。
    """

    def __init__(self, conn, link, *, ring_beats, per_ms):
        self.conn, self.link, self.ring, self.per_ms = conn, link, ring_beats, per_ms
        self.transport = LinkTransport(link)
        self.written = self.consumed = self.stream_beats = 0
        self.running = self.overrun = False
        self.sending = False
        self.overrun_event = threading.Event()              # PL が溢れを立てた（試験の同期点。時間で待たない）
        self.unread_at_overrun = None                       # そのときの未読（書いた − 中継が読んだ）の拍数
        self.control_writes = []
        self._lock = threading.Lock()
        self._quit = threading.Event()
        self._threads = [threading.Thread(target=f, daemon=True) for f in (self._pl, self._relay)]
        for t in self._threads:
            t.start()

    def close(self):
        self._quit.set()
        self.conn.close()
        for t in self._threads:
            t.join(3)

    # --- Device の代わり ----------------------------------------------------------------------

    def capabilities(self):
        return caps_for(mem_bytes=128 << 20, la_channels=16, la_sample_hz=100_000_000, la_stream_ps=True)

    stream_tcp_open = True

    def write(self, address, words):
        pass

    def write32(self, address, value):
        if address != g.LA_DEEP_CONTROL:
            return
        self.control_writes.append(value)
        with self._lock:
            run = bool(value & 1)
            if run and not self.running:
                assert value >> g.LA_DEEP_CONTROL_PS_READ[0] & 1
                self.written = self.consumed = self.stream_beats = 0
                self.overrun = False
            self.running = run

    def read32(self, address):
        with self._lock:
            if address == g.LA_DEEP_RING_BEATS:
                return self.ring
            if address == g.MEM_STATUS:
                return 1 << g.MEM_STATUS_READY[0]
            if address == g.LA_DEEP_STREAM_BEATS:
                return self.stream_beats
            if address == g.LA_DEEP_STATUS:
                reading = self.running or self.consumed < self.written or self.sending
                return int(self.overrun) << g.LA_DEEP_STATUS_OVERRUN[0] | int(reading) << g.LA_DEEP_STATUS_READING[0]
        raise AssertionError(f"想定外の読み 0x{address:X}")

    def read(self, address, count, *, timeout_retries=0):
        """CONTROL の READ（番地の順に 1 語ずつ。この役が持たない la_deep の語は 0）。"""
        known = (g.LA_DEEP_RING_BEATS, g.MEM_STATUS, g.LA_DEEP_STREAM_BEATS, g.LA_DEEP_STATUS)
        return [self.read32(a) if a in known else 0 for a in range(address, address + 4 * count, 4)]

    def read_stream_some(self, size, timeout_ms=2000):
        return self.transport.read_stream_some(size, timeout_ms)

    def stream_buffer(self, reset_peak=False):
        return self.link.stream_buffer(reset_peak=reset_peak)

    # --- PL と中継 -------------------------------------------------------------------------------

    def _pl(self):
        while not self._quit.is_set():
            time.sleep(0.001)
            with self._lock:
                if not self.running or self.overrun:
                    continue
                room = self.ring - 256 - (self.written - self.consumed)
                n = min(self.per_ms, max(0, room))
                if n < self.per_ms:
                    self.overrun = True
                    self.unread_at_overrun = self.written - self.consumed
                    self.overrun_event.set()
                self.written += n

    def _relay(self):
        while not self._quit.is_set():
            with self._lock:
                take = min(768, self.written - self.consumed)
                first = self.consumed
                if take:
                    self.consumed += take
                    self.stream_beats += take
                    self.sending = True
            if not take:
                time.sleep(0.0005)
                continue
            try:
                self.conn.sendall(counting_beats(first, take))
            except OSError:
                return
            finally:
                with self._lock:
                    self.sending = False


def test_a_slow_consumer_stops_the_relay_and_the_ring_overflow_is_recorded(small_kernel_buffer, fpga, tcp_board):
    """消費側が止まっている間に、PC の溜まりが上限で止まり → カーネルの窓が詰まり → 中継が送れず → リングが溢れる、
    という連鎖が起きる。決まった手順で確かめる: 消費側は最初のチャンクを受けたあと、PL が溢れを記録するまで受け取らない
    （受け取る速さや経過時間に頼らない。溢れが起きなければ guard_s 秒で誤り）。溢れが記録されたあとで全部受け取る。"""
    limit = 512 << 10
    guard_s = 60.0                                                   # 連鎖が起きない（実装が壊れた）ときに永久に待たないためだけの値
    link, conn = open_link(fpga, tcp_board, limit)
    ring_beats, per_ms = 1 << 16, 1250
    dev = RingAndRelay(conn, link, ring_beats=ring_beats, per_ms=per_ms)   # リング 1 MB
    try:
        stream = DeepAnalyzer(dev).stream(rle=False)
        received = array("H")
        for chunk in stream.chunks(guard_s):
            assert chunk.first == len(received)
            received.extend(chunk.samples())
            assert dev.overrun_event.wait(guard_s), "消費側が止まっても、リングが溢れなかった（中継が止まらなかった）"
        stats = stream.stats()

        assert stats.overrun                                         # リングが溢れたことは PL が記録し、ホストに届く
        assert dev.unread_at_overrun >= ring_beats - 256 - per_ms    # 溢れた時点で未読がリングを満たしていた = 中継は読めずに止まっていた
        assert len(received) == stats.samples > 0
        expected = array("H", (i & 0xFFFF for i in range(len(received))))
        assert received == expected                                  # 届いた分は欠けも壊れも無い（黙って捨てない）
        assert stats.samples == dev.written * 8                      # PL が書いた分は全部届いた（溢れた後は書いていない）

        buf = stats.link_buffer
        assert buf is not None and buf.capacity_bytes == limit
        assert buf.peak_bytes <= limit                               # PC のメモリは上限で止まる
        assert buf.full_count >= 1 and buf.full_s > 0                # 上限で受信が待った（回数と、待った時間が 0 でないこと）
    finally:
        dev.close()
