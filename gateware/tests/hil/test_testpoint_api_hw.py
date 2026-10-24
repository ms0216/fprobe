"""テストポイントの道具（U17、`testpoint_*`）の実機試験: 挿入済みの試験用ファーム（host/firmware/testpoint_demo）を
Nucleo F401RE の SRAM で走らせ、道具の口から受け取り・解析・記録を確かめる。

配線（今のまま）: 2 線は CLK = PA5 → ch4、DAT = PA9 → ch5。ITM は SWO = PB3 → ch6（ファームが TPIU を 2 Mbaud・NRZ に
設定する。コアは 16 MHz の HSI）。並列（PC0〜PC3 → ch1・ch5・ch6・ch4）は配線を替えるので、環境変数
FPROBE_TP_PARALLEL_WIRED=1 のときだけ走らせる（仕様 docs/superpowers/specs/2026-10-17-f7-testpoint-design.md の 6・8 節）。
"""

import os
import time
from pathlib import Path

import pytest

from fprobe.mcp_server import Probe
from tests.hil.program import SERIAL, program

pytestmark = pytest.mark.hil
DEMO = Path(__file__).resolve().parents[3] / "host" / "firmware" / "testpoint_demo"
MAP = str(DEMO / "testpoints.json")
FUNCTIONS = {"step", "fib", "validate", "classify", "sum_until", "parse"}


@pytest.fixture(scope="module")
def probe():
    program("fprobe")
    p = Probe(SERIAL)
    p.target_connect("stm32f401xe", frequency_hz=4_000_000)
    yield p
    p.target_reset()
    p.close()


def run_demo(probe, elf: str) -> None:
    probe.program_ram(str(DEMO / elf))
    probe.target_resume()
    time.sleep(0.1)


def test_two_wire_deep_capture_through_the_tools(probe):
    run_demo(probe, "testpoint_demo_serial.elf")
    started = probe.testpoint_start("gpio_serial", map_path=MAP, clock_channel=4, data_channel=5, guard_samples=1, rate_hz=50e6,
                                    after_beats=100_000)
    assert started["capture"] == "deep"
    stopped = probe.testpoint_stop(timeout_s=10)
    assert stopped["hits"] > 1000 and stopped["losses"] <= 2, stopped        # 端で切れた記録だけ
    report = probe.testpoint_report(top=50)
    assert FUNCTIONS <= {f["name"] for f in report["functions"]}
    other = {(a["type"], a["function"]) for a in report["anomalies"]
             if a["type"] not in ("loss", "no_exit", "exit_without_entry")}
    assert other <= {("long_call", "fib")}, other                              # fib は引数で仕事が変わる
    cov = probe.testpoint_coverage()
    assert cov["c0"][0] > 0 and cov["c1"][0] > 0
    calls = probe.testpoint_calls(function="fib", limit=20)
    assert calls["total"] > 0 and "fib" in calls["text"]
    assert probe.la_view(width=100)["capture"]["source"] == "testpoint"          # 捕捉は画面の波形にも


def test_two_wire_stream_runs_long_and_goes_to_the_recording(probe):
    run_demo(probe, "testpoint_demo_serial.elf")
    probe.recording_start(name="testpoint-stream")
    try:
        probe.testpoint_start("gpio_serial", map_path=MAP, clock_channel=4, data_channel=5, guard_samples=1, capture="stream",
                              rate_hz=25e6, rle=True, duration_s=5.0, bucket_s=1.0,
                              long_s=100e-6)   # 長い呼び出しの印は明示したときだけ（inject_delay の spin(2000) は約 0.5 ms）
        time.sleep(2.5)
        middle = probe.testpoint_report(top=5)
        assert middle["running"] and middle["hits"] > 1000
        probe.variable_write("inject_delay", 1)                                  # classify を 1 回だけ遅くする
        stopped = probe.testpoint_stop(timeout_s=15)
        assert not stopped["stream"]["overrun"] and "error" not in stopped, stopped
        assert stopped["stream"]["duration_s"] >= 4.5 and stopped["losses"] <= 2
        longs = [a for a in probe.testpoint_report(anomalies=1000)["anomalies"]
                 if a["type"] == "long_call" and a["function"] == "classify"]
        assert longs, "inject_delay で classify の long_call が出る"
        found = probe.recording_find({"stream": "testpoint", "kind": "call", "body": {"function": "classify"}})
        assert found["count"] > 100
        # 解析の t（タイムベース）と記録の時刻が一致する
        first = probe.testpoint_calls(function="step", limit=1)["calls"][0]
        assert probe.recording_find({"stream": "testpoint", "kind": "call", "t0": first["t"], "t1": first["t"] + 1,
                                     "body": {"function": "step"}})["count"] == 1
    finally:
        probe.recording_stop()


def test_itm_gives_the_same_functions_with_device_times(probe):
    run_demo(probe, "testpoint_demo_itm.elf")
    probe.testpoint_start("itm", map_path=MAP, port=1, swo_baud=2_000_000)
    time.sleep(1.0)
    middle = probe.testpoint_status()
    assert middle["running"] and middle["hits"] > 100, middle
    stopped = probe.testpoint_stop()
    assert stopped["losses"] == 0, stopped["anomalies"]                       # 受信を始めてから出力を有効にした
    assert FUNCTIONS <= {f["name"] for f in probe.testpoint_report(top=50)["functions"]}
    assert stopped["window"]["lost_events"] == 0


@pytest.mark.skipif(os.environ.get("FPROBE_TP_PARALLEL_WIRED") != "1",
                    reason="並列は配線を替える（ch1 ← PC0、ch5 ← PC1、ch6 ← PC2、ch4 ← PC3）")
def test_parallel_deep_capture_through_the_tools(probe):
    run_demo(probe, "testpoint_demo_parallel.elf")
    probe.testpoint_start("gpio_parallel", map_path=MAP, data_channels=[1, 5, 6], strobe_channel=4, rate_hz=100e6,
                          after_beats=100_000)
    stopped = probe.testpoint_stop(timeout_s=10)
    assert stopped["hits"] > 1000 and stopped["losses"] <= 2, stopped
    assert FUNCTIONS <= {f["name"] for f in probe.testpoint_report(top=50)["functions"]}
