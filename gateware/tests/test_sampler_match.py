from fprobe_gw.rtl import rtl
from fprobe_gw.toolchain.sim import run_cocotb


def test_sampler_match(tmp_path):
    run_cocotb(toplevel="sampler_match", sources=rtl("debug/sampler_match.vhd"), test_module="tests.tb.sampler_match_tb",
               build_dir=tmp_path)


def test_sampler_match_fuzz(tmp_path):
    run_cocotb(toplevel="sampler_match", sources=rtl("debug/sampler_match.vhd"),
               test_module="tests.tb.sampler_match_fuzz_tb", build_dir=tmp_path)
