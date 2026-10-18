from fprobe_gw.rtl import rtl
from fprobe_gw.toolchain.sim import run_cocotb

SOURCES = rtl("common/sync_bit.vhd", "common/text_report_pkg.vhd", "common/text_report.vhd",
              "serial/uart_tx.vhd", "serial/uart_rx.vhd",
              "usb/ulpi_link.vhd", "designs/ulpi_probe_core.vhd")


def test_ulpi_probe_core(tmp_path):
    run_cocotb(toplevel="ulpi_probe_core", sources=SOURCES, test_module="tests.tb.ulpi_probe_tb",
               generics={"CLK_HZ": 60_000_000, "BAUD": 6_000_000, "STARTUP_STP_CYCLES": 8},
               build_dir=tmp_path)
