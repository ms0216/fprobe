from fprobe_gw.rtl import rtl
from fprobe_gw.toolchain.sim import run_cocotb
from tests.rtl import TEST_RTL


def test_mem_ring(tmp_path):
    run_cocotb(toplevel="mem_ring_harness",
               sources=[*rtl("common/util_pkg.vhd", "common/sync_fifo.vhd", "mem/la_mem_writer.vhd",
                             "mem/mem_reader.vhd"), TEST_RTL / "mem_ring_harness.vhd"],
               test_module="tests.tb.mem_ring_tb", build_dir=tmp_path)
