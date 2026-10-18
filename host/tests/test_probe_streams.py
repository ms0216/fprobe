"""Probe の流れの公開と記録（fprobe.probe_streams、U05）: RTT のチャンネル・SWO・UART を TCP で複数の相手へ配り、
相手からの書き込みをターゲットへ、ファイルへの記録。機器は使わない（rtt_demo の ELF を置いた偽のメモリ、偽の SWO・UART）。"""

import socket
import struct
from types import SimpleNamespace

import pytest
from test_probe_observe import RTT_DEMO, SRAM, FakeCpu, FakeSwo, connect, target_writes, wait_for
from test_probe_recording import FakeDevice
from test_streams import recv_exactly

from fprobe.mcp_server import Probe
from fprobe.probe_streams import stream_key
from fprobe.symbols import Symbols


class FakeLink:
    """フレームのリンクの UART（ch 3）の偽物。"""

    def __init__(self):
        self.rx = bytearray()
        self.tx = bytearray()
        self.uart_baud = 115200

    def set_uart_baud(self, baud):
        self.uart_baud = baud

    def uart_read(self, n):
        data, self.rx[:] = bytes(self.rx[:n]), self.rx[n:]
        return data

    def uart_write(self, data):
        self.tx += data
        return len(data)


@pytest.fixture
def probe(tmp_path, monkeypatch):
    monkeypatch.setenv("FPROBE_HOME", str(tmp_path))
    p = Probe("unused")
    p._session = SimpleNamespace(device=FakeDevice(), link=FakeLink())
    yield p
    p._close_streams()
    p._stop_observing()


def open_client(probe, stream) -> socket.socket:
    port = next(s["port"] for s in probe.stream_list()["served"] if s["stream"] == stream)
    sock = socket.create_connection(("127.0.0.1", port), timeout=3)
    wait_for(lambda: any(s["peers"] for s in probe.stream_list()["served"] if s["stream"] == stream))
    return sock


def test_stream_names():
    assert stream_key("rtt0") == 0 and stream_key("rtt12") == 12 and stream_key("swo") == "swo"
    with pytest.raises(ValueError):
        stream_key("rtt")
    with pytest.raises(ValueError):
        stream_key("itm")


def test_an_rtt_channel_is_served_to_several_clients_and_written_back(probe):
    cpu = FakeCpu(image=RTT_DEMO)
    connect(probe, cpu)
    probe.elf_load(str(RTT_DEMO))
    probe.rtt_start()
    served = probe.stream_serve("rtt0", protocol="telnet")
    assert served["address"] == "127.0.0.1" and served["writable"]
    a, b = open_client(probe, "rtt0"), socket.create_connection(("127.0.0.1", served["port"]), timeout=3)
    wait_for(lambda: len(probe.stream_list()["served"][0]["peers"]) == 2)
    target_writes(cpu, 0, b"tick 1\n")
    assert recv_exactly(a, 7) == b"tick 1\n" and recv_exactly(b, 7) == b"tick 1\n"
    assert probe.rtt_read(timeout_s=1)["data"] == "tick 1\n"                  # 道具の読みも同じものを受ける
    a.sendall(b"echo\n")
    down = Symbols(RTT_DEMO).variable("down0_buffer").address
    wait_for(lambda: bytes(cpu.ram[down - SRAM:][:5]) == b"echo\n")
    a.close()
    b.close()
    closed = probe.stream_close("rtt0")
    assert closed["published"] == 7 and not probe.stream_list()["served"]
    with pytest.raises(KeyError):
        probe.stream_close("rtt0")


def test_writing_before_rtt_is_started_is_reported_on_the_stream(probe):
    probe.stream_serve("rtt0")
    a = open_client(probe, "rtt0")
    a.sendall(b"x")
    wait_for(lambda: probe.stream_list()["served"][0]["errors"])
    assert "rtt_start" in probe.stream_list()["served"][0]["errors"][0]
    a.close()
    with pytest.raises(RuntimeError):
        probe.stream_serve("rtt0")                                            # 二重の公開


def test_swo_is_pumped_in_the_background_and_read_only(probe):
    swo = FakeSwo([])
    connect(probe, FakeCpu(), swo)
    probe.swo_start(baud=1_000_000)
    served = probe.stream_serve("swo")                                        # ここから背景で汲み出す
    assert not served["writable"]
    a = open_client(probe, "swo")
    # 公開の口は、つないだ後に届いたバイトだけを配る（つなぐ前の分は相手がいないので配らない）。バイトはつないだ後に
    # 届かせる（B21: 前は始めから届く形で、背景の汲み出しが接続より先に読むと相手に届かず、時々時間切れになった）
    swo.chunks = [b"\x01\x02", b"", b"\x03"]
    assert recv_exactly(a, 3) == b"\x01\x02\x03"                              # 誰も読まなくても背景で受ける
    assert probe.swo_read(timeout_s=0) == "010203"                            # 道具の読みにも残る
    a.sendall(b"zz")
    wait_for(lambda: probe.stream_list()["served"][0]["rejected"] == 2)
    a.close()
    probe.stream_close("swo")
    assert "swo" not in probe._pumps


def test_uart_is_served_and_written(probe):
    link = probe.session.link
    probe.stream_serve("uart", port=0)
    a = open_client(probe, "uart")
    link.rx += b"boot ok\r\n"
    assert recv_exactly(a, 9) == b"boot ok\r\n"
    a.sendall(b"reset\r")
    wait_for(lambda: bytes(link.tx) == b"reset\r")
    a.close()
    link.rx += b"more"
    wait_for(lambda: probe.uart_read(timeout_s=0) == "boot ok\r\nmore" or False, timeout_s=3)


def test_a_stream_is_logged_to_a_file_with_times(probe, tmp_path):
    cpu = FakeCpu(image=RTT_DEMO)
    connect(probe, cpu)
    probe.elf_load(str(RTT_DEMO))
    probe.rtt_start()
    path = tmp_path / "logs" / "rtt0.txt"
    started = probe.stream_log_start("rtt0", str(path), timestamps=True, append=False)
    assert started["path"] == str(path)
    target_writes(cpu, 0, b"one\ntwo\n")
    wait_for(lambda: path.exists() and path.read_text(encoding="utf-8").count("\n") == 2)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("[20") and lines[0].endswith("] one") and lines[1].endswith("] two")
    with pytest.raises(RuntimeError):
        probe.stream_log_start("rtt0", str(path))
    assert probe.stream_log_stop("rtt0")["written"] > 0 and not probe.stream_list()["logs"]


def test_writes_go_through_the_host_when_attached(probe):
    class Host:
        def __init__(self):
            self.calls, self.joined, self.left = [], [], []

        def join(self, name):
            self.joined.append(name)
            return name

        def call(self, client, method, params):
            self.calls.append((client, method, params))
            return {"written": len(bytes.fromhex(params["data_hex"]))}

        def leave(self, client):
            self.left.append(client)
    host = Host()
    probe.attach_stream_host(host)
    probe.stream_serve("rtt1")
    probe.stream_serve("swo")                                                 # 読むだけの口は利用者にならない
    a = open_client(probe, "rtt1")
    a.sendall(b"\x10\x20")
    wait_for(lambda: host.calls)
    assert host.calls == [("tcp:rtt1", "rtt.write", {"channel": 1, "data_hex": "1020"})]
    a.close()
    probe._close_streams()
    assert host.joined == ["tcp:rtt1"] and host.left == ["tcp:rtt1"]
