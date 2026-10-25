from fprobe_gw.rtl import rtl
from fprobe_gw.toolchain.sim import run_cocotb


def test_udiv(tmp_path):
    run_cocotb(toplevel="udiv", sources=rtl("common/udiv.vhd"), test_module="tests.tb.udiv_tb",
               generics={"WIDTH": 32}, build_dir=tmp_path)


def test_udiv_fuzz(tmp_path):
    run_cocotb(toplevel="udiv", sources=rtl("common/udiv.vhd"), test_module="tests.tb.udiv_fuzz_tb",
               generics={"WIDTH": 32}, build_dir=tmp_path)


def test_cdc_value(tmp_path):
    run_cocotb(toplevel="cdc_value", sources=rtl("common/sync_bit.vhd", "common/cdc_value.vhd"),
               test_module="tests.tb.cdc_value_tb", generics={"WIDTH": 32}, build_dir=tmp_path)


def test_udiv_shared(tmp_path):
    run_cocotb(toplevel="udiv_shared", sources=rtl("common/udiv.vhd", "common/udiv_shared.vhd"),
               test_module="tests.tb.udiv_shared_tb", generics={"WIDTH": 32}, build_dir=tmp_path)
