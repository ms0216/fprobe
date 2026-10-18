"""既存のログ・トレースの形式の取り込み（U14・U15）の実機試験: Nucleo F401RE の SRAM で formats_demo。

formats_demo（host/firmware/formats_demo）: RTT の上り "Terminal"（tick の行・pc=0x… の番地）・"defmt"（rzcobs）・
"SysView"（下り "SysView" に START が来たら。worker / IDLE を 10 ms ごとに切り替え、SysTick を割り込みとして）・
"Compact"（書式の番地 + 引数）、ITM ポート 2 の Trice（枠なし、ID 100）、RAM の Event Recorder（ThreadSwitched・stdout）・
Zephyr の CTF の RAM の backend（ram_tracing。満ちたら止まる）・ThreadX の TraceX のバッファ。
走らせるのは本流の担当（`pytest gateware/tests/hil/test_formats_hw.py --hil`）。
"""

import json
import time
from pathlib import Path

import pytest

from fprobe.mcp_server import Probe
from tests.hil.program import SERIAL, program

pytestmark = pytest.mark.hil
FIRMWARE = Path(__file__).resolve().parents[3] / "host" / "firmware"
FORMATS_DEMO = FIRMWARE / "formats_demo" / "formats_demo.elf"
SCVD = Path(__file__).resolve().parents[3] / "host" / "tests" / "data" / "tracefmt"


@pytest.fixture(scope="module")
def probe():
    program("fprobe")
    p = Probe(SERIAL)
    p.target_connect("stm32f401xe", frequency_hz=4_000_000)
    yield p
    p.target_reset()
    p.close()


@pytest.fixture
def running(probe):
    probe.program_ram(str(FORMATS_DEMO))
    probe.target_resume()
    time.sleep(0.5)
    yield probe
    for stop in (probe.log_stop, probe.trace_stop):
        try:
            stop()
        except RuntimeError:
            pass
    probe.rtt_stop()


def wait_records(probe, count: int, timeout_s: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout_s
    while probe.log_status()["records"] < count and time.monotonic() < deadline:
        time.sleep(0.1)
    return probe.log_status()


def test_defmt_over_rtt(running):
    probe = running
    probe.log_start("defmt")                                         # ELF は program_ram で読み込み済み、"defmt"
    status = wait_records(probe, 30)
    assert status["records"] >= 30 and status["errors"] == 0, status
    records = probe.log_read(limit=1000)["records"]
    ticks = [r for r in records if r["level"] == "info"]
    seqs = [r["args"][1] for r in ticks]
    assert all(b == (a + 1) & 0xFFFF for a, b in zip(seqs, seqs[1:])), seqs[:20]     # 取りこぼしが無い
    assert ticks[0]["text"].startswith("tick ") and ticks[0]["timestamp_text"].endswith("000")
    late = [r for r in records if r["level"] == "warn"]
    assert late and late[0]["text"].startswith("late ") and late[0]["location"]["file"].endswith("formats_demo.c")
    probe.log_stop()


def test_text_with_code_addresses(running):
    probe = running
    probe.log_start("text", channel="Terminal")
    wait_records(probe, 12, timeout_s=3.0)
    lines = probe.log_read(limit=1000, contains="pc=0x")["records"]
    assert lines and lines[0]["addresses"][0]["function"] == "systick_handler", lines[:2]
    probe.log_stop()


def test_compact_over_rtt(running):
    probe = running
    probe.log_start("compact", channel="Compact")
    status = wait_records(probe, 20)
    assert status["errors"] == 0, status
    text = probe.log_read()["records"][0]["text"]
    assert text.startswith("compact seq=") and text.endswith(" ms"), text
    probe.log_stop()


def test_trice_over_swo(running, tmp_path):
    probe = running
    til = tmp_path / "til.json"
    til.write_text(json.dumps({"100": {"Type": "TRICE32", "Strg": "inf:seq %u\\n"}}))
    probe.log_start("trice", source="swo", port=2, til=str(til), framing="none", swo_baud=2_000_000)
    status = wait_records(probe, 20)
    assert status["errors"] == 0 and status["records"] >= 20, status
    records = probe.log_read(limit=1000)["records"]
    assert records[0]["channel"] == "inf" and records[0]["text"].startswith("seq ")
    assert not any(r.get("notes") for r in records[1:]), [r["notes"] for r in records if r.get("notes")][:3]
    assert all("t" in r for r in records)                            # FPGA が受けた時刻
    probe.log_stop()


def test_systemview_over_rtt_becomes_a_timeline(running):
    probe = running
    probe.recording_start(name="sysview")
    try:
        started = probe.trace_start("systemview")                    # 下り "SysView" に START
        assert not started["warnings"], started
        time.sleep(1.0)
        stopped = probe.trace_stop()
        assert stopped["ts_hz"] == 16e6 and stopped["errors"] == 0 and stopped["lost"] == 0, stopped
        names = {t["name"] for t in stopped["tasks"]}
        assert names == {"worker", "IDLE"}, stopped["tasks"]
        tl = stopped["timeline"]
        assert tl["made"] and tl["recorded"] and tl["isr"] > 500, tl     # SysTick 1 kHz
        worker = tl["stats"]["tasks"]["worker"]
        assert 0.3 < worker["cpu"] < 0.7, worker                          # 10 ms ごとに交互
        load = probe.rtos_timeline_load(window_us=100_000)
        assert load["windows"], load
        prints = probe.trace_events(kinds=["print"])["events"]
        assert prints and prints[0]["text"].startswith("seq="), prints[:2]
        assert probe.recording_find({"stream": "trace", "kind": "isr_enter"})["events"]
    finally:
        probe.recording_stop()


def test_ram_snapshots(running):
    probe = running
    ctf = probe.trace_snapshot("ctf")
    assert ctf["events"] > 20 and ctf["errors"] == 0 and ctf["timeline"]["made"], ctf
    assert probe.trace_events(kinds=["marker"])["events"][0]["text"] == "boot"
    tx = probe.trace_snapshot("tracex", ts_hz=16e6)
    assert tx["events"] >= 32 and {t["name"] for t in tx["tasks"]} == {"worker", "idle"}, tx
    probe.variable_write("sysview_autostart", 1)                         # 下りのコマンドを待たずに始める
    time.sleep(0.3)
    probe.target_halt()
    sv = probe.trace_snapshot("systemview")                              # リングを消費せずに（SKIP: 最初の 4 KB）
    assert sv["ts_hz"] == 16e6 and sv["errors"] == 0 and sv["timeline"]["made"], sv
    probe.target_resume()


def test_event_recorder_while_running_with_scvd(running):
    probe = running
    probe.scvd_load([str(SCVD / "RTX5.scvd"), str(SCVD / "EventRecorder.scvd")])
    probe.trace_start("eventrecorder", source="memory", period_s=0.02)
    time.sleep(1.0)
    stopped = probe.trace_stop()
    assert stopped["events"] > 50 and stopped["ts_hz"] == 1000, stopped
    assert stopped["lost"] < stopped["events"] // 10, stopped                # 16 件のリングを 20 ms ごとに読む
    switched = probe.trace_events(kinds=["task_in"])["events"]
    assert switched and switched[0]["name"] == "ThreadSwitched"
    prints = probe.trace_events(kinds=["print"])["events"]
    assert prints and prints[0]["text"].startswith("seq "), prints[:2]


def test_component_viewer_on_the_target(running):
    probe = running
    probe.scvd_load([str(SCVD / "RTX5.scvd")])
    view = probe.scvd_view()                                             # RTX は無い: 式の誤り無く空に近い表示
    assert view["objects"][0]["property"] == "RTX RTOS Object", view
