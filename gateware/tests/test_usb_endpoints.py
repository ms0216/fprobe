from fprobe_gw.rtl import rtl
from fprobe_gw.toolchain.sim import run_cocotb


def test_usb_ep_out(tmp_path):
    run_cocotb(toplevel="usb_ep_out", sources=rtl("common/util_pkg.vhd", "usb/usb_ep_out.vhd"),
               test_module="tests.tb.usb_ep_out_tb", generics={"DEPTH": 64, "MPS": 16},
               build_dir=tmp_path)


def test_usb_ep_in(tmp_path):
    run_cocotb(toplevel="usb_ep_in", sources=rtl("common/util_pkg.vhd", "usb/usb_ep_in.vhd"),
               test_module="tests.tb.usb_ep_in_tb",
               generics={"DEPTH": 64, "MPS": 16, "FLUSH_CYCLES": 20}, build_dir=tmp_path,
               test_filter="^(?!.*boundary_without_zlp)")


def test_usb_ep_in_without_zlp(tmp_path):
    run_cocotb(toplevel="usb_ep_in", sources=rtl("common/util_pkg.vhd", "usb/usb_ep_in.vhd"),
               test_module="tests.tb.usb_ep_in_tb",
               generics={"DEPTH": 64, "MPS": 16, "FLUSH_CYCLES": 20, "ZLP": False}, build_dir=tmp_path,
               test_filter="boundary_without_zlp")
