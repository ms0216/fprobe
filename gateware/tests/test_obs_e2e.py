"""FPGA 観測の端から端までの差分試験（tests/tb/obs_e2e_tb.py）。設定は環境変数 E2E_* で渡す。"""

import pytest

from fprobe_gw.rtl import rtl, target_ip
from fprobe_gw.toolchain.sim import run_cocotb
from tests.rtl import TEST_RTL

SOURCES = [*target_ip("fprobe_obs/rtl/obs_async_fifo.vhd", "fprobe_obs/rtl/obs_core.vhd",
                      "fprobe_obs/rtl/obs_tck_sampler.vhd", "fprobe_obs/rtl/obs_soft_tap.vhd",
                      "fprobe_obs/rtl/obs_soft_core.vhd", "fprobe_obs/rtl/obs_snap_core.vhd"),
           *rtl("debug/jtag_phy.vhd", "debug/obs_cmd_queue.vhd", "debug/obs_match.vhd", "debug/obs_record_rx.vhd", "debug/obs_jtag_engine.vhd"), TEST_RTL / "obs_harness.vhd"]


def run(tmp_path, monkeypatch, test_filter, *, one_clock=False, chain=False, snap=False, **env):
    for k, v in env.items():
        monkeypatch.setenv(f"E2E_{k.upper()}", str(v))
    monkeypatch.setenv("E2E_ONE_CLOCK", "1" if one_clock else "0")
    monkeypatch.setenv("E2E_CHAIN", "1" if chain else "0")
    monkeypatch.setenv("E2E_SNAP", "1" if snap else "0")
    run_cocotb(parallel=True, toplevel="obs_harness", sources=SOURCES,
               generics={"CHAIN": chain, "ONE_CLOCK": one_clock, "SNAP": snap},
               test_module="tests.tb.obs_e2e_tb", build_dir=tmp_path, test_filter=test_filter)


@pytest.mark.parametrize("half,sync,words,seed", [(3, 2, 8, 1), (3, 1, 8, 2), (5, 4, 4, 3), (20, 2, 8, 4), (3, 8, 2, 5)])
def test_random_changes_two_clock(tmp_path, monkeypatch, half, sync, words, seed):
    run(tmp_path, monkeypatch, "random_changes_match", half=half, sync=sync, words=words, seed=seed)


@pytest.mark.parametrize("tgt_ps,half,sync", [(23_000, 3, 2), (17_000, 3, 1), (31_700, 5, 3)])
def test_random_changes_with_an_unrelated_target_clock(tmp_path, monkeypatch, tgt_ps, half, sync):
    """対象の clk が fprobe の clk と整数比でない（位相が走る）。"""
    run(tmp_path, monkeypatch, "random_changes_match", tgt_ps=tgt_ps, half=half, sync=sync, seed=7)


@pytest.mark.parametrize("half,sync,words,seed", [(10, 2, 8, 11), (14, 1, 8, 12), (10, 4, 4, 13)])
def test_random_changes_one_clock(tmp_path, monkeypatch, half, sync, words, seed):
    run(tmp_path, monkeypatch, "random_changes_match", one_clock=True, half=half, sync=sync, words=words, seed=seed)


@pytest.mark.parametrize("one_clock,half", [(False, 3), (True, 10)])
def test_random_changes_through_a_chain(tmp_path, monkeypatch, one_clock, half):
    run(tmp_path, monkeypatch, "random_changes_match", chain=True, one_clock=one_clock, half=half, sync=2, seed=21)


@pytest.mark.parametrize("one_clock,half,words", [(False, 3, 2), (True, 10, 2)])
def test_overflow_lost_counts(tmp_path, monkeypatch, one_clock, half, words):
    run(tmp_path, monkeypatch, "lost_counts_in_a_gap", one_clock=one_clock, half=half, words=words, sync=3, seed=31)


@pytest.mark.parametrize("one_clock,half", [(False, 3), (True, 10)])
def test_reopen_and_leak_and_reset_and_quiet(tmp_path, monkeypatch, one_clock, half):
    run(tmp_path, monkeypatch, "reopening_repeatedly|unread_records|target_domain_reset", one_clock=one_clock, half=half,
        words=2, sync=3, seed=41)


def test_snapshot_values(tmp_path, monkeypatch):
    run(tmp_path, monkeypatch, "snapshot_values_have", snap=True, one_clock=True, half=10, sync=0, seed=51)


@pytest.mark.parametrize("one_clock,half,sync", [(False, 3, 3), (True, 10, 3), (False, 3, 1)])
def test_long_quiet_period(tmp_path, monkeypatch, one_clock, half, sync):
    """sync = 1 は印が 256 個を超えて番号が一周する長さ。"""
    run(tmp_path, monkeypatch, "long_quiet_period", one_clock=one_clock, half=half, words=2, sync=sync, seed=41)


def test_changes_during_a_target_reset_are_reported_as_lost(tmp_path, monkeypatch):
    run(tmp_path, monkeypatch, "changes_made_during_a_target_reset", half=3, words=8, sync=2, seed=61)
