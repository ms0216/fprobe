"""fprobe.streams（U05）: 生のバイトの流れの TCP の口（複数の相手へ配る・下りの書き込み・Telnet）とファイルへの記録。"""

import socket
import time
from datetime import datetime

import pytest

from fprobe.streams import StreamLogger, StreamServer, TelnetFilter, telnet_escape


def wait_for(condition, timeout_s=3.0):
    deadline = time.monotonic() + timeout_s
    while not condition():
        assert time.monotonic() < deadline, "時間切れ"
        time.sleep(0.005)


def recv_exactly(sock, n, timeout_s=3.0):
    sock.settimeout(timeout_s)
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        assert chunk, "切れた"
        data += chunk
    return data


@pytest.fixture
def server():
    received = []
    s = StreamServer("rtt0", on_input=received.append)
    s.received = received
    yield s
    s.close()


def connect(s: StreamServer) -> socket.socket:
    sock = socket.create_connection(("127.0.0.1", s.port), timeout=3)
    wait_for(lambda: s.connections and len(s.info()["peers"]) >= 1)
    return sock


def test_listens_only_on_the_local_address(server):
    assert server.info()["address"] == "127.0.0.1" and server.port > 0


def test_every_client_gets_the_same_bytes(server):
    a = connect(server)
    b = socket.create_connection(("127.0.0.1", server.port), timeout=3)
    wait_for(lambda: len(server.info()["peers"]) == 2)
    server.publish(b"tick 1\n")
    server.publish(b"\xff\x00")
    assert recv_exactly(a, 9) == b"tick 1\n\xff\x00" and recv_exactly(b, 9) == b"tick 1\n\xff\x00"
    a.close()
    b.close()
    wait_for(lambda: not server.info()["peers"])
    assert server.info()["published"] == 9


def test_bytes_from_a_client_go_to_the_input(server):
    a = connect(server)
    a.sendall(b"help\n")
    wait_for(lambda: b"".join(server.received) == b"help\n")
    a.close()


def test_a_client_that_does_not_read_loses_bytes_but_does_not_block_others():
    s = StreamServer("swo", queue_limit=1000)
    try:
        slow = socket.create_connection(("127.0.0.1", s.port), timeout=3)
        slow.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)
        wait_for(lambda: len(s.info()["peers"]) == 1)
        started = time.monotonic()
        for _ in range(5000):
            s.publish(b"x" * 500)                                   # 2.5 MB を、読まない相手に
        assert time.monotonic() - started < 2                        # 出どころは待たされない
        wait_for(lambda: s.info()["peers"][0]["dropped"] > 0)
        slow.close()
    finally:
        s.close()


def test_a_read_only_stream_counts_and_drops_input():
    s = StreamServer("swo")
    try:
        a = connect(s)
        a.sendall(b"abc")
        wait_for(lambda: s.info()["rejected"] == 3)
        assert not s.info()["writable"]
        a.close()
    finally:
        s.close()


def test_input_errors_are_kept_and_the_port_stays_open():
    def broken(data):
        raise RuntimeError("RTT を始めていない")
    s = StreamServer("rtt0", on_input=broken)
    try:
        a = connect(s)
        a.sendall(b"x")
        wait_for(lambda: s.info()["errors"])
        s.publish(b"ok")
        assert recv_exactly(a, 2) == b"ok"
        a.close()
    finally:
        s.close()


def test_telnet_doubles_iac_on_output_and_strips_commands_on_input():
    received = []
    s = StreamServer("rtt0", protocol="telnet", on_input=received.append)
    try:
        a = connect(s)
        s.publish(b"a\xffb")
        assert recv_exactly(a, 4) == b"a\xff\xffb"
        a.sendall(b"\xff\xfb\x01hi\xff\xff\r\x00\xff\xfa\x18\x00xterm\xff\xf0!")   # WILL ECHO、IAC IAC、CR NUL、SB … SE
        wait_for(lambda: b"".join(received) == b"hi\xff\r!")
        a.close()
    finally:
        s.close()
    with pytest.raises(ValueError):
        StreamServer("x", protocol="zmq")


def test_telnet_filter_keeps_state_across_chunks():
    f = TelnetFilter()
    assert f.feed(b"a\xff") == b"a" and f.feed(b"\xfd") == b"" and f.feed(b"\x03b") == b"b"
    assert telnet_escape(b"\xff") == b"\xff\xff"


def test_closing_the_server_closes_the_clients(server):
    a = connect(server)
    server.close()
    a.settimeout(3)
    assert a.recv(10) == b""


def test_the_logger_writes_bytes_or_lines_with_times(tmp_path):
    raw = StreamLogger(tmp_path / "raw.bin")
    raw.write(b"\x00\x01")
    raw.write(b"\x02")
    raw.close()
    assert (tmp_path / "raw.bin").read_bytes() == b"\x00\x01\x02"
    clock = datetime(2026, 10, 18, 12, 0, 0, 123000)
    text = StreamLogger(tmp_path / "log" / "rtt.txt", timestamps=True, now=lambda: clock)
    text.write(b"one\ntw")
    text.write(b"o\nthree\n")
    text.close()
    stamp = "[2026-10-18 12:00:00.123] "
    assert (tmp_path / "log" / "rtt.txt").read_text() == f"{stamp}one\n{stamp}two\n{stamp}three\n"
    again = StreamLogger(tmp_path / "raw.bin")                      # 既定は追記
    again.write(b"\x03")
    again.close()
    assert (tmp_path / "raw.bin").read_bytes() == b"\x00\x01\x02\x03" and again.info()["written"] == 1
