from fprobe_gw.rtl import rtl
from fprobe_gw.toolchain.sim import run_cocotb


def test_aux_link(tmp_path):
    run_cocotb(toplevel="aux_link",
               sources=rtl("common/util_pkg.vhd", "common/sync_bit.vhd", "serial/uart_rx.vhd", "core/aux_link.vhd"),
               generics={"CLK_HZ": 8_000_000, "BAUD": 1_000_000}, test_module="tests.tb.aux_link_tb",
               build_dir=tmp_path)
