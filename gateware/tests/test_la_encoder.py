from fprobe_gw.rtl import rtl
from fprobe_gw.toolchain.sim import run_cocotb


def test_la_encoder(tmp_path):
    run_cocotb(toplevel="la_encoder", sources=rtl("la/la_encoder.vhd"), test_module="tests.tb.la_encoder_tb",
               build_dir=tmp_path)
