"""受信の各層（eth_rx_header・arp_rx・ipv4_rx・icmp_rx・udp_rx。udp_stack の受信の部品）の試験。"""

from fprobe_gw.rtl import rtl
from fprobe_gw.toolchain.sim import run_cocotb
from tests.tb.eth_rx_frames import IP_GENERIC, MAC_GENERIC, PORT

SOURCES = rtl("common/util_pkg.vhd", "eth/eth_pkg.vhd", "eth/eth_rx_header.vhd", "eth/arp_rx.vhd", "eth/ipv4_rx.vhd",
              "eth/icmp_rx.vhd", "eth/udp_rx.vhd")


def test_eth_rx_header(tmp_path):
    run_cocotb(toplevel="eth_rx_header", sources=SOURCES, generics={"MAC": MAC_GENERIC},
               test_module="tests.tb.eth_rx_header_tb", build_dir=tmp_path)


def test_arp_rx(tmp_path):
    run_cocotb(toplevel="arp_rx", sources=SOURCES, generics={"IP": IP_GENERIC},
               test_module="tests.tb.arp_rx_tb", build_dir=tmp_path)


def test_ipv4_rx(tmp_path):
    run_cocotb(toplevel="ipv4_rx", sources=SOURCES, generics={"IP": IP_GENERIC},
               test_module="tests.tb.ipv4_rx_tb", build_dir=tmp_path)


def test_icmp_rx(tmp_path):
    run_cocotb(toplevel="icmp_rx", sources=SOURCES, test_module="tests.tb.icmp_rx_tb", build_dir=tmp_path)


def test_udp_rx(tmp_path):
    run_cocotb(toplevel="udp_rx", sources=SOURCES, generics={"PORT_NUM": PORT, "MAX_PAYLOAD": 16, "RAM_BITS": 5},
               test_module="tests.tb.udp_rx_tb", build_dir=tmp_path)
