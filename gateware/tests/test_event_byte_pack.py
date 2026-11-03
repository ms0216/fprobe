from fprobe_gw.rtl import rtl
from fprobe_gw.toolchain.sim import run_cocotb


def test_event_byte_pack(tmp_path):
    run_cocotb(toplevel="event_byte_pack", sources=rtl("core/event_byte_pack.vhd"),
               test_module="tests.tb.event_byte_pack_tb", build_dir=tmp_path)
