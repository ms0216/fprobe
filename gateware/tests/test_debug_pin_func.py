from fprobe_gw.rtl import rtl
from fprobe_gw.toolchain.sim import run_cocotb
from tests.rtl import TEST_RTL


def test_debug_pin_func(tmp_path):
    run_cocotb(toplevel="debug_pin_func_harness",
               sources=[*rtl("core/debug_pin_func.vhd"), TEST_RTL / "debug_pin_func_harness.vhd"],
               test_module="tests.tb.debug_pin_func_tb", build_dir=tmp_path)
