from fprobe_gw.rtl import rtl
from fprobe_gw.toolchain.sim import run_cocotb


def test_dut_axil_core(tmp_path):
    run_cocotb(toplevel="dut_axil_core",
               sources=rtl("common/util_pkg.vhd", "common/sync_fifo.vhd", "designs/dut_stream_core.vhd",
                           "designs/dut_pins.vhd", "designs/dut_axil_slave.vhd", "designs/dut_axil_core.vhd"),
               test_module="tests.tb.dut_axil_core_tb", build_dir=tmp_path)
