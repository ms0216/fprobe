from fprobe_gw.rtl import rtl
from fprobe_gw.toolchain.sim import run_cocotb


def test_sync_link(tmp_path):
    run_cocotb(toplevel="sync_link", sources=rtl("common/sync_bit.vhd", "core/sync_link.vhd"),
               test_module="tests.tb.sync_link_tb", build_dir=tmp_path)
