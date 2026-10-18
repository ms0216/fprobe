from fprobe_gw.rtl import rtl
from fprobe_gw.toolchain.sim import run_cocotb


def test_mem_test(tmp_path):
    run_cocotb(toplevel="mem_test", sources=rtl("mem/mem_test.vhd"), test_module="tests.tb.mem_test_tb",
               build_dir=tmp_path)


def test_mem_test_sys(tmp_path):
    run_cocotb(toplevel="mem_test_sys",
               sources=rtl("common/util_pkg.vhd", "common/sync_bit.vhd", "common/cdc_value.vhd", "mem/mem_test.vhd",
                           "mem/mem_test_sys.vhd"),
               test_module="tests.tb.mem_test_sys_tb", build_dir=tmp_path)
