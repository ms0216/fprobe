from fprobe_gw.rtl import rtl
from fprobe_gw.toolchain.sim import run_cocotb


def test_la_capture(tmp_path):
    run_cocotb(toplevel="la_capture", sources=rtl("common/util_pkg.vhd", "la/la_front.vhd", "la/la_capture.vhd"),
               test_module="tests.tb.la_capture_tb", generics={"WIDTH": 4, "DEPTH": 32}, build_dir=tmp_path)
