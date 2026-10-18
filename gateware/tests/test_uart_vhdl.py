from fprobe_gw.rtl import rtl
from fprobe_gw.toolchain.sim import run_cocotb
from tests.rtl import TEST_RTL

UART = rtl("common/sync_bit.vhd", "serial/uart_tx.vhd", "serial/uart_rx.vhd")


def test_uart_tx(tmp_path):
    run_cocotb(toplevel="uart_tx", sources=UART, test_module="tests.tb.uart_tx_tb",
               build_dir=tmp_path)


def test_uart_rx(tmp_path):
    run_cocotb(toplevel="uart_rx", sources=UART, test_module="tests.tb.uart_rx_tb",
               build_dir=tmp_path)


def test_uart_loopback(tmp_path):
    run_cocotb(toplevel="uart_loopback", sources=[*UART, TEST_RTL / "uart_loopback.vhd"],
               test_module="tests.tb.uart_loopback_tb", build_dir=tmp_path)
