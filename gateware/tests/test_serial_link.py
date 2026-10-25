from fprobe_gw.rtl import rtl
from fprobe_gw.toolchain.sim import run_cocotb


def test_serial_link(tmp_path):
    run_cocotb(parallel=True, toplevel="serial_link",
               sources=rtl("common/util_pkg.vhd", "common/sync_bit.vhd", "common/sync_fifo.vhd", "common/sdp_ram.vhd", "serial/uart_rx.vhd",
                           "serial/uart_tx.vhd", "common/stream_slice.vhd", "hostlink/frame_mux.vhd", "hostlink/serial_link.vhd"),
               generics={"CLK_HZ": 8_000_000, "BAUD": 1_000_000}, test_module="tests.tb.serial_link_tb",
               build_dir=tmp_path)
