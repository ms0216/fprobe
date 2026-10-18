from fprobe_gw.rtl import rtl
from fprobe_gw.toolchain.sim import run_cocotb


def test_trig_seq(tmp_path):
    run_cocotb(toplevel="trig_seq", sources=rtl("core/trig_seq.vhd"), test_module="tests.tb.trig_seq_tb",
               build_dir=tmp_path)
