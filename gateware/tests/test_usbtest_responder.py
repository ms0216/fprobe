from fprobe_gw.rtl import rtl
from fprobe_gw.toolchain.sim import run_cocotb


def test_usbtest_responder(tmp_path):
    run_cocotb(toplevel="usbtest_responder", sources=rtl("designs/usbtest_responder.vhd"),
               test_module="tests.tb.usbtest_responder_tb", build_dir=tmp_path)
