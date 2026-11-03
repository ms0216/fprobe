from fprobe_gw.rtl import rtl
from fprobe_gw.toolchain.sim import run_cocotb
from tests.rtl import TEST_RTL


def test_la_testgen(tmp_path):
    run_cocotb(parallel=True, toplevel="la_testgen", sources=rtl("common/sync_bit.vhd", "common/cdc_value.vhd", "la/la_testgen.vhd"),
               test_module="tests.tb.la_testgen_tb", build_dir=tmp_path)


def test_la_testgen_same_clock(tmp_path):
    # fprobe_pynq_deep の形: clk と cap_clk が同じクロック。cdc_value が同一クロックでも値を届けること
    run_cocotb(parallel=True, toplevel="la_testgen_same_clk_harness",
               sources=[*rtl("common/sync_bit.vhd", "common/cdc_value.vhd", "la/la_testgen.vhd"),
                        TEST_RTL / "la_testgen_same_clk_harness.vhd"],
               test_module="tests.tb.la_testgen_same_clk_tb", build_dir=tmp_path)
