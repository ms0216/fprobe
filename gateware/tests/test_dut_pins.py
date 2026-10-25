from fprobe_gw.rtl import rtl
from fprobe_gw.toolchain.sim import run_cocotb


def test_dut_pins(tmp_path):
    run_cocotb(toplevel="dut_pins", sources=rtl("common/util_pkg.vhd", "designs/dut_pins.vhd"),
               test_module="tests.tb.dut_pins_tb", generics={"N": 4}, build_dir=tmp_path)
