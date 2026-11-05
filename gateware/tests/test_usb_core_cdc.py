from fprobe_gw.toolchain.sim import run_cocotb
from fprobe_gw.rtl import rtl


def test_usb_core_cdc(tmp_path):
    run_cocotb(toplevel="usb_core_cdc",
               sources=rtl("common/util_pkg.vhd", "common/sync_bit.vhd", "common/async_fifo.vhd", "common/cdc_value.vhd",
                           "usb/usb_core_cdc.vhd"),
               test_module="tests.tb.usb_core_cdc_tb", build_dir=tmp_path)
