"""PYNQ-Z2 の P1・P2: PS の中継（UDP 63920）越しに、Primer と同じホストソフトで fprobe_core を使う。

準備（一度）: `FPROBE_PYNQ_SUDO_PASSWORD=... python -m fprobe_gw pynq install`。
環境変数 FPROBE_PYNQ_HOST（PYNQ の IP。既定の 192.168.77.2 を使うなら 192.168.77.2）を入れたときだけ実行する。
FPROBE_PYNQ_TARGET=xiao なら XIAO nRF52840（hhkb-split の右手）の線も確かめる。XIAO は止めない（読むだけ）。
"""

import os
import subprocess
import sys
import time

import pytest

pytest.importorskip("pyocd")
from fprobe import generated as g  # noqa: E402
from fprobe.la import LogicAnalyzer  # noqa: E402
from fprobe.session import Session  # noqa: E402

HOST = os.environ.get("FPROBE_PYNQ_HOST")
TARGET = os.environ.get("FPROBE_PYNQ_TARGET")
pytestmark = [pytest.mark.hil, pytest.mark.skipif(not HOST, reason="FPROBE_PYNQ_HOST（PYNQ の IP）が未設定")]


@pytest.fixture(scope="module")
def session():
    if not os.environ.get("FPROBE_PYNQ_SKIP_PROGRAM"):
        subprocess.run([sys.executable, "-m", "fprobe_gw", "build", "fprobe_pynq", "--program"], check=True)
    deadline = time.monotonic() + 15                   # 中継の起動を待つ
    while True:
        try:
            s = Session.open_eth(HOST)
            break
        except (TimeoutError, OSError):
            if time.monotonic() > deadline:
                raise
            time.sleep(1)
    with s:
        yield s


def test_p1_the_same_core_answers_through_the_relay(session):
    caps = session.device.capabilities()
    assert (caps.board, caps.design, caps.host_link) == ("PYNQ-Z2", "fprobe_pynq", g.LINK_ETH)
    assert caps.jtag and caps.sampler and sum(c.kind == "input" for c in caps.la_layout) == 6
    for value in (0x5A5AC3C3, 0x12345678):
        session.device.write32(g.SYSTEM_SCRATCH, value)
        assert session.device.read32(g.SYSTEM_SCRATCH) == value


def test_p1_internal_logic_analyzer(session):
    la = LogicAnalyzer(session.device)
    period = la.start(rate_hz=1e6, pretrigger=1024)
    la.wait(timeout_s=5)
    cap = la.read(period)
    assert len(cap.samples) == la.depth and cap.trigger == 1024
    assert session.link.lost_frames == 0


@pytest.mark.skipif(TARGET != "xiao", reason="FPROBE_PYNQ_TARGET=xiao のときだけ（XIAO の線をつないだ構成）")
def test_p2_reads_the_xiao_without_halting_it(session):
    with session.debug(target_override="nrf52840", frequency=4_000_000, connect_mode="attach") as debug:
        t = debug.target
        assert t.read32(0xE000ED00) == 0x410FC241                 # Cortex-M4F の CPUID
        assert t.read32(0x10000100) == 0x52840                    # FICR.INFO.PART
        start = time.perf_counter()
        t.read_memory_block32(0x20000000, 4096)
        rate = 16384 / (time.perf_counter() - start)
    print(f"pyOCD 読み出し（PYNQ の中継、SWD 4 MHz）: {rate / 1e3:.0f} KB/s")


def test_p1_events_are_recorded_in_the_internal_ring(session):
    # 外部メモリの無い構成のイベント記録（BRAM のリング、docs/superpowers/specs/2026-10-20-bram-event-ring-design.md）
    assert session.device.read32(g.EVENTS_CAPACITY) == 1024


@pytest.mark.skipif(TARGET != "xiao", reason="FPROBE_PYNQ_TARGET=xiao のときだけ（XIAO の線をつないだ構成）")
def test_p2_swd_traffic_is_captured_and_decoded(session):
    """内蔵ロジアナ（25 MS/s）で XIAO への SWD の線を取り、SWD の解読器で TAR 書き → DRW 読みまで読み解く。"""
    from fprobe.mcp_server import Probe
    session.close()
    p = Probe(eth=HOST)
    try:
        p.target_connect("nrf52840", frequency_hz=4_000_000, connect_mode="attach")
        p.la_arm(rate_hz=25e6, pretrigger=256, fall=1 << 8)           # ch8 = プローブが SWDIO を駆動し始めた
        p.read_memory(0x10000100, 4)
        p.la_collect()
        texts = [a["text"] for a in p.la_decode("swd", {"swclk": 0, "swdio": 3, "drive": None, "turnaround": 1,
                                                        "orundetect": False}, limit=8)["annotations"]]
        assert any("TAR 0x10000100 OK" in t for t in texts), texts
        assert any(t.startswith("AP0 R DRW") for t in texts), texts
    finally:
        p.close()


@pytest.mark.skipif(TARGET != "xiao", reason="FPROBE_PYNQ_TARGET=xiao のときだけ（XIAO の線をつないだ構成）")
def test_p2_live_watch_by_name_without_halting(session):
    """名前 → DWARF の番地と型 → サンプラ → イベントのリング → 型の値。XIAO は止めない。"""
    from fprobe.mcp_server import Probe
    session.close()                                            # 同じ中継へ Probe がつなぎ直す（宛先は最後の送り主）
    p = Probe(eth=HOST)
    try:
        p.target_connect("nrf52840", frequency_hz=4_000_000, connect_mode="attach")
        p.elf_load(os.environ.get("FPROBE_XIAO_ELF", "C:/Users/M/zmk/ws/build/right-mon/zephyr/zmk.elf"))
        p.watch_set(["is_connected", "position_state"], rate_hz=1000)
        time.sleep(0.3)
        values = {v["name"]: v["value"] for v in p.watch_values()["values"]}
        status = p.watch_stop()
        assert status["errors"] == 0 and status["rounds"] > 200
        assert values["is_connected"] is True and len(values["position_state"]) == 16
        assert p.target_status()["state"] != "HALTED"
    finally:
        p.close()
