from fprobe_gw.rtl import rtl
from fprobe_gw.toolchain.sim import run_cocotb


def test_axi_mem_port(tmp_path):
    run_cocotb(toplevel="axi_mem_port", sources=rtl("mem/axi_mem_port.vhd"),
               test_module="tests.tb.axi_mem_port_tb", generics={"ADDR_BITS": 21}, build_dir=tmp_path)
