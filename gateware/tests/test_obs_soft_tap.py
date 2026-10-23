from fprobe_gw.rtl import target_ip
from fprobe_gw.toolchain.sim import run_cocotb
from tests.rtl import TEST_RTL


def test_obs_soft_tap(tmp_path):
    run_cocotb(toplevel="obs_tap_harness",
               sources=[*target_ip("fprobe_obs/rtl/obs_async_fifo.vhd", "fprobe_obs/rtl/obs_core.vhd",
                                   "fprobe_obs/rtl/obs_soft_tap.vhd"), TEST_RTL / "obs_tap_harness.vhd"],
               test_module="tests.tb.obs_soft_tap_tb", build_dir=tmp_path)
