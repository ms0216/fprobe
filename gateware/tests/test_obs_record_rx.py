from fprobe_gw.rtl import rtl
from fprobe_gw.toolchain.sim import run_cocotb


def test_obs_record_rx(tmp_path):
    run_cocotb(toplevel="obs_record_rx", sources=rtl("debug/obs_record_rx.vhd"),
               test_module="tests.tb.obs_record_rx_tb", build_dir=tmp_path)
