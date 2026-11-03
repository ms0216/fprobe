"""ホストの受け取りの道（リンク → フレーム → ロジアナの流れ → 欠けの置き方）の段ごとの速さを測る（試験の計測用。本体ではない）。

ネットワークを使わない: 中継の TCP の役はメモリの上の偽のソケット（recv_into が用意した流れを写すだけ = カーネルの写しに相当）、
機器の CSR はメモリの上の偽の機器。どの段も、本体の同じ関数（EthLink の TCP の受け取り・FrameLink・LinkTransport・LaStream・
SegmentSplitter・GapPlacer）を通す。出すのは測った事実（バイト数・秒・MB/s）だけ。

    python tests/bench_la_receive.py --mb 1024            # 1 GB を各段に流す
    python tests/bench_la_receive.py --mb 256 --stage e2e_raw

段:
  link       偽の TCP → EthLink の受け取りのスレッド → read_bytes（ch 4）。リンクだけ
  split      SegmentSplitter.feed（4 MB ずつ。見出し付きの流れ: 256 拍のかたまり）
  stream_raw LaStream（RAW・イベントなし）。読みはメモリから直接（リンクなし）
  stream_ev  LaStream（見出し付き・イベントあり）。同上
  stream_gap LaStream（見出し付き・欠けの区間あり）。同上
  e2e_raw    偽の TCP → EthLink → LinkTransport → LaStream（RAW）
  e2e_gap    偽の TCP → EthLink → LinkTransport → LaStream（欠けの区間あり）
"""

import argparse
import os
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fprobe import generated as g                       # noqa: E402
from fprobe.eth_link import EthLink                     # noqa: E402
from fprobe.events import KIND_LA_GAP                   # noqa: E402
from fprobe.frame_link import CH_STREAM                 # noqa: E402
from fprobe.la import LaStream, SegmentSplitter         # noqa: E402
from fprobe.transport import LinkTransport              # noqa: E402
from caps_fakes import FakeDeviceBase, caps_for        # noqa: E402

BEAT = 16
SEG = g.STREAM_PL_CHUNK_BEATS                           # PL・複写の道のかたまり（256 拍 = 4 KB）
TCP_RECV = 1 << 20


def header(kind, count, seq):
    return (g.STREAM_SEG_MAGIC << 64 | (seq & 0xFFFFFFFF) << 32 | count << 16 | kind).to_bytes(16, "little")


def raw_pattern(nbytes: int) -> bytes:
    """RAW の拍の列（値の変わる、ありふれた形）。"""
    import numpy as np
    words = (np.arange(nbytes // 2, dtype=np.uint32) // 7 % 0xFFFF).astype("<u2")
    return words.tobytes()


def framed(total_bytes: int, *, gaps_every: int = 0, events_every: int = 0) -> tuple[bytes, int, int, list]:
    """見出し付きの流れ（ロジアナのかたまり SEG 拍。gaps_every かたまりごとに欠けの記録 1 件、events_every かたまりごとに
    UART のイベント 1 件）。返す: (流れ, ロジアナの拍の数, イベントの件数, 欠けの記録[(拍の位置, サンプル)])"""
    block = raw_pattern(SEG * BEAT)
    out, la, ev, records, k = bytearray(), 0, 0, [], 0
    while len(out) < total_bytes:
        if gaps_every and k % gaps_every == gaps_every - 1:
            records.append((la, 64))
            out += header(g.STREAM_SEG_EVENTS, 1, ev)
            out += (KIND_LA_GAP << 120 | 64 << 80 | la << 48 | 5).to_bytes(16, "little")
            ev += 1
        if events_every and k % events_every == events_every - 1:
            out += header(g.STREAM_SEG_EVENTS, 1, ev)
            out += (3 << 120 | 0x41 << 80 | 1000 + ev).to_bytes(16, "little")
            ev += 1
        out += header(g.STREAM_SEG_LA, SEG, la)
        out += block
        la += SEG
        k += 1
    return bytes(out), la, ev, records


class MemTcp:
    """中継の TCP の役: recv_into は用意した流れを写す（最後は 0 = 閉じた）。"""

    def __init__(self, data: bytes):
        self.view, self.pos = memoryview(data), 0
        self.done = threading.Event()

    def recv_into(self, buffer, nbytes=0):
        n = min(nbytes or len(buffer), len(self.view) - self.pos)
        if n <= 0:
            self.done.set()
            time.sleep(0.05)
            raise socket.timeout()
        memoryview(buffer)[:n] = self.view[self.pos:self.pos + n]
        self.pos += n
        return n

    def settimeout(self, _):
        pass

    def setsockopt(self, *a):
        pass

    def close(self):
        pass


class MemUdp:
    """制御の UDP の役（何も届かない）。"""

    def sendto(self, data, address):
        return len(data)

    def recvfrom(self, n):
        time.sleep(0.05)
        raise socket.timeout()

    def recvfrom_into(self, buffer, nbytes=0):
        time.sleep(0.05)
        raise socket.timeout()

    def settimeout(self, _):
        pass

    def close(self):
        pass


def open_mem_link(data: bytes, buffer_bytes: int) -> tuple[EthLink, MemTcp]:
    import inspect
    kw = {}
    if "buffer_bytes" in inspect.signature(EthLink.__init__).parameters:
        from fprobe.session import link_buffer_bytes
        kw["buffer_bytes"] = link_buffer_bytes()
    link = EthLink(MemUdp(), ("127.0.0.1", 1), **kw)
    tcp = MemTcp(data)
    original = socket.create_connection
    socket.create_connection = lambda *a, **k: tcp
    try:
        link.open_stream_tcp(buffer_bytes=buffer_bytes)
    finally:
        socket.create_connection = original
    return link, tcp


class MemDevice(FakeDeviceBase):
    """LaStream の相手の偽の機器: すべての拍は始めから書かれている（stream_beats = 全体）。read_stream_some は read_some を呼ぶ。"""

    def __init__(self, total_bytes: int, read_some, *, events: bool, records: list | None = None):
        self.total_beats = total_bytes // BEAT
        self.read_some = read_some
        self.events, self.records = events, records or []
        self.received = 0
        self.running = True
        self.stream = None                                   # 全部を読んだら止める（run_stream が付ける）

    def capabilities(self):
        return caps_for(mem_bytes=128 << 20, la_channels=16, la_sample_hz=100_000_000,
                        la_stream_events=self.events, la_stream_gaps=bool(self.records))

    def read32(self, address):
        if address == g.LA_DEEP_STATUS:
            reading = self.received < self.total_beats * BEAT
            return int(reading) << g.LA_DEEP_STATUS_READING[0]
        if address == g.LA_DEEP_STREAM_BEATS:
            return self.total_beats & 0xFFFFFFFF
        raise AssertionError(hex(address))

    def read(self, address, count, *, timeout_retries=0):
        if address == g.LA_DEEP_GAP_LAST_BEAT and count == 2:
            return [self.records[-1][0] & 0xFFFFFFFF if self.records else 0, len(self.records)]
        out = []
        for a in range(address, address + 4 * count, 4):              # まとめた READ（番地の順）
            if a in (g.LA_DEEP_STATUS, g.LA_DEEP_STREAM_BEATS):
                out.append(self.read32(a))
            elif a == g.LA_DEEP_GAP_LAST_BEAT:
                out.append(self.records[-1][0] & 0xFFFFFFFF if self.records else 0)
            elif a == g.LA_DEEP_GAPS:
                out.append(len(self.records))
            else:
                out.append(0)
        return out

    def write32(self, address, value):
        if address == g.LA_DEEP_CONTROL and not value & 1:
            self.running = False

    def write(self, address, words):
        pass

    def read_stream_some(self, size, timeout_ms=2000):
        data = self.read_some(size, timeout_ms)
        self.received += len(data)
        if self.received >= self.total_beats * BEAT and self.stream is not None:
            self.stream.stop()
        return data


def mem_reader(data: bytes):
    view, pos = memoryview(data), [0]

    def read_some(size, timeout_ms):
        n = min(size, len(view) - pos[0])
        if n <= 0:
            raise TimeoutError("終わり")
        out = bytes(view[pos[0]:pos[0] + n])                 # USB の EP の読みと同じく、毎回新しい bytes
        pos[0] += n
        return out
    return read_some


def run_stream(dev, *, rle=False, events=None, gaps=False, gap_hold_bytes=None):
    kw = {} if gap_hold_bytes is None else {"gap_hold_bytes": gap_hold_bytes}
    stream = LaStream(dev, rle, 1e-8, 16, events=events, gaps=gaps, **kw)
    dev.stream = stream
    t0 = time.perf_counter()
    for _ in stream.chunks(1e9):
        pass
    return time.perf_counter() - t0, stream


def stage_link(mb):
    data = raw_pattern(mb << 20)
    link, tcp = open_mem_link(data, 8 << 20)
    t0, got = time.perf_counter(), 0
    try:
        while got < len(data):
            got += len(link.read_bytes(CH_STREAM, 4 << 20, 2.0))
    finally:
        link.close()
    return len(data), time.perf_counter() - t0


def stage_split(mb):
    data, *_ = framed(mb << 20)
    view, splitter, t0 = memoryview(data), SegmentSplitter(), time.perf_counter()
    for i in range(0, len(data), 4 << 20):
        splitter.feed(view[i:i + (4 << 20)])
    return len(data), time.perf_counter() - t0


def stage_stream_raw(mb):
    data = raw_pattern(mb << 20)
    dev = MemDevice(len(data), mem_reader(data), events=False)
    dt, _ = run_stream(dev)
    return len(data), dt


def stage_stream_ev(mb):
    data, _, _, _ = framed(mb << 20, events_every=4)
    dev = MemDevice(len(data), mem_reader(data), events=True)
    dt, _ = run_stream(dev, events=(0, 1 << 19))
    return len(data), dt


def _gap_hold():
    return {"gap_hold_bytes": 64 << 20} if "gap_hold_bytes" in LaStream.__init__.__code__.co_varnames else {}


def stage_stream_gap(mb):
    data, _, _, records = framed(mb << 20, gaps_every=16)
    dev = MemDevice(len(data), mem_reader(data), events=True, records=records)
    dt, _ = run_stream(dev, events=(0, 1 << 19), gaps=True, **{k: v for k, v in _gap_hold().items()})
    return len(data), dt


def _e2e(data, events, records, **kw):
    link, tcp = open_mem_link(data, 8 << 20)
    transport = LinkTransport(link)
    try:
        dev = MemDevice(len(data), transport.read_stream_some, events=events is not None, records=records)
        dt, _ = run_stream(dev, events=events, **kw)
    finally:
        link.close()
    return len(data), dt


def stage_e2e_raw(mb):
    return _e2e(raw_pattern(mb << 20), None, None)


def stage_e2e_gap(mb):
    data, _, _, records = framed(mb << 20, gaps_every=16)
    return _e2e(data, (0, 1 << 19), records, gaps=True, **_gap_hold())


STAGES = {"link": stage_link, "split": stage_split, "stream_raw": stage_stream_raw, "stream_ev": stage_stream_ev,
          "stream_gap": stage_stream_gap, "e2e_raw": stage_e2e_raw, "e2e_gap": stage_e2e_gap}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--mb", type=int, default=1024)
    p.add_argument("--stage", action="append", choices=list(STAGES))
    args = p.parse_args()
    for name in args.stage or STAGES:
        nbytes, dt = STAGES[name](args.mb)
        print(f"{name:11s} {nbytes / 1e6:9.1f} MB {dt:7.2f} s {nbytes / dt / 1e6:8.1f} MB/s", flush=True)


if __name__ == "__main__":
    main()
