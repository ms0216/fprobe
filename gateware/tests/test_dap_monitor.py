from fprobe_gw.rtl import rtl
from fprobe_gw.toolchain.sim import run_cocotb


def test_dap_monitor(tmp_path):
    run_cocotb(toplevel="dap_monitor", sources=rtl("debug/dap_monitor.vhd"), test_module="tests.tb.dap_monitor_tb",
               build_dir=tmp_path)
