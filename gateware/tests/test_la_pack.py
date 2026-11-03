from fprobe_gw.rtl import rtl
from fprobe_gw.toolchain.sim import run_cocotb


def test_la_pack(tmp_path):
    run_cocotb(parallel=True, toplevel="la_pack", sources=rtl("la/la_pack.vhd"), test_module="tests.tb.la_pack_tb",
               build_dir=tmp_path)
