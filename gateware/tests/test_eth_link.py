from fprobe_gw.rtl import rtl
from fprobe_gw.toolchain.sim import run_cocotb


def test_eth_link(tmp_path):
    run_cocotb(toplevel="eth_link",
               sources=rtl("common/util_pkg.vhd", "common/sync_bit.vhd", "common/sync_fifo.vhd", "common/sdp_ram.vhd",
                           "common/reset_sync.vhd", "common/async_fifo.vhd", "eth/rmii_rx.vhd", "eth/rmii_tx.vhd",
                           "eth/udp_stack.vhd", "common/stream_slice.vhd", "hostlink/frame_mux.vhd", "hostlink/eth_link.vhd"),
               generics={"MAC": f"{0x026670720001:048b}", "IP": f"{0xC0A84D02:032b}"}, test_module="tests.tb.eth_link_tb",
               build_dir=tmp_path)
