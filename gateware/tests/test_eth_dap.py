from fprobe_gw.rtl import rtl
from fprobe_gw.toolchain.sim import run_cocotb
from tests.rtl import TEST_RTL
from tests.test_dap_processor import SOURCES as DAP_SOURCES


def unique(paths):
    """同じソースを 1 度だけ（共通の部品は DAP_SOURCES と重なる）。"""
    return list(dict.fromkeys(paths))


def test_eth_dap(tmp_path):
    run_cocotb(parallel=True, toplevel="eth_dap_harness",
               sources=unique([*rtl("common/util_pkg.vhd", "common/sync_bit.vhd", "common/sync_fifo.vhd", "common/sdp_ram.vhd",
                             "common/reset_sync.vhd", "common/async_fifo.vhd", "eth/eth_pkg.vhd", "eth/rmii_rx.vhd", "eth/rmii_tx.vhd",
                             "eth/eth_rx_header.vhd", "eth/arp_rx.vhd", "eth/ipv4_rx.vhd", "eth/icmp_rx.vhd",
                             "eth/udp_rx.vhd", "eth/udp_stack.vhd", "common/stream_slice.vhd", "hostlink/frame_mux.vhd", "hostlink/eth_link.vhd"),
                        *DAP_SOURCES, TEST_RTL / "eth_dap_harness.vhd"]),
               test_module="tests.tb.eth_dap_tb", build_dir=tmp_path)
