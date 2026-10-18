from fprobe_gw.rtl import rtl
from fprobe_gw.toolchain.sim import run_cocotb
from tests.rtl import TEST_RTL


def test_usb_crc_pkg(tmp_path):
    run_cocotb(toplevel="usb_crc_harness",
               sources=[*rtl("usb/usb_crc_pkg.vhd"), TEST_RTL / "usb_crc_harness.vhd"],
               test_module="tests.tb.usb_crc_tb", build_dir=tmp_path)
