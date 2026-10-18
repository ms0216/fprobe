from fprobe_gw.rtl import rtl
from fprobe_gw.toolchain.sim import run_cocotb
from tests.rtl import TEST_RTL

SOURCES = rtl("common/util_pkg.vhd", "usb/usb_types_pkg.vhd", "usb/usb_crc_pkg.vhd",
              "usb/ulpi_link.vhd", "usb/usb_rx_packet.vhd", "usb/usb_tx_packet.vhd",
              "usb/usb_ep_out.vhd", "usb/usb_ep_in.vhd", "usb/usb_protocol.vhd")


def test_usb_protocol(tmp_path):
    run_cocotb(toplevel="usb_protocol_harness",
               sources=[*SOURCES, TEST_RTL / "usb_protocol_harness.vhd"],
               test_module="tests.tb.usb_protocol_tb", build_dir=tmp_path)
