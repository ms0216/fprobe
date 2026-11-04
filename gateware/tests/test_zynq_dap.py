from fprobe_gw.rtl import CORE_GENERATED, rtl
from fprobe_gw.toolchain.sim import run_cocotb
from tests.rtl import TEST_RTL
from tests.test_dap_processor import SOURCES as DAP_SOURCES


def unique(paths):
    """同じソースを 1 度だけ（共通の部品は DAP_SOURCES と重なる）。"""
    return list(dict.fromkeys(paths))


def test_zynq_dap(tmp_path):
    run_cocotb(toplevel="zynq_dap_harness",
               sources=unique([CORE_GENERATED / "ps_window_pkg.vhd", *rtl("common/util_pkg.vhd", "common/sync_bit.vhd", "common/sync_fifo.vhd", "common/sdp_ram.vhd",
                             "common/stream_slice.vhd", "hostlink/frame_mux.vhd", "hostlink/zynq_frame_link.vhd"),
                        *DAP_SOURCES, TEST_RTL / "zynq_dap_harness.vhd"]),
               test_module="tests.tb.zynq_dap_tb", build_dir=tmp_path)
