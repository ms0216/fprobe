from fprobe_gw.rtl import rtl
from fprobe_gw.toolchain.sim import run_cocotb


def test_async_fifo(tmp_path):
    run_cocotb(toplevel="async_fifo",
               sources=rtl("common/util_pkg.vhd", "common/async_fifo.vhd"),
               test_module="tests.tb.async_fifo_tb", generics={"WIDTH": 8, "DEPTH": 16},
               build_dir=tmp_path)
