from fprobe_gw.rtl import rtl
from fprobe_gw.toolchain.sim import run_cocotb


def test_la_filter(tmp_path):
    run_cocotb(toplevel="la_filter", sources=rtl("la/la_filter.vhd"), test_module="tests.tb.la_filter_tb",
               generics={"WIDTH": 4, "LEN_BITS": 8}, build_dir=tmp_path)


def test_la_pulse(tmp_path):
    run_cocotb(toplevel="la_pulse", sources=rtl("la/la_pulse.vhd"), test_module="tests.tb.la_pulse_tb",
               generics={"WIDTH": 4, "COUNT_BITS": 16}, build_dir=tmp_path)
