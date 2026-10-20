from fprobe_gw.rtl import rtl
from fprobe_gw.toolchain.sim import run_cocotb


def test_event_ring(tmp_path):
    run_cocotb(toplevel="event_ring", sources=rtl("common/util_pkg.vhd", "common/sdp_ram.vhd", "la/event_ring.vhd"),
               test_module="tests.tb.event_ring_tb", generics={"DEPTH_BITS": 4, "ADDR_BITS": 8}, build_dir=tmp_path)
