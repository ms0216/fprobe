"""Probe の既存のログ・トレースの形式の取り込み（fprobe.probe_formats）。機器は使わない（rtt_demo の ELF を置いた
偽の CPU、偽のイベント記録のリング、偽のターゲットのメモリ）。"""

import json
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest
from probe_fakes import DictMemory, FakeDevice, FakeTarget, use_fake_ring
from sysview_writer import Writer, varint
from test_probe_observe import RTT_DEMO, SRAM, FakeCpu, connect, target_writes, wait_for
from test_tracefmt_ctf import scenario as ctf_scenario
from test_tracefmt_eventrecorder import Recorder
from test_tracefmt_scvd import DATA as SCVD_DATA

from fprobe import probe_window
from fprobe.events import KIND_UART_RX, DapEvent
from fprobe.logfmt.framing import rzcobs_encode
from fprobe.mcp_server import Probe
from fprobe.symbols import Symbols

FW = Path(__file__).resolve().parents[1] / "firmware"
DEFMT = FW / "defmt_demo" / "defmt_demo.elf"


@pytest.fixture
def probe(tmp_path, monkeypatch):
    monkeypatch.setenv("FPROBE_HOME", str(tmp_path))
    use_fake_ring(monkeypatch, probe_window)
    p = Probe("unused")
    p._session = SimpleNamespace(device=FakeDevice())
    yield p
    for stop in (p.log_stop, p.trace_stop):
        try:
            stop()
        except RuntimeError:
            pass
    if p._recorder is not None:
        p.recording_stop()
    p._stop_observing()


def rtt_probe(probe) -> FakeCpu:
    cpu = FakeCpu(image=RTT_DEMO)
    connect(probe, cpu)
    probe.elf_load(str(RTT_DEMO))
    return cpu


# --- ログ ------------------------------------------------------------------------------------------

def defmt_frame(index: int, *payload: bytes) -> bytes:
    return b"\x00" + rzcobs_encode(index.to_bytes(2, "little") + b"".join(payload)) + b"\x00"


def test_defmt_over_rtt(probe):
    cpu = rtt_probe(probe)
    started = probe.log_start("defmt", channel=1, elf=str(DEFMT))
    assert started["running"] and started["settings"]["channel"] == 1
    from fprobe.logfmt.defmt import DefmtTable
    table = DefmtTable.from_elf(DEFMT)
    hello = table.find("Hello {=str}, count={=u8}")
    error = table.find("fault at {=u32:#010x}")
    target_writes(cpu, 1, defmt_frame(hello, (5).to_bytes(4, "little"), (2).to_bytes(4, "little"), b"ab", b"\x07"))
    target_writes(cpu, 1, defmt_frame(error, (9).to_bytes(4, "little"), (0xDEAD).to_bytes(4, "little")))
    wait_for(lambda: probe.log_status()["records"] == 2)
    out = probe.log_read()
    assert [(r["text"], r["level"]) for r in out["records"]] == [("Hello ab, count=7", "info"),
                                                                  ("fault at 0x0000dead", "error")]
    assert out["records"][0]["location"]["file"].endswith("defmt_demo.c") and out["records"][0]["t"] > 0
    assert [r["text"] for r in probe.log_read(level="error")["records"]] == ["fault at 0x0000dead"]
    assert probe.log_read(since=out["next"])["records"] == []
    stopped = probe.log_stop()
    assert not stopped["running"] and stopped["kinds"] == {"info": 1, "error": 1}
    with pytest.raises(RuntimeError):
        probe.log_stop()


def test_text_logs_are_recorded_with_addresses(probe):
    cpu = rtt_probe(probe)
    probe.recording_start(name="log")
    probe.log_start("text")
    func = Symbols(RTT_DEMO).symbol("rtt_read").address
    target_writes(cpu, 0, f"panic at 0x{func:08x}\n".encode())
    wait_for(lambda: probe.log_status()["records"] == 1)
    probe.log_stop()
    found = probe.recording_find({"stream": "log"})["events"]
    assert len(found) == 1 and found[0]["kind"] == "log"
    assert found[0]["body"]["addresses"][0]["function"] == "rtt_read"


def uart(dev: FakeDevice, t: int, data: bytes, spacing: int = 100) -> None:
    for i, b in enumerate(data):
        dev._event(DapEvent(t + i * spacing, KIND_UART_RX, False, False, 0, 0, False, 0, b))


def test_uart_lines_carry_the_device_time_of_the_newline(probe):
    dev = FakeDevice()
    probe._session = SimpleNamespace(device=dev)
    probe.log_start("text", source="uart")
    t0 = dev.ticks + 10
    uart(dev, t0, b"boot ok\r\nsecond")
    wait_for(lambda: probe.log_status()["records"] == 1)
    [r] = probe.log_read()["records"]
    assert r["text"] == "boot ok" and r["t"] == t0 + 8 * 100
    uart(dev, dev.ticks + 10, b" line\n")
    stopped = probe.log_stop()
    assert stopped["records"] == 2 and probe.log_read(since=1)["records"][0]["text"] == "second line"


def test_trice_over_swo_itm(probe, tmp_path):
    dev = FakeDevice()
    probe._session = SimpleNamespace(device=dev)
    mem = DictMemory()
    target = FakeTarget(mem, halted=False)
    probe._debug = SimpleNamespace(target=target, probe=None, close=lambda: None)
    probe._swo_baud = 2_000_000
    til = tmp_path / "til.json"
    til.write_text(json.dumps({"5": {"Type": "TRICE8", "Strg": "inf:v=%u\\n"}}))
    probe.log_start("trice", source="swo", port=3, til=str(til), framing="none")
    assert target.write_log[-1] == (0xE0000E00, 1 << 3)              # ITM_TER: ポート 3
    packet = bytes.fromhex("0540") + bytes([0xC0, 1]) + b"\x2a"     # ID 5・NC（長さ 1）・値 42
    itm = b"".join(bytes([3 << 3 | 1, b]) for b in packet)           # ポート 3 の 1 バイトの書き込み
    dev.swo(dev.ticks + 10, itm)
    wait_for(lambda: probe.log_status()["records"] == 1)
    [r] = probe.log_read()["records"]
    assert (r["text"], r["level"], r["channel"]) == ("v=42", "info", "inf")
    probe.log_stop()
    assert target.write_log[-1] == (0xE0000E00, 0)                   # 元に戻す


def test_offline_decode_and_annotate(probe):
    from fprobe.logfmt.defmt import DefmtTable
    warn = DefmtTable.from_elf(DEFMT).find("temperature {=i16} is high")
    frame = defmt_frame(warn, (1).to_bytes(4, "little"), (-3).to_bytes(2, "little", signed=True))
    out = probe.log_decode("defmt", data_hex=frame.hex(), elf=str(DEFMT))
    assert out["count"] == 1 and out["records"][0]["text"] == "temperature -3 is high"
    func = Symbols(RTT_DEMO).symbol("reset_handler").address
    annotated = probe.log_annotate(f"Backtrace: 0x{func + 4:08x}:0x20007000", elf=str(RTT_DEMO))
    assert "[reset_handler+4" in annotated["text"] and annotated["addresses"][0]["function"] == "reset_handler"
    with pytest.raises(ValueError):
        probe.log_decode("defmt")
    with pytest.raises(ValueError):
        probe.log_start("syslog")


# --- トレース -----------------------------------------------------------------------------------------

def sysview_stream() -> bytes:
    w = Writer().start(t=1_000)
    a, b = 0x2000_1000, 0x2000_2000
    w.task_info(a, 3, "sensor", 1_000).task_info(b, 1, "IDLE", 1_000)
    w.packet(4, varint(w.shrink(b)), 2_000)
    w.packet(4, varint(w.shrink(a)), 66_000)
    w.packet(2, varint(27), 70_000).packet(3, b"", 70_640)
    w.packet(5, b"", 130_000).packet(4, varint(w.shrink(b)), 130_000)
    return w.bytes()


def test_systemview_over_rtt_becomes_a_recorded_timeline(probe):
    cpu = rtt_probe(probe)
    probe.recording_start(name="sv")
    started = probe.trace_start("systemview", channel=1)
    assert started["running"] and any("SysView" in w for w in started["warnings"])   # 下りに SysView が無い
    target_writes(cpu, 1, sysview_stream())
    wait_for(lambda: probe.trace_status()["events"] >= 10)
    stopped = probe.trace_stop()
    assert stopped["ts_hz"] == 64e6 and stopped["recorded_events"] == stopped["events"]
    tl = stopped["timeline"]
    assert tl["made"] and tl["recorded"] and tl["isr"] == 1 and tl["stats"]["tasks"]["sensor"]["runs"] == 1
    got = probe.rtos_timeline_get()
    assert got["method"] == "systemview" and not got["running"] and got["exceptions"]["activations"] == 1
    assert abs(got["stats"]["tasks"]["sensor"]["total_us"] - 1000) < 1                # 64 MHz で 64000 サイクル
    assert any("systemview" in w for w in got["warnings"])
    load = probe.rtos_timeline_load(window_us=500)
    assert load["windows"]
    assert probe.recording_find({"stream": "trace", "kind": "isr_enter"})["events"]
    assert probe.recording_find({"stream": "rtos", "kind": "run"})["events"]
    events = probe.trace_events(kinds=["task_in"])
    assert events["total"] == 3 and events["events"][1]["task_name"] == "sensor" and "t" in events["events"][1]


def test_ctf_snapshot_from_ram(probe):
    cpu = FakeCpu(image=RTT_DEMO)
    connect(probe, cpu)
    data = ctf_scenario()
    cpu.ram[0x4000:0x4000 + len(data)] = data
    out = probe.trace_snapshot("ctf", address=SRAM + 0x4000, size=1024)
    assert out["events"] == 12 and out["read"]["source"] == "ram_tracing" and out["timeline"]["made"]
    assert out["clock"]["anchor_t"] > 0 and probe.trace_events(kinds=["marker"])["events"][0]["text"] == "frame"


def test_event_recorder_is_read_while_running_and_formatted_with_scvd(probe):
    cpu = FakeCpu(image=RTT_DEMO)
    connect(probe, cpu)
    probe.scvd_load([str(SCVD_DATA / "RTX5.scvd"), str(SCVD_DATA / "EventRecorder.scvd")])
    r = Recorder(count=16)
    info_at, status_at, buf_at = SRAM + 0x5000, SRAM + 0x40, SRAM + 0x100

    def publish():
        info = bytearray(r.info())
        struct.pack_into("<II", info, 8, buf_at, 0)
        struct.pack_into("<I", info, 16, status_at)
        cpu.ram[info_at - SRAM:info_at - SRAM + 24] = info
        cpu.ram[status_at - SRAM:status_at - SRAM + 36] = r.status()
        cpu.ram[buf_at - SRAM:buf_at - SRAM + len(r.buf)] = r.buf
    r.record2(0xF203, 100, 0x2000_1000, 0)
    r.record2(0xF219, 200, 0x2000_1000, 0)
    publish()
    probe.trace_start("eventrecorder", source="memory", address=info_at, period_s=0.01)
    r.record2(0xF219, 300, 0x2000_2000, 0)
    publish()
    wait_for(lambda: probe.trace_status()["events"] == 3)
    stopped = probe.trace_stop()
    names = [e["name"] for e in probe.trace_events()["events"]]
    assert names == ["ThreadCreated", "ThreadSwitched", "ThreadSwitched"] and stopped["timeline"]["made"]


def test_component_viewer_reads_target_memory(probe, tmp_path):
    cpu = rtt_probe(probe)
    scvd = tmp_path / "rtt.scvd"
    scvd.write_text("""<?xml version="1.0" encoding="utf-8"?>
<component_viewer schemaVersion="1.2.0">
  <component name="RTT" version="1"/>
  <typedefs><typedef name="cb_t" size="24"><member name="up" type="uint32_t" offset="16"/>
    <member name="down" type="uint32_t" offset="20"/></typedef></typedefs>
  <objects><object name="RTT">
    <read name="cb" type="cb_t" symbol="_SEGGER_RTT"/>
    <out name="RTT"><item property="Buffers" value="up %d[cb.up], down %d[cb.down] at %x[cb._addr]"/></out>
  </object></objects>
</component_viewer>""", encoding="utf-8")
    loaded = probe.scvd_load([str(scvd)])
    assert loaded["objects"] == ["RTT"]
    view = probe.scvd_view()
    cb = Symbols(RTT_DEMO).variable("_SEGGER_RTT").address
    assert view["objects"][0]["children"] == [{"property": "Buffers", "value": f"up 2, down 1 at 0x{cb:X}"}]


def test_trace_import_from_a_file_feeds_the_timeline_analysis(probe, tmp_path):
    path = tmp_path / "trace.ctf"
    path.write_bytes(ctf_scenario())
    out = probe.trace_import("ctf", str(path))
    assert out["events"] == 12 and out["timeline"]["made"] and not out["timeline"]["recorded"]
    assert probe.rtos_timeline_get()["runs"]
    with pytest.raises(ValueError):
        probe.trace_import("eventrecorder", str(path))
