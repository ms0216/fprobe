"""SCVD の解釈（fprobe.tracefmt.scvd）: 式・出来事の書式（実物の RTX5.scvd・EventRecorder.scvd）・Component Viewer。"""

import struct
from pathlib import Path

import pytest

from fprobe.tracefmt.scvd import Scvd, ScvdContext, ScvdError, evaluate

DATA = Path(__file__).resolve().parent / "data" / "tracefmt"


@pytest.mark.parametrize("expr,expected", [
    ("1 + 2 * 3", 7), ("(1 + 2) * 3", 9), ("7 / 2", 3), ("-7 / 2", -3), ("-7 % 3", -1), ("1 << 4 | 1", 17),
    ("3 > 2 && 2 > 3", 0), ("!0 + ~0", 0), ("0x10 >= 16 ? 5 : 6", 5), ("(uint8_t)0x1FF", 0xFF),
    ("(int8_t)0xFF", -1), ("a = 4; a += 2; a * 10", 60), ("x ? y : z", 3), ("1.5 * 2", 3.0),
    ("\"abc\" == \"abc\"", 1),
])
def test_expressions(expr, expected):
    assert evaluate(expr, {"x": 0, "y": 2, "z": 3}) == expected


def test_unknown_names_and_division_by_zero():
    with pytest.raises(ScvdError):
        evaluate("nothing + 1")
    with pytest.raises(ScvdError):
        evaluate("1 / 0")


@pytest.fixture(scope="module")
def rtx():
    return Scvd.load(DATA / "RTX5.scvd", DATA / "EventRecorder.scvd")


def test_real_scvd_files_load(rtx):
    assert len(rtx.events) > 180 and "osRtxThread_t" in rtx.typedefs
    assert rtx.typedefs["osRtxThread_t"].size == 80
    assert rtx.component(0xF2).states["Running"]["unique"] == "1"
    assert rtx.events[0xEF00].property == "StartA(0)"


def test_event_values_are_formatted(rtx):
    names = {0x0800_0101: "worker_thread"}
    ctx = ScvdContext(symbol_name=names.get)
    switched = rtx.format_event(0xF219, {"val1": 0x2000_1000, "val2": 0}, ctx)
    assert switched["property"] == "ThreadSwitched" and switched["value"] == "thread_id=0x20001000"
    assert switched["state"] == "Running" and switched["unique"] and switched["handle"] == 0x2000_1000
    created = rtx.format_event(0xF203, {"val1": 0x2000_1000, "val2": 0x0800_0101}, ctx)
    assert created["name"] == "worker_thread" and created["tracking"] == "Start"
    info = rtx.format_event(0xF104, {"val1": 20_010_000, "val2": 50_090_001}, ctx)
    assert info["value"] == "version_api=2.1.0, version_kernel=5.9.1"
    freed = rtx.format_event(0xF005, {"val1": 1, "val2": 2, "val3": 0xFFFF_FFFD}, ctx)
    assert freed["value"].endswith("status=osErrorResource") and freed["level"] == "info"
    error = rtx.format_event(0xF200, {"val1": 1, "val2": 0xFFFF_FFFE}, ctx)
    assert error["level"] == "error" and error["component"] == "Thread Events"
    assert rtx.format_event(0x1234, {}) is None


def test_file_names_from_the_elf(rtx):
    out = rtx.format_event(0xEF00, {"val1": 0x0800_2000, "val2": 42}, ScvdContext(string={0x0800_2000: "main.c"}.get))
    assert out["value"] == "File=main.c(42)"


# --- Component Viewer ---------------------------------------------------------------------------------

VIEW = """<?xml version="1.0" encoding="utf-8"?>
<component_viewer schemaVersion="1.2.0">
  <component name="Demo" version="1.0"/>
  <typedefs>
    <typedef name="node_t" size="12">
      <member name="next"  type="*node_t"  offset="0"/>
      <member name="name"  type="uint32_t" offset="4"/>
      <member name="state" type="uint8_t"  offset="8">
        <enum name="Idle" value="0"/>
        <enum name="Busy" value="1"/>
      </member>
      <member name="load"  type="int16_t"  offset="10"/>
    </typedef>
    <typedef name="info_t" size="8">
      <member name="count" type="uint32_t" offset="0"/>
      <member name="head"  type="uint32_t" offset="4"/>
    </typedef>
  </typedefs>
  <objects>
    <object name="Demo Object">
      <var name="busy" type="uint32_t" value="0"/>
      <read name="info" type="info_t" symbol="demo_info"/>
      <readlist name="nodes" type="node_t" offset="info.head" next="next"/>
      <list name="i" start="0" limit="nodes._count">
        <calc cond="nodes[i].state == node_t:state:Busy">busy += 1;</calc>
      </list>
      <calc>info.double = info.count * 2;</calc>
      <out name="Demo">
        <item property="Nodes" value="%d[nodes._count] (busy %d[busy], twice %d[info.double])">
          <list name="i" start="0" limit="nodes._count">
            <item property="%N[nodes[i].name]" value="%E[nodes[i].state] at %x[nodes[i]._addr]">
              <print cond="nodes[i].load &lt; 0" property="%N[nodes[i].name]" value="negative %d[nodes[i].load]"/>
            </item>
          </list>
        </item>
        <item property="Hidden" value="x" cond="busy == 99"/>
      </out>
    </object>
  </objects>
</component_viewer>
"""


class Memory:
    def __init__(self):
        self.mem = {}

    def put(self, address: int, data: bytes) -> None:
        for i, b in enumerate(data):
            self.mem[address + i] = b

    def read(self, address: int, size: int) -> bytes:
        if address == 0xDEAD0000:
            raise OSError("読めない")
        return bytes(self.mem.get(address + i, 0) for i in range(size))


def demo_memory() -> Memory:
    m = Memory()
    m.put(0x2000_0000, struct.pack("<II", 3, 0x2000_0100))
    m.put(0x2000_0100, struct.pack("<IIBxh", 0x2000_0200, 0x2000_0400, 1, 5))
    m.put(0x2000_0200, struct.pack("<IIBxh", 0x2000_0300, 0x2000_0410, 0, -4))
    m.put(0x2000_0300, struct.pack("<IIBxh", 0, 0x2000_0420, 1, 0))
    m.put(0x2000_0400, b"alpha\0")
    m.put(0x2000_0410, b"beta\0")
    m.put(0x2000_0420, b"gamma\0")
    return m


def test_component_viewer_reads_memory_and_builds_the_tree():
    scvd = Scvd()
    scvd.add(VIEW)
    m = demo_memory()
    ctx = ScvdContext(read=m.read, symbol={"demo_info": (0x2000_0000, 8)}.get)
    [obj] = scvd.view(ctx)
    assert obj["property"] == "Demo Object" and "notes" not in obj
    [nodes] = obj["children"]
    assert nodes["value"] == "3 (busy 2, twice 6)"
    assert [(c["property"], c["value"]) for c in nodes["children"]] == [
        ("alpha", "Busy at 0x20000100"), ("beta", "negative -4"), ("gamma", "Busy at 0x20000300")]


def test_missing_symbols_are_noted_not_raised():
    scvd = Scvd()
    scvd.add(VIEW)
    [obj] = scvd.view(ScvdContext(read=demo_memory().read, symbol=lambda name: None))
    assert obj["notes"] and "demo_info" in obj["notes"][0]


def test_real_rtx5_objects_on_an_empty_target(rtx):
    """RTX が動いていないメモリ（0）: 式の誤りなく、カーネルの状態だけが出る。"""
    symbols = {"osRtxInfo": (0x2000_0000, 164), "osRtxConfig": (0x0800_0000, 100)}
    view = rtx.view(ScvdContext(read=lambda a, n: bytes(n), symbol=symbols.get))
    assert view[0]["property"] == "RTX RTOS Object" and "notes" not in view[0]
    assert view[0]["children"][0]["children"][0] == {"property": "Kernel State", "value": "osKernelInactive"}
