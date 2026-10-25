from fprobe_gw.rtl import rtl
from fprobe_gw.toolchain.sim import run_cocotb


def test_obs_match(tmp_path):
    run_cocotb(toplevel="obs_match", sources=rtl("debug/obs_match.vhd"),
               test_module="tests.tb.obs_match_tb", build_dir=tmp_path)
