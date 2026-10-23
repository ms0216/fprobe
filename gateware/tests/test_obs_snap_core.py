from fprobe_gw.rtl import target_ip
from fprobe_gw.toolchain.sim import run_cocotb


def test_obs_snap_core(tmp_path):
    run_cocotb(toplevel="obs_snap_core",
               sources=target_ip("fprobe_obs/rtl/obs_tck_sampler.vhd", "fprobe_obs/rtl/obs_soft_tap.vhd",
                                 "fprobe_obs/rtl/obs_snap_core.vhd"),
               test_module="tests.tb.obs_snap_core_tb", build_dir=tmp_path)
