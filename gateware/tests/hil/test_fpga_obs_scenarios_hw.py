"""FPGA 観測の実機のシナリオ（自己デバッグ。仕様 docs/superpowers/specs/2026-10-25-fprobe-self-debug-design.md）。

準備は test_fpga_obs_hw.py と同じ（PYNQ の fprobe_pynq_deep + Primer の obs_target_primer + 観測の 6 本）。FPROBE_OBS_MCU=1 のときは、さらに
PYNQ の PMOD JA に Nucleo F401RE を SWD でつなぐ（docs/boards/pynq-z2.md の JA の割り当て）と、FPGA の条件 → MCU の停止も試す。
環境変数 FPROBE_PYNQ_HOST と FPROBE_OBS_TARGET=1 のときだけ実行。Probe（API の実装）を直接使うのでデーモンは要らない。"""
import os
import time
from pathlib import Path

import pytest

from fprobe.mcp_server import Probe

HOST = os.environ.get("FPROBE_PYNQ_HOST")
pytestmark = [pytest.mark.hil,
              pytest.mark.skipif(not HOST or not os.environ.get("FPROBE_OBS_TARGET"),
                                 reason="FPROBE_PYNQ_HOST と FPROBE_OBS_TARGET（Primer の代役と配線）が要る")]
MCU = pytest.mark.skipif(not os.environ.get("FPROBE_OBS_MCU"), reason="FPROBE_OBS_MCU（PYNQ の JA に Nucleo）が要る")
SRAM_DEMO = Path(__file__).resolve().parents[3] / "host" / "firmware" / "sram_demo" / "sram_demo.elf"
COUNTER_HZ = 27_000_000 / 4096
OPEN = dict(chain={"ir_len": 4}, user_ir=2, half_period=5, words=64, sync_period=20)
SIGNALS = {"groups": {"0": [{"name": "mark", "lsb": 0, "width": 1}, {"name": "counter", "lsb": 1, "width": 15, "format": "dec"}]}}


@pytest.fixture
def probe(tmp_path, monkeypatch):
    monkeypatch.setenv("FPROBE_HOME", str(tmp_path))
    p = Probe(eth=HOST)
    try:
        yield p
    finally:
        for stop in (p.fpga_obs_close, p.events_stop, p.target_disconnect):
            try:
                stop()
            except Exception:                                   # noqa: BLE001  後片付け（開いていないものは断られる）
                pass
        p.close()


def test_the_declaration_registers_match_the_readme(probe):
    # README のレジスタ: 0・1 ID "FOBS"、2 VERSION = 2、3 GROUPS、4 FIFO_DEPTH、5・6 CLOCK_HZ、9 LOST_BITS
    info = probe.fpga_obs_open(**OPEN)
    regs = {a: probe.fpga_obs_register_read(a)["value"] for a in range(10)}
    assert (regs[1] << 16 | regs[0]) == 0x464F4253 == info["id"]
    assert regs[2] == info["version"] == 2 and regs[3] == info["groups"] == 2 and regs[4] == info["fifo_depth"]
    assert (regs[6] << 16 | regs[5]) == info["clock_hz"] == 27_000_000 and regs[9] == info["lost_bits"]


def test_open_close_and_start_stop_many_times_leave_no_stuck_state(probe):
    for _ in range(20):
        probe.fpga_obs_open(**OPEN)
        probe.fpga_obs_close()
    probe.fpga_obs_open(**OPEN)
    for _ in range(20):
        probe.fpga_obs_start()
        time.sleep(0.03)
        probe.fpga_obs_stop()
    status = probe.fpga_obs_status()
    assert not status["stuck"] and status["running"]            # stop は記録を止めるだけ。走査（線の駆動）は close まで続く


def test_group_switching_is_recorded_as_group_markers(probe):
    probe.events_start()
    probe.fpga_obs_open(**OPEN)
    probe.fpga_obs_start()
    for group in (0, 1, 0):
        probe.fpga_obs_select(group)
        time.sleep(0.4)
    got = probe.fpga_obs_read(last=100_000)
    kinds = [i.get("kind") for i in got["items"]]
    assert kinds.count("group") >= 3
    values0 = [i for i in got["items"] if i.get("kind") == "value" and i.get("group") == 0]
    assert len(values0) > 0.6 * COUNTER_HZ * 0.8 * 0.5          # 組 0 を観測した約 0.8 秒の分（余裕を見て半分以上）


def test_hardware_trigger_counts_every_rising_edge_of_the_marker(probe):
    probe.fpga_obs_signals_set(signals=SIGNALS)
    probe.events_start()
    probe.fpga_obs_open(**OPEN)
    probe.fpga_obs_start()
    probe.fpga_obs_trigger_set(condition="mark rose", halt=False, la=True)      # halt も la も False は断られる（成立先が無い）
    time.sleep(1.0)
    first = probe.fpga_obs_trigger_status()["count"]
    time.sleep(1.0)
    second = probe.fpga_obs_trigger_status()["count"]
    rate = second - first
    assert rate == pytest.approx(COUNTER_HZ / 2, rel=0.05)      # bit12 が 1 になる立ち上がりは、カウンタの変化 2 回に 1 回
    probe.fpga_obs_trigger_clear()


@MCU
def test_an_fpga_condition_halts_the_running_mcu(probe):
    probe.fpga_obs_signals_set(signals=SIGNALS)
    probe.target_connect(target="stm32f401xe", frequency_hz=4_000_000, connect_mode="halt")
    probe.program_ram(path=str(SRAM_DEMO))
    probe.target_resume()
    time.sleep(0.3)
    assert probe.target_status()["state"] == "RUNNING"
    probe.events_start()
    probe.fpga_obs_open(**OPEN)
    probe.fpga_obs_start()
    probe.fpga_obs_trigger_set(condition="mark rose", halt=True, la=False)
    deadline = time.monotonic() + 2.0
    while probe.target_status()["state"] != "HALTED" and time.monotonic() < deadline:
        time.sleep(0.01)
    assert probe.target_status()["state"] == "HALTED"
    assert probe.fpga_obs_trigger_status()["count"] >= 1
    probe.fpga_obs_trigger_clear()
