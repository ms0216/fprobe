# 測定の道具（PC で動かす。1.7.6 の b1_recv.py と同じ受け方）: python recv.py 機器 口 [口 ...] [--sha256]
# 口ごとに 1 本の TCP でつなぎ、recv_into(1 MB, MSG_WAITALL) で相手が閉じるまで受ける。--sha256 は受けた全バイトの
# SHA-256 も出す（zc_send -n で送った中身を、機器の側で読んだリングの中身と照合する）。
# SPDX-License-Identifier: AGPL-3.0-only
import hashlib
import socket
import sys
import threading
import time

args = [a for a in sys.argv[1:] if not a.startswith("--")]
want_sha = "--sha256" in sys.argv
host, ports = args[0], [int(p) for p in args[1:]]
res = {}


def rx(port):
    for _ in range(50):
        try:
            s = socket.create_connection((host, port))
            break
        except OSError:
            time.sleep(0.1)
    else:
        res[port] = (0, 1.0, "connect failed")
        return
    s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
    buf = bytearray(1 << 20)
    mv = memoryview(buf)
    h = hashlib.sha256() if want_sha else None
    total, t0 = 0, time.time()
    while True:
        n = s.recv_into(mv, len(buf), socket.MSG_WAITALL)
        if n == 0:
            break
        if h:
            h.update(mv[:n])
        total += n
    res[port] = (total, time.time() - t0, h.hexdigest() if h else None)


th = [threading.Thread(target=rx, args=(p,)) for p in ports]
for t in th:
    t.start()
for t in th:
    t.join()
for p, (n, dt, sha) in res.items():
    print("port", p, "bytes", n, "MB/s", round(n / dt / 1e6, 1), *(["sha256", sha] if sha else []))
print("sum MB/s", round(sum(n / dt for n, dt, _ in res.values()) / 1e6, 1))
