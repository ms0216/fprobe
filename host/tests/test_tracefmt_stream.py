"""トレースの形式の選び方と RAM からの吸い出し（fprobe.tracefmt.stream）。偽の RAM に各形式のバッファを置く。"""

import struct

import pytest
from sysview_writer import Writer
from test_tracefmt_ctf import scenario as ctf_scenario
from test_tracefmt_eventrecorder import Recorder
from test_tracefmt_systemview import scenario as sysview_scenario
from test_tracefmt_tracex import REGISTRY, ev, image

from fprobe.rtt import RttError
from fprobe.tracefmt.stream import TraceStream, decode_trace, rtt_ring, snapshot

RAM = 0x2000_0000


class Ram:
    def __init__(self, size: int = 0x10000):
        self.mem = bytearray(size)
        self.reads = 0

    def put(self, address: int, data: bytes) -> None:
        self.mem[address - RAM:address - RAM + len(data)] = data

    def read(self, address: int, length: int) -> bytes:
        self.reads += 1
        return bytes(self.mem[address - RAM:address - RAM + length])

    read_memory = read

    def write_memory(self, address: int, data: bytes) -> None:
        raise AssertionError("スナップショットは書かない")


def rtt_block(ram: Ram, cb: int, name: str, buffer: int, size: int, wr: int, rd: int) -> None:
    ram.put(cb, b"SEGGER RTT\0".ljust(16, b"\0") + struct.pack("<II", 1, 0))
    ram.put(cb + 24, struct.pack("<6I", cb + 0x100, buffer, size, wr, rd, 0))
    ram.put(cb + 0x100, name.encode() + b"\0")


def test_rtt_ring_reads_from_rdoff_to_wroff_wrapping():
    ram = Ram()
    ring = bytes(range(64))
    ram.put(0x2000_1000, ring)
    rtt_block(ram, 0x2000_0000, "SysView", 0x2000_1000, 64, wr=10, rd=50)
    assert rtt_ring(ram, 0x2000_0000, "SysView") == ring[50:] + ring[:10]
    with pytest.raises(RttError):
        rtt_ring(ram, 0x2000_0000, "TzData")


def test_systemview_post_mortem_snapshot():
    data = b"\x55\x66" + sysview_scenario().bytes()               # 前のパケットの尻尾から始まる
    size = 1024
    ram = Ram()
    start = size - 40                                              # リングを一周させて置く
    ring = bytearray(size)
    for i, b in enumerate(data):
        ring[(start + i) % size] = b
    ram.put(0x2000_2000, bytes(ring))
    rtt_block(ram, 0x2000_0000, "SysView", 0x2000_2000, size, wr=(start + len(data)) % size, rd=start)
    trace, info = snapshot("systemview", ram.read, lambda n: None, rtt_address=0x2000_0000, memory=ram)
    assert info["bytes"] == len(data) and sum(1 for e in trace.events if e.kind == "task_in") == 3


def test_ctf_ram_backend_snapshot():
    ram = Ram()
    data = ctf_scenario()
    ram.put(0x2000_3000, data)
    trace, info = snapshot("ctf", ram.read, {"ram_tracing": (0x2000_3000, 4096)}.get, ctf_header="ts32_id8")
    assert len(trace.events) == 12 and info["source"] == "ram_tracing"
    assert info["size"] == 4096 and info["truncated"] is False and info["max_bytes"] == 4 << 20
    with pytest.raises(ValueError):
        snapshot("ctf", ram.read, lambda n: None, ctf_header="ts32_id8")


def test_a_snapshot_cut_at_max_bytes_says_so():
    # 黙って切り詰めない（総点検）: 上限 max_bytes で読みを切ったら、領域の大きさ・切った印・使った上限を返す
    ram = Ram()
    data = ctf_scenario()
    ram.put(0x2000_3000, data)
    trace, info = snapshot("ctf", ram.read, {"ram_tracing": (0x2000_3000, 4096)}.get, max_bytes=len(data),
                            ctf_header="ts32_id8")
    assert info == {"bytes": len(data), "size": 4096, "max_bytes": len(data), "truncated": True,
                    "address": 0x2000_3000, "source": "ram_tracing"}
    assert len(trace.events) == 12
    buf = image([ev(0x2000_0100, 6, 5)], REGISTRY)
    ram.put(0x2000_8000, buf)
    ram.put(0x2000_0010, struct.pack("<I", 0x2000_8000))
    with pytest.raises(ValueError, match="max_bytes"):
        snapshot("tracex", ram.read, {"_tx_trace_header_ptr": (0x2000_0010, 4)}.get, max_bytes=16)


def test_tracex_snapshot_through_the_header_pointer():
    ram = Ram()
    buf = image([ev(0x2000_0100, 6, 5)], REGISTRY)
    base = 0x2000_8000                                             # test_tracefmt_tracex の BASE
    ram.put(base, buf)
    ram.put(0x2000_0010, struct.pack("<I", base))
    trace, info = snapshot("tracex", ram.read, {"_tx_trace_header_ptr": (0x2000_0010, 4)}.get, ts_hz=1e6)
    assert info["address"] == base and trace.tasks[0x2000_0100].name == "sensor"


def test_event_recorder_snapshot_and_since():
    ram = Ram()
    r = Recorder(count=8)
    for t in range(5):
        r.record2(0x0101, t, t, 0)
    ram.put(0x2000_0040, r.status())
    ram.put(0x2000_0100, bytes(r.buf))
    ram.put(0x2000_0400, r.info())
    trace, info = snapshot("eventrecorder", ram.read, {"EventRecorderInfo": (0x2000_0400, 24)}.get)
    assert len(trace.events) == 5 and info["next"] == 5
    r.record2(0x0101, 9, 9, 0)
    ram.put(0x2000_0040, r.status())
    ram.put(0x2000_0100, bytes(r.buf))
    more, info = snapshot("eventrecorder", ram.read, {"EventRecorderInfo": (0x2000_0400, 24)}.get, since=5)
    assert [e.args["values"][0] for e in more.events] == [9] and info["next"] == 6


def test_stream_and_file_choices():
    s = TraceStream.create("systemview")
    s.feed(Writer().start(t=0).bytes())
    assert s.trace.ts_hz == 64e6
    assert decode_trace("ctf", ctf_scenario(), ctf_header="ts32_id8").events
    with pytest.raises(ValueError):
        TraceStream.create("tracex")
    with pytest.raises(ValueError):
        decode_trace("eventrecorder", b"")
    with pytest.raises(ValueError):
        snapshot("tracealyzer", lambda a, n: b"", lambda n: None)
