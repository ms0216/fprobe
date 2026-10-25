from fprobe_gw.rtl import rtl
from fprobe_gw.toolchain.sim import run_cocotb


def test_la_front(tmp_path):
    run_cocotb(toplevel="la_front", sources=rtl("la/la_front.vhd"), test_module="tests.tb.la_front_tb",
               generics={"WIDTH": 4}, build_dir=tmp_path)
