"""実機: 止めて調べる gdb（R26 段階 3）を Pi 2B で確かめる。FPROBE_LINUX_HOST・FPROBE_LINUX_USER・FPROBE_LINUX_KEY。
題材のアプリ（tick を 10 ms ごと）に gdb の batch で bt を取り、gdbserver の中継に RSP の qSupported を送る。どちらの後も
アプリが終了コード 0 で終わる（止めて調べた後に、プロセスを壊していない）。"""
import os
import socket
import time

import pytest

from fprobe.mcp_server import Probe

HOST = os.environ.get("FPROBE_LINUX_HOST")
pytestmark = pytest.mark.skipif(not HOST, reason="FPROBE_LINUX_HOST が無い")
APP = "/tmp/fprobe_tick_thumb"


@pytest.fixture
def pi():
    p = Probe()
    p.linux_connect(target="t", host=HOST, user=os.environ.get("FPROBE_LINUX_USER", "ms0216"),
                    key=os.environ.get("FPROBE_LINUX_KEY"), sudo=True)
    yield p
    p.close()


def _app(p, n):
    app = p._linux["t"].ssh.open(f"sh -c 'echo $$; exec {APP} {n}'")
    return app, int(app.stdout.readline())


def test_batch_backtrace(pi):
    app, pid = _app(pi, 300)
    time.sleep(1.5)
    r = pi.linux_gdb_batch(target="t", commands=["bt", "info registers pc"], pid=pid, timeout_s=60)
    print(r["command"])
    print(r["stdout"][-800:])
    print("stderr:", r["stderr"][-800:])
    assert r["exit_code"] == 0 and not r["timed_out"] and ("main" in r["stdout"] or "nanosleep" in r["stdout"])
    app.wait(timeout=60)
    assert app.returncode == 0


def test_serve_relays_rsp(pi):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    app, pid = _app(pi, 500)
    time.sleep(1.5)
    print(pi.linux_gdb_serve(target="t", pid=pid, port=port))
    c = socket.create_connection(("127.0.0.1", port), timeout=30)
    c.settimeout(30)
    c.sendall(b"$qSupported#37")
    got = b""
    while b"#" not in got[1:] or len(got) < 4:
        part = c.recv(4096)
        assert part, f"中継が閉じた（受けたもの {got!r}）"
        got += part
    print("reply", got[:200])
    assert got.lstrip(b"+").startswith(b"$") and b"PacketSize" in got
    c.sendall(b"+$D#44")                                   # detach（プロセスを続ける）
    time.sleep(1)
    c.close()
    st = pi.linux_gdb_stop(target="t")
    print("status", st)
    app.wait(timeout=60)
    assert app.returncode == 0
