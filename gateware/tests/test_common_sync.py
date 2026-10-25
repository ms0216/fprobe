from fprobe_gw.rtl import rtl
from fprobe_gw.toolchain.sim import run_cocotb


def test_sync_bit(tmp_path):
    run_cocotb(toplevel="sync_bit", sources=rtl("common/sync_bit.vhd"),
               test_module="tests.tb.sync_bit_tb", generics={"INIT": "'1'"}, build_dir=tmp_path)


def test_reset_sync(tmp_path):
    run_cocotb(toplevel="reset_sync", sources=rtl("common/reset_sync.vhd"),
               test_module="tests.tb.reset_sync_tb", generics={"STAGES": 3}, build_dir=tmp_path)


def test_reset_fanout(tmp_path):
    run_cocotb(toplevel="reset_fanout", sources=rtl("common/reset_fanout.vhd"),
               test_module="tests.tb.reset_fanout_tb", generics={"COUNT": 5}, build_dir=tmp_path)
