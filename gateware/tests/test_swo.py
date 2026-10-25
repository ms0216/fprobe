"""SWO の受信器（Manchester 方式・NRZ との切り替え）。CMSIS-DAP のコマンド越しの試験は test_dap_processor の test_swo。"""

from fprobe_gw.rtl import rtl
from fprobe_gw.toolchain.sim import run_cocotb

MANCHESTER = rtl("common/sync_bit.vhd", "debug/swo_manchester_rx.vhd")
RECEIVER = rtl("common/sync_bit.vhd", "serial/uart_rx.vhd", "debug/swo_manchester_rx.vhd", "debug/swo_receiver.vhd")


def test_swo_manchester_rx(tmp_path):
    run_cocotb(toplevel="swo_manchester_rx", sources=MANCHESTER, test_module="tests.tb.swo_manchester_rx_tb",
               build_dir=tmp_path)


def test_swo_receiver(tmp_path):
    run_cocotb(toplevel="swo_receiver", sources=RECEIVER, test_module="tests.tb.swo_receiver_modes_tb",
               build_dir=tmp_path)


def test_swo_receiver_without_manchester(tmp_path):
    run_cocotb(toplevel="swo_receiver", sources=RECEIVER, test_module="tests.tb.swo_receiver_modes_tb",
               build_dir=tmp_path, generics={"HAS_MANCHESTER": False})


def test_swo_receiver_fuzz(tmp_path):
    run_cocotb(toplevel="swo_receiver", sources=RECEIVER, test_module="tests.tb.swo_fuzz_tb", build_dir=tmp_path)


def test_swo_receiver_fuzz_without_manchester(tmp_path):
    run_cocotb(toplevel="swo_receiver", sources=RECEIVER, test_module="tests.tb.swo_fuzz_tb", build_dir=tmp_path,
               generics={"HAS_MANCHESTER": False})


def test_dap_cmd_swo(tmp_path):
    run_cocotb(toplevel="dap_cmd_swo", sources=rtl("debug/dap_pkg.vhd", "debug/dap_cmd_swo.vhd"),
               test_module="tests.tb.dap_cmd_swo_tb", generics={"CLK_HZ": 100_000_000}, build_dir=tmp_path)
