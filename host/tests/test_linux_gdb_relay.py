import socket
import time
from pathlib import Path

import pytest

from fprobe.linux.gdb_relay import GdbRelay
from fprobe.linux.ssh import SshTarget

FAKE_SSH = Path(__file__).with_name("linux_fake_ssh.py")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while not cond() and time.monotonic() < end:
        time.sleep(0.01)
    return cond()


def test_serve_relays_and_closes_both_sides(monkeypatch):
    monkeypatch.setenv("FPROBE_SSH", str(FAKE_SSH))
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).parent))
    port = _free_port()
    relay = GdbRelay(SshTarget("h", "u"), "gdbserver --once --attach - 5", port)
    try:
        c = socket.create_connection(("127.0.0.1", port), timeout=5)
        c.sendall(b"$qsupported#37")
        got = b""
        while len(got) < 14:
            got += c.recv(64)
        assert got == b"$QSUPPORTED#37"
        c.close()
        assert _wait(lambda: relay.status()["closed_by"] is not None)
        st = relay.status()
        assert st["closed_by"] == "gdb" and st["bytes_to_target"] == 14 and st["bytes_from_target"] == 14
        assert st["connected"] is False and st["error"] is None
        assert _wait(lambda: not relay.status()["target_running"])
    finally:
        relay.stop()


def test_serve_port_in_use():
    busy = socket.socket()
    busy.bind(("127.0.0.1", 0))
    busy.listen(1)
    try:
        with pytest.raises(RuntimeError, match="ポート"):
            GdbRelay(SshTarget("h", "u"), "gdbserver - 1", busy.getsockname()[1])
    finally:
        busy.close()


def test_stop_before_connection_starts_nothing(monkeypatch):
    monkeypatch.setenv("FPROBE_SSH", str(FAKE_SSH))
    port = _free_port()
    relay = GdbRelay(SshTarget("h", "u"), "gdbserver --once --attach - 5", port)
    st = relay.stop()
    assert st["closed_by"] == "stop" and not st["target_running"]
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=2)
    time.sleep(0.3)
    assert relay.status()["exit_code"] is None                   # gdbserver を起動していない


def test_ssh_failure_is_reported(monkeypatch, tmp_path):
    monkeypatch.setenv("FPROBE_SSH", str(tmp_path / "no_such_ssh.exe"))
    port = _free_port()
    relay = GdbRelay(SshTarget("h", "u"), "gdbserver --once --attach - 5", port)
    try:
        c = socket.create_connection(("127.0.0.1", port), timeout=5)
        c.settimeout(5)
        assert c.recv(16) == b""                                 # 中継は閉じる（gdb は待たされない）
        assert _wait(lambda: relay.status()["error"] is not None)
    finally:
        relay.stop()
