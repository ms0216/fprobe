from fprobe_gw.rtl import rtl
from fprobe_gw.toolchain.sim import run_cocotb


def test_jtag_phy(tmp_path):
    run_cocotb(toplevel="jtag_phy", sources=rtl("debug/jtag_phy.vhd"), test_module="tests.tb.jtag_phy_tb",
               build_dir=tmp_path)
