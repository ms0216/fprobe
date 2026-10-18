from fprobe_gw.rtl import rtl
from fprobe_gw.toolchain.sim import run_cocotb


def test_frame_mux(tmp_path):
    run_cocotb(toplevel="frame_mux",
               sources=rtl("common/util_pkg.vhd", "common/sync_fifo.vhd", "common/sdp_ram.vhd", "common/stream_slice.vhd", "hostlink/frame_mux.vhd"),
               test_module="tests.tb.frame_mux_tb", build_dir=tmp_path)
