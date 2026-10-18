from fprobe_gw.rtl import rtl
from fprobe_gw.toolchain.sim import run_cocotb


def test_usb_cdc_acm_requests(tmp_path):
    run_cocotb(toplevel="usb_cdc_acm_requests", sources=rtl("usb/usb_cdc_acm_requests.vhd"),
               test_module="tests.tb.usb_cdc_acm_requests_tb", generics={"COMM_INTERFACE": 1, "CLK_HZ": 10_000},
               build_dir=tmp_path)


def test_usb_cdc_notify(tmp_path):
    run_cocotb(toplevel="usb_cdc_notify", sources=rtl("usb/usb_cdc_notify.vhd"),
               test_module="tests.tb.usb_cdc_notify_tb", generics={"COMM_INTERFACE": 2}, build_dir=tmp_path)
