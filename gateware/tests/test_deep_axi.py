from fprobe_gw.rtl import CORE_GENERATED, rtl
from fprobe_gw.toolchain.sim import run_cocotb
from tests.rtl import TEST_RTL
from tests.test_la_deep_buffer import SOURCES


def test_deep_buffer_through_axi(tmp_path):
    """PYNQ の P3 のつなぎ（la_deep_buffer → axi_mem_port → AXI3）。"""
    run_cocotb(parallel=True, toplevel="deep_axi_harness", sources=[CORE_GENERATED / "timing_pkg.vhd", *rtl(*SOURCES, "mem/axi_mem_port.vhd"),
                                                    TEST_RTL / "deep_axi_harness.vhd"],
               generics={"ADDR_BITS": 10}, test_module="tests.tb.deep_axi_tb", build_dir=tmp_path)
