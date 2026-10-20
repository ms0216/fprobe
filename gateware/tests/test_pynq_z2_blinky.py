from fprobe_gw.rtl import rtl
from fprobe_gw.toolchain.sim import run_cocotb


def test_pynq_z2_blinky(tmp_path):
    run_cocotb(toplevel="pynq_z2_blinky_top", sources=rtl("boards/pynq_z2/pynq_z2_blinky_top.vhd"),
               test_module="tests.tb.pynq_z2_blinky_tb", generics={"DIV": 100}, build_dir=tmp_path)
