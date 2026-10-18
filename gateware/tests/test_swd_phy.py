from fprobe_gw.rtl import rtl
from fprobe_gw.toolchain.sim import run_cocotb
from tests.rtl import TEST_RTL


def test_swd_phy(tmp_path):
    run_cocotb(toplevel="swd_phy_harness",
               sources=[*rtl("debug/swd_phy.vhd"), TEST_RTL / "swd_phy_harness.vhd"],
               test_module="tests.tb.swd_phy_tb", build_dir=tmp_path)
