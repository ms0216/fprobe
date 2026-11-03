from fprobe_gw.rtl import CORE_GENERATED, rtl
from fprobe_gw.toolchain.sim import run_cocotb


def test_la_front(tmp_path):
    run_cocotb(toplevel="la_front", sources=[CORE_GENERATED / "timing_pkg.vhd", *rtl("la/la_front.vhd")], test_module="tests.tb.la_front_tb",
               generics={"WIDTH": 4}, build_dir=tmp_path)
