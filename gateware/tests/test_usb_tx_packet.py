from fprobe_gw.rtl import rtl
from fprobe_gw.toolchain.sim import run_cocotb


def test_usb_tx_packet(tmp_path):
    run_cocotb(toplevel="usb_tx_packet", sources=rtl("usb/usb_crc_pkg.vhd", "usb/usb_tx_packet.vhd"),
               test_module="tests.tb.usb_tx_packet_tb", build_dir=tmp_path)
