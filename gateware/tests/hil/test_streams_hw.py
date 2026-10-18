"""生の流れの TCP の公開とファイルへの記録（U05）の実機試験: Nucleo F401RE に RAM で走らせた rtt_demo の RTT を、
デーモン（--stream rtt0=…:telnet）の TCP の口から 2 つの相手が同時に読み、片方から送った行がエコーで返る。
SWO は試験用ファームの ITM の出力（swo_start の後）を公開して読む。"""

import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from fprobe.api.client import DaemonClient
from tests.hil.program import SERIAL, program

pytestmark = pytest.mark.hil
RTT_DEMO = Path(__file__).resolve().parents[3] / "host" / "firmware" / "rtt_demo" / "rtt_demo.elf"
API_PORT, RTT_PORT = 7831, 19021


def read_until(sock: socket.socket, needle: bytes, timeout_s: float = 5.0) -> bytes:
    sock.settimeout(0.5)
    data, deadline = b"", time.monotonic() + timeout_s
    while needle not in data and time.monotonic() < deadline:
        try:
            data += sock.recv(4096)
        except socket.timeout:
            pass
    return data


@pytest.fixture(scope="module")
def daemon(tmp_path_factory):
    program("fprobe")
    log = tmp_path_factory.mktemp("streams") / "rtt0.log"
    proc = subprocess.Popen([sys.executable, "-m", "fprobe.api.daemon", "--serial", SERIAL, "--listen", str(API_PORT),
                             "--stream", f"rtt0={RTT_PORT}:telnet", "--stream-log", f"rtt0={log}"])
    deadline = time.monotonic() + 30
    while True:
        try:
            client = DaemonClient(f"ws://127.0.0.1:{API_PORT}/api", "hil", "script")
            break
        except OSError:
            assert time.monotonic() < deadline, "デーモンが起動しない"
            time.sleep(0.5)
    client.call("target.connect", target="stm32f401xe")
    client.call("target.program_ram", path=str(RTT_DEMO))
    client.call("target.resume")
    client.call("rtt.start")
    yield client, log
    client.close()
    proc.terminate()
    proc.wait(10)


def test_two_tools_read_rtt_at_once_and_one_writes(daemon):
    client, log = daemon
    a = socket.create_connection(("127.0.0.1", RTT_PORT))
    b = socket.create_connection(("127.0.0.1", RTT_PORT))
    assert b"tick" in read_until(a, b"tick") and b"tick" in read_until(b, b"tick")
    a.sendall(b"hello\n")
    assert b"echo: hello" in read_until(b, b"echo: hello")                 # 片方の書き込みの結果をもう片方も見る
    served = client.call("stream.list")["served"][0]
    assert served["stream"] == "rtt0" and len(served["peers"]) == 2 and not served["errors"], served
    a.close()
    b.close()
    text = log.read_text(encoding="utf-8")
    assert "tick" in text and text.startswith("["), text[:200]


def test_the_ports_are_not_open_on_the_lan(daemon):
    import psutil
    listening = [c for c in psutil.net_connections("tcp") if c.status == "LISTEN" and c.laddr.port == RTT_PORT]
    assert listening and all(c.laddr.ip == "127.0.0.1" for c in listening), listening


def test_swo_can_be_served_while_the_tools_still_decode(daemon):
    client, _ = daemon
    client.call("swo.configure", ports=[0], swo_baud=2_000_000, core_clock_hz=16_000_000)
    port = client.call("stream.serve", stream="swo")["port"]
    s = socket.create_connection(("127.0.0.1", port))
    s.settimeout(3)
    got = s.recv(4096)
    assert got, "SWO が届かない"
    s.close()
    client.call("stream.close", stream="swo")
    client.call("swo.unconfigure")
