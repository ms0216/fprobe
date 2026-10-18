from fprobe_gw.rtl import rtl
from fprobe_gw.toolchain.sim import run_cocotb
from tests.rtl import TEST_RTL
from tests.test_dap_processor import SOURCES as DAP_SOURCES


def test_eth_dap(tmp_path):
    run_cocotb(toplevel="eth_dap_harness",
               sources=[*rtl("common/util_pkg.vhd", "common/sync_bit.vhd", "common/sync_fifo.vhd", "common/sdp_ram.vhd",
                             "common/reset_sync.vhd", "common/async_fifo.vhd", "eth/rmii_rx.vhd", "eth/rmii_tx.vhd",
                             "eth/udp_stack.vhd", "common/stream_slice.vhd", "hostlink/frame_mux.vhd", "hostlink/eth_link.vhd"),
                        *DAP_SOURCES[2:], TEST_RTL / "eth_dap_harness.vhd"],
               test_module="tests.tb.eth_dap_tb", build_dir=tmp_path)
