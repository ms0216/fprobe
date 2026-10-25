from fprobe_gw.rtl import rtl
from fprobe_gw.toolchain.sim import run_cocotb
from tests.rtl import TEST_RTL


def test_frame_link_harness(tmp_path):
    run_cocotb(toplevel="frame_link_harness",
               sources=[*rtl("common/util_pkg.vhd", "common/sync_fifo.vhd", "common/sdp_ram.vhd",
                             "common/stream_slice.vhd", "hostlink/frame_mux.vhd", "hostlink/zynq_frame_link.vhd"),
                        TEST_RTL / "frame_link_harness.vhd"],
               test_module="tests.tb.frame_link_harness_tb", build_dir=tmp_path)
