from fprobe_gw.rtl import rtl
from fprobe_gw.toolchain.sim import run_cocotb


def test_pin_select(tmp_path):
    run_cocotb(toplevel="pin_select", sources=rtl("core/pin_select.vhd"), test_module="tests.tb.pin_select_tb",
               build_dir=tmp_path)


def test_pin_select_random(tmp_path):
    run_cocotb(toplevel="pin_select", sources=rtl("core/pin_select.vhd"), test_module="tests.tb.pin_select_random_tb",
               generics={"ACTIONS": 3}, build_dir=tmp_path)
