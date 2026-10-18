from fprobe_gw.rtl import rtl
from fprobe_gw.toolchain.sim import run_cocotb


def test_swo_receiver(tmp_path):
    run_cocotb(toplevel="swo_receiver",
               sources=rtl("common/sync_bit.vhd", "serial/uart_rx.vhd", "debug/swo_manchester_rx.vhd",
                           "debug/swo_receiver.vhd"),
               test_module="tests.tb.swo_receiver_tb", generics={"FLUSH_CYCLES": 32}, build_dir=tmp_path)
