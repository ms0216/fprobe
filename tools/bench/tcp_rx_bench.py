"""PC の TCP 受信（EthLink._tcp_rx_task）だけの上限を、PC の中のループバックで測る（試験の計測用。本体ではない）。

送り手は別プロセス（GIL を共有しない。中継の口の役で待ち受け、つながれたら全速で送る）、受け手は実物の
EthLink.open_stream_tcp → read_bytes。実機・PYNQ は使わない。出す数: 受けた MB/s、プロセス全体の CPU（全スレッド。
ms/MB と 1 コアに対する割合）、リンクに溜まった最大。variant は受信のスレッドの本体だけの差し替え。

    python tools/bench/tcp_rx_bench.py [--seconds 6] [--repeat 3] [--variants now,...] [--chunk-mb 4]
"""

import argparse
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, os.environ.get("FPROBE_HOST") or str(Path(__file__).resolve().parents[2] / "host"))

from fprobe import generated as g                                # noqa: E402
from fprobe.la_segments import SegmentSplitter                   # noqa: E402
from fprobe.eth_link import CH_STREAM, EthLink                   # noqa: E402
from fprobe.session import link_buffer_bytes                     # noqa: E402

SENDER = r"""
import socket, sys, time
seconds, chunk = float(sys.argv[1]), int(sys.argv[2])
lst = socket.socket()
lst.bind(("127.0.0.1", 0))
lst.listen(1)
print(lst.getsockname()[1], flush=True)
s, _ = lst.accept()
import struct
mode, magic = sys.argv[3], int(sys.argv[4])
if mode == "segments":        # 見出し 16 B（種類 LA = 1・拍 256・番号・印）+ 中身 4096 B を続けた並び（実機のストリームの形）
    unit = struct.pack("<HHIQ", 1, 256, 0, magic) + bytes(4096)
    buf = unit * (chunk // len(unit))
else:
    buf = bytes(range(256)) * (chunk // 256)
end = time.monotonic() + seconds
n = 0
while time.monotonic() < end:
    s.sendall(buf)
    n += len(buf)
s.close()
print(n, flush=True)
"""


def make_rx(recv_size, batch, wait_s, waitall):
    """_tcp_rx_task の差し替え版（現行と同じ構造。recv_size = 1 回の読みの上限、batch 未満なら wait_s 眠る、waitall = MSG_WAITALL）。"""
    def task(self):
        sock = self._tcp_sock

        def recv(view):
            try:
                return sock.recv_into(view, 0, socket.MSG_WAITALL) if waitall else sock.recv_into(view)
            except socket.timeout:
                return -1
        try:
            while not self._stop.is_set():
                size = self._receive_into(CH_STREAM, recv, recv_size)
                if size < 0:
                    continue
                if not size:
                    raise ConnectionError("closed")
                if batch and size < batch:
                    time.sleep(wait_s)
        except (OSError, ConnectionError) as e:
            if self._stop.is_set():
                return
            with self._cond:
                self._stream_error = e
                self._chan_cond[CH_STREAM].notify_all()
    return task


# waitall = True の版は Windows（タイムアウトつきのソケット）で OSError 10045（WSAEOPNOTSUPP）になるので、--variants で明示したときだけ
VARIANTS = {
    "waitall": make_rx(1 << 20, 0, 0, True),
    "now": None,                                                      # 現行のコードそのまま
    "nosleep": make_rx(1 << 20, 0, 0, False),
    "sleep-256K": make_rx(1 << 20, 256 << 10, 0.001, False),
    "recv256K": make_rx(256 << 10, 64 << 10, 0.001, False),
    "recv64K": make_rx(64 << 10, 64 << 10, 0.001, False),
}


def run(variant, seconds, mode, read_bytes):
    proc = subprocess.Popen([sys.executable, "-c", SENDER, str(seconds), str(1 << 20), mode, str(g.STREAM_SEG_MAGIC)], stdout=subprocess.PIPE, text=True)
    port = int(proc.stdout.readline())
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.bind(("127.0.0.1", 0))
    saved = EthLink._tcp_rx_task
    if VARIANTS[variant] is not None:
        EthLink._tcp_rx_task = VARIANTS[variant]
    try:
        link = EthLink.open("127.0.0.1", udp.getsockname()[1], buffer_bytes=link_buffer_bytes())
        total = segments = 0
        splitter = SegmentSplitter() if mode == "segments" else None
        cpu0, t0 = time.process_time(), time.perf_counter()
        link.open_stream_tcp(port=port, buffer_bytes=8 << 20)
        try:
            while True:
                data = link.read_bytes(CH_STREAM, read_bytes, timeout_s=0.5)
                total += len(data)
                if splitter is not None:                              # かたまりの境目の検出（読んだ塊をそのまま分ける）
                    segments += len(splitter.feed(data))
        except ConnectionError:
            pass
        wall, cpu = time.perf_counter() - t0, time.process_time() - cpu0
        peak = link.stream_buffer().peak_bytes
        sent = int(proc.stdout.readline())
        assert total == sent and (splitter is None or not splitter.pending), (total, sent)                         # 欠け・余りなし
        link.close()
    finally:
        EthLink._tcp_rx_task = saved
        udp.close()
        proc.wait()
    mb = total / 1e6
    return mb / wall, cpu * 1000 / mb, cpu / wall, peak / 1e6


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=6)
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--variants", default=",".join(v for v in VARIANTS if v != "waitall"))
    ap.add_argument("--mode", choices=("raw", "segments"), default="raw", help="segments = 見出し付きのかたまりを境目で分けながら読む")
    ap.add_argument("--read-mb", type=int, default=4, help="消費側の 1 回の read_bytes の大きさ")
    args = ap.parse_args()
    print(f"{'variant':18} {'MB/s':>8} {'CPU ms/MB':>10} {'core':>6} {'peak MB':>8}")
    for name in args.variants.split(","):
        for _ in range(args.repeat):
            mbs, ms, core, peak = run(name, args.seconds, args.mode, args.read_mb << 20)
            print(f"{name:18} {mbs:8.1f} {ms:10.2f} {core:6.2f} {peak:8.1f}", flush=True)


if __name__ == "__main__":
    main()
