from fprobe_gw.rtl import rtl
from fprobe_gw.toolchain.sim import run_cocotb


def test_sync_fifo(tmp_path):
    run_cocotb(toplevel="sync_fifo", sources=rtl("common/util_pkg.vhd", "common/sync_fifo.vhd"),
               test_module="tests.tb.sync_fifo_tb", generics={"WIDTH": 8, "DEPTH": 16},
               build_dir=tmp_path)
