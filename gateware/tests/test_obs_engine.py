import pytest

from fprobe_gw.rtl import rtl, target_ip
from fprobe_gw.toolchain.sim import run_cocotb
from tests.rtl import TEST_RTL

SOURCES = [*target_ip("fprobe_obs/rtl/obs_async_fifo.vhd", "fprobe_obs/rtl/obs_core.vhd",
                      "fprobe_obs/rtl/obs_tck_sampler.vhd", "fprobe_obs/rtl/obs_soft_tap.vhd",
                      "fprobe_obs/rtl/obs_soft_core.vhd", "fprobe_obs/rtl/obs_snap_core.vhd"),
           *rtl("debug/jtag_phy.vhd", "debug/obs_jtag_engine.vhd"), TEST_RTL / "obs_harness.vhd"]


@pytest.mark.parametrize("chain", [False, True])
def test_obs_engine(tmp_path, chain):
    """chain = True は対象の前後に BYPASS の装置を置いた鎖（鎖の試験だけ）。"""
    run_cocotb(toplevel="obs_harness", sources=SOURCES, generics={"CHAIN": chain},
               test_module="tests.tb.obs_engine_tb", build_dir=tmp_path,
               test_filter="records_flow_through_a_chain" if chain else "^(?!.*(records_flow_through_a_chain|snapshot))")


def test_obs_engine_with_a_one_clock_target(tmp_path, monkeypatch):
    """対象が 1 クロック版（TCK を標本化する。TCK は遅く）でも同じに動く。"""
    monkeypatch.setenv("OBS_ONE_CLOCK", "1")
    run_cocotb(toplevel="obs_harness", sources=SOURCES, generics={"ONE_CLOCK": True},
               test_module="tests.tb.obs_engine_tb", build_dir=tmp_path,
               test_filter="records_flow_into_events|sync_pairs_are_recorded|register_read_via_cmd")


def test_obs_engine_with_a_snapshot_target(tmp_path, monkeypatch):
    """対象が標本型（obs_snap_core。1 クロック版なので TCK は遅く）。"""
    monkeypatch.setenv("OBS_ONE_CLOCK", "1")
    run_cocotb(toplevel="obs_harness", sources=SOURCES, generics={"SNAP": True},
               test_module="tests.tb.obs_engine_tb", build_dir=tmp_path, test_filter="snapshots_carry_the_capture_time|snapshot_values_fire_the_match")
