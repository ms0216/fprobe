from fprobe_gw.rtl import rtl
from fprobe_gw.toolchain.sim import run_cocotb


def test_ulpi_link(tmp_path):
    run_cocotb(toplevel="ulpi_link", sources=rtl("usb/ulpi_link.vhd"),
               test_module="tests.tb.ulpi_link_tb", generics={"STARTUP_STP_CYCLES": 8},
               build_dir=tmp_path)
