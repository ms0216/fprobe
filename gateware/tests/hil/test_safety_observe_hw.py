"""安全と観測（止めると壊れるターゲットの見分け・走らせたままの致命的エラーの見張り・ライブ ウォッチ・データ サンプリングの
誤った式・サンプラの持ち主）の実機試験: Nucleo F401RE に RAM で走らせた試験用ファーム（dwarf_demo）。"""

import time
from pathlib import Path

import pytest

from fprobe.mcp_server import Probe
from tests.hil.program import SERIAL, program

pytestmark = pytest.mark.hil
FIRMWARE = Path(__file__).resolve().parents[3] / "host" / "firmware"
DWARF_O0 = FIRMWARE / "dwarf_demo" / "dwarf_demo_O0.elf"


@pytest.fixture(scope="module")
def probe():
    program("fprobe")
    p = Probe(SERIAL)
    p.target_connect("stm32f401xe", frequency_hz=4_000_000)
    p.program_ram(str(DWARF_O0))
    yield p
    p.target_reset()
    p.close()


def test_halt_risk_of_a_bare_metal_demo_matches_nothing(probe):
    assert probe.halt_risk()["matched"] is False


def test_live_watch_reads_globals_while_running(probe):
    probe.target_resume()
    first = probe.evaluate("walk::visits", live=True)["value"]
    time.sleep(0.2)
    assert probe.evaluate("walk::visits", live=True)["value"] != first           # 走っている間に増える
    assert probe.target_status()["state"] in ("RUNNING", "SLEEPING")
    with pytest.raises(Exception, match="止まっているとき"):
        probe.evaluate("total", live=True)                                       # ローカル変数


def test_fatal_loop_in_the_fault_handler_is_detected_without_halting(probe):
    probe.target_halt()
    probe.write_register("pc", probe._symbols.symbol("default_handler").address)
    probe.target_resume()                                                       # 故障の処理の無限ループを回る
    fatal = None
    deadline = time.monotonic() + 5
    while fatal is None and time.monotonic() < deadline:
        fatal = probe.target_status().get("fatal")
        time.sleep(0.5)
    assert fatal is not None and fatal["kind"] == "fault_loop" and fatal["function"] == "default_handler", fatal
    assert probe.target_status()["state"] in ("RUNNING", "SLEEPING")
    probe.target_halt()
    assert "fatal" not in probe.target_status()
    probe.program_ram(str(DWARF_O0))


def test_data_sampling_keeps_good_expressions_and_counts_from_the_start(probe):
    probe.target_resume()
    out = probe.watch_set(["walk::visits", "no_such_variable"], rate_hz=100)
    assert [w["name"] for w in out["watches"]] == ["walk::visits"]
    assert [r["expression"] for r in out["rejected"]] == ["no_such_variable"]
    assert probe.sampler_status()["user"] == "watch"
    time.sleep(1.0)
    status = probe.watch_status()
    assert 50 <= status["rounds"] <= 200, status                                # 開始からの数（100 Hz × 1 秒）
    assert status["device"]["rounds"] >= status["rounds"]
    probe.watch_stop()
    assert probe.sampler_status()["user"] is None
