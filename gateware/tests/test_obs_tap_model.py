import pytest

from fprobe_gw.rtl import target_ip
from fprobe_gw.toolchain.sim import run_cocotb

SOURCES = ("fprobe_obs/rtl/obs_async_fifo.vhd", "fprobe_obs/rtl/obs_core.vhd", "fprobe_obs/rtl/obs_tck_sampler.vhd",
           "fprobe_obs/rtl/obs_soft_tap.vhd", "fprobe_obs/rtl/obs_soft_core.vhd")


@pytest.mark.parametrize("one_clock", [True, False])
def test_obs_tap_matches_1149_1_model(tmp_path, one_clock):
    run_cocotb(toplevel="obs_soft_core", sources=target_ip(*SOURCES),
               generics={"FIFO_DEPTH": 16, "ONE_CLOCK": one_clock}, test_module="tests.tb.obs_tap_model_tb",
               build_dir=tmp_path)


def test_obs_snap_core_matches_model(tmp_path):
    run_cocotb(toplevel="obs_snap_core",
               sources=target_ip("fprobe_obs/rtl/obs_tck_sampler.vhd", "fprobe_obs/rtl/obs_soft_tap.vhd",
                                 "fprobe_obs/rtl/obs_snap_core.vhd"),
               test_module="tests.tb.obs_snap_model_tb", build_dir=tmp_path)
