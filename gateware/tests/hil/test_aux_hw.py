"""M11: 補助マイコン（Pico）が測ったターゲット電圧を、fprobe の CSR・イベント・MCP から読む。

配線（docs/superpowers/specs/2026-10-17-m11-aux-pico-design.md §3）: Pico GP0 → J14 の 5 番、GND 共通、
GP26 → 測る電圧。Pico には MicroPython と host/firmware/aux_pico/main.py を入れておく。

GP26 の先は配線で変わる（Nucleo の 3V3、校正用の ESP32 の DAC 等）ので、既定では「計測値が届き、範囲が妥当」だけを確かめる。
電圧そのものは、環境変数 FPROBE_AUX_EXPECT_V（例 3.3、範囲外を期待するなら over）を与えたときだけ確かめる。
"""

import os
import time

import pytest

from fprobe.aux_mcu import OVER_RANGE_MV, Aux
from fprobe.events import EventLog, resolve
from fprobe.mcp_server import Probe
from fprobe.session import Session
from tests.hil.program import SERIAL, program

pytestmark = pytest.mark.hil
EXPECT = os.environ.get("FPROBE_AUX_EXPECT_V")


def plausible(over_range: bool, volts: float | None) -> bool:
    """配線に合った値か。EXPECT が無ければ ADC の範囲（0〜3.4 V）か範囲外の報告。"""
    if EXPECT == "over":
        return over_range
    if EXPECT:
        return not over_range and abs(volts - float(EXPECT)) <= 0.1
    return over_range or 0.0 <= volts <= 3.4


@pytest.fixture(scope="module")
def programmed():
    program("fprobe")


def test_target_voltage_is_measured(programmed):
    with Session.open(SERIAL) as s:
        r = Aux(s.device).read()
    assert r.live, "Pico が計測値を送っていない（配線・プログラムを確認）"
    assert plausible(r.over_range, r.target_volts), r
    assert r.errors == 0


def test_samples_are_recorded_as_events_on_the_common_timebase(programmed):
    with Session.open(SERIAL) as s:
        log = EventLog(s.device)
        log.start()
        time.sleep(0.05)
        log.stop()
        hz = s.device.capabilities().timebase_hz
        samples = [a for a in resolve(log.read()) if a.kind == "aux" and a.address == 0]
    assert 40 <= len(samples) <= 60                                   # 1 ms ごと
    gaps = [(b.time - a.time) / hz for a, b in zip(samples, samples[1:])]
    assert all(0.8e-3 < gap < 1.2e-3 for gap in gaps), gaps
    assert all(a.address == 0 and plausible(a.value == OVER_RANGE_MV, a.value / 1000) for a in samples)


def test_mcp_reports_the_target_voltage(programmed):
    probe = Probe(SERIAL)
    try:
        r = probe.target_voltage()
    finally:
        probe.close()
    assert r["live"] and plausible(r["over_range"], r["volts"])


@pytest.mark.skip(reason="補助は Pico WH（2026-10-17〜）。Pico W の GP29 は無線チップと共用で ch 1 を送らない")
def test_pico_supply_on_channel_1_checks_the_whole_path(programmed):
    # ch 1 = Pico 自身の VSYS（USB の 5V からショットキー 1 段、基板上で 1/3 に分圧して ADC3）。振り切れない実際の電圧で、
    # ADC → UART → FPGA → イベント記録 → PC の経路が値を正しく運ぶことを、追加の部品なしで確かめる
    with Session.open(SERIAL) as s:
        log = EventLog(s.device)
        log.start()
        time.sleep(0.1)
        log.stop()
        supply = [a.value for a in resolve(log.read()) if a.kind == "aux" and a.address == 1]
    assert 8 <= len(supply) <= 12                                      # 10 ms ごと
    assert all(4300 <= mv <= 5300 for mv in supply), supply
    assert max(supply) - min(supply) < 100                             # 平均した値はほぼ一定
