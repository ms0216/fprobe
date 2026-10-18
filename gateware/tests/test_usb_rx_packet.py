from fprobe_gw.rtl import rtl
from fprobe_gw.toolchain.sim import run_cocotb


def test_usb_rx_packet(tmp_path):
    run_cocotb(toplevel="usb_rx_packet", sources=rtl("usb/usb_types_pkg.vhd", "usb/usb_crc_pkg.vhd",
                                                     "usb/usb_rx_packet.vhd"),
               test_module="tests.tb.usb_rx_packet_tb", build_dir=tmp_path)
