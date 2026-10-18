"""SWO の DWT・ITM の設定（U11）とサイクルの計測（U12）の実機試験: Nucleo F401RE（Cortex-M4、HSI 16 MHz）の SRAM で
sram_demo を走らせる。確かめること:

- 走行中に swo_configure してもターゲットが止まらない（MCUX-06）。
- DWT のデータトレース: counter への書き込みが値・PC（tick）・時刻付きで、抜けなく続けて届く（パケットの順 = PC → 値、
  FUNCTION 0b1111 の符号化が正しい）。
- 値一致のウォッチポイント（比較器 1 の DATAVMATCH + 番地の比較器のリンク）で milestone が指定の値のときだけ止まる。
- イベントカウンタの溢れのパケットが届く。
- cycles_measure（reached_milestone の 1 行）と cycles_clock（16 MHz ± HSI の誤差）。
"""

import time
from pathlib import Path

import pytest
from pyocd.core.target import Target

from fprobe.mcp_server import Probe
from tests.hil.program import SERIAL, program

pytestmark = pytest.mark.hil
SRAM_DEMO = Path(__file__).resolve().parents[3] / "host" / "firmware" / "sram_demo" / "sram_demo.elf"
CLOCK = 16e6                                                         # リセット後の HSI


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
    probe.program_ram(str(SRAM_DEMO))
    probe.target_resume()
    time.sleep(0.1)
    yield probe
    if probe._swo_run is not None and probe._swo_run.running:
        probe.swo_unconfigure()
    probe.breakpoint_clear()


def test_data_trace_without_halting(running):
    """milestone（tick の 256 回ごと。毎秒約 3000 回 = 30 KB/s）への書き込みが、値・PC・時刻付きで、止めずに抜けなく届く。
    counter（毎秒約 80 万回）は SWO の帯域（2 Mbaud）を超えるので、溢れたことが数えられる。"""
    probe = running
    before = probe.cycles_read()["cyccnt"]
    out = probe.swo_configure(data_trace=[{"expression": "milestone"}], swo_baud=2_000_000, core_clock_hz=CLOCK)
    assert out["trace_pin"] == "DBG.DBGMCU_CR" and out["core"]["name"] == "Cortex-M4", out
    assert probe.target.get_state() == Target.State.RUNNING                     # 設定で止まらない
    time.sleep(0.5)
    trace = probe.swo_trace(limit=5000)
    assert probe.target.get_state() == Target.State.RUNNING
    assert probe.cycles_read()["cyccnt"] != before
    accesses = [a for a in trace["accesses"] if a["complete"]]
    assert len(accesses) > 300 and trace["lost"] == 0 and trace["overflows"] == 0, {k: trace[k] for k in ("lost", "overflows")}
    assert all(a["function"] == "reached_milestone" and a["write"] for a in accesses), accesses[:3]
    assert all(b["value"] - a["value"] == 256 for a, b in zip(accesses, accesses[1:])), accesses[:5]
    assert all(b["t"] > a["t"] for a, b in zip(accesses, accesses[1:]))
    probe.swo_unconfigure()
    probe.swo_configure(data_trace=[{"expression": "counter"}], swo_baud=2_000_000, core_clock_hz=CLOCK)
    time.sleep(0.2)
    assert probe.swo_trace(limit=10)["overflows"] > 0                         # 帯域を超えたことも分かる
    stopped = probe.swo_unconfigure()
    assert stopped["running"] is False


def test_value_watch_halts_only_on_the_value(running):
    probe = running
    target = probe.variable_read("milestone")["value"] + 256 * 2000           # 約 0.6 秒後に milestone がとる値
    out = probe.swo_configure(value_watch=[{"expression": "milestone", "value": target}], trace_pin=False)
    roles = {c["role"]: c["index"] for c in out["plan"]["comparators"]}
    assert roles == {"value_watch": 1, "link": 3}, out
    stop = probe.run_until_halt(timeout_s=3)
    assert stop["reason"] == "watchpoint", stop
    assert probe.variable_read("milestone")["value"] == target
    probe.swo_unconfigure()


def test_event_counters_and_cycles_between_breakpoints(running):
    probe = running
    probe.swo_configure(counters=["cpi", "exc", "sleep", "lsu", "fold"], swo_baud=2_000_000, core_clock_hz=CLOCK)   # 全部数えると正確
    time.sleep(0.3)
    counters = probe.swo_trace()["counters"]
    assert counters["cpi"] > 0 and counters["lsu"] > 0, counters
    out = probe.cycles_measure("sram_demo.c:13", "sram_demo.c:14", count=3, core_clock_hz=CLOCK)   # milestone = value の 1 行
    assert len(out["intervals"]) == 3 and all(0 < c < 200 for c in out["intervals"]), out
    assert out["counters_exact"] and all(c["instructions"] > 0 for c in out["counters"]), out
    probe.swo_unconfigure()


def test_cpu_clock(running):
    out = running.cycles_clock(duration_s=1.0, rate_hz=200)
    assert out["method"] == "sampler" and out["clock_hz"] == pytest.approx(CLOCK, rel=0.02), out
    assert out["ppm"] < 1000 and not out["warnings"], out
