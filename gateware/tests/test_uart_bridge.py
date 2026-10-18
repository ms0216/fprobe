from fprobe_gw.rtl import rtl
from fprobe_gw.toolchain.sim import run_cocotb


def test_uart_bridge(tmp_path):
    run_cocotb(toplevel="uart_bridge",
               sources=rtl("common/util_pkg.vhd", "common/sync_bit.vhd", "common/udiv.vhd", "common/udiv_shared.vhd",
                           "serial/uart_tx.vhd", "serial/uart_rx.vhd", "serial/uart_bridge.vhd"),
               test_module="tests.tb.uart_bridge_tb",
               generics={"CLK_HZ": 10_000_000, "DEFAULT_BAUD": 1_000_000}, build_dir=tmp_path)


def test_uart_bridge_with_flow_control(tmp_path):
    run_cocotb(toplevel="uart_bridge",
               sources=rtl("common/util_pkg.vhd", "common/sync_bit.vhd", "common/udiv.vhd", "common/udiv_shared.vhd",
                           "serial/uart_tx.vhd", "serial/uart_rx.vhd", "serial/uart_bridge.vhd"),
               test_module="tests.tb.uart_bridge_tb",
               generics={"CLK_HZ": 10_000_000, "DEFAULT_BAUD": 1_000_000, "FLOW": True}, build_dir=tmp_path)
