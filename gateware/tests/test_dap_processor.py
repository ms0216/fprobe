from fprobe_gw.rtl import rtl
from fprobe_gw.toolchain.sim import run_cocotb
from tests.rtl import TEST_RTL


SOURCES = [*rtl("common/util_pkg.vhd", "common/sync_bit.vhd", "serial/uart_rx.vhd", "debug/swo_manchester_rx.vhd", "debug/swo_receiver.vhd",
               "common/udiv.vhd", "common/udiv_shared.vhd", "debug/generated/dap_info_pkg.vhd", "debug/swd_phy.vhd", "debug/jtag_phy.vhd", "debug/debug_port.vhd",
               "debug/dap_pkg.vhd", "debug/dap_packet.vhd", "debug/dap_link.vhd", "debug/dap_cmd_config.vhd",
               "debug/dap_cmd_swo.vhd", "debug/dap_cmd_sequence.vhd", "debug/dap_cmd_transfer.vhd", "debug/dap_processor.vhd", "debug/swd_injector.vhd", "debug/dap_sampler.vhd", "debug/dap_monitor.vhd"),
           TEST_RTL / "dap_harness.vhd"]


def test_dap_processor(tmp_path):
    run_cocotb(toplevel="dap_harness", sources=SOURCES, test_module="tests.tb.dap_processor_tb", build_dir=tmp_path)


def test_swd_injector_and_monitor(tmp_path):
    run_cocotb(toplevel="dap_harness", sources=SOURCES, test_module="tests.tb.swd_injector_tb", build_dir=tmp_path)


def test_swo(tmp_path):
    run_cocotb(toplevel="dap_harness", sources=SOURCES, test_module="tests.tb.swo_tb", build_dir=tmp_path)


def test_swo_without_manchester(tmp_path):
    run_cocotb(toplevel="dap_harness", sources=SOURCES, test_module="tests.tb.swo_tb", build_dir=tmp_path,
               generics={"HAS_SWO_MANCHESTER": False})


def test_jtag(tmp_path):
    run_cocotb(toplevel="dap_harness", sources=SOURCES, test_module="tests.tb.dap_jtag_tb", build_dir=tmp_path)


def test_without_jtag(tmp_path):
    run_cocotb(toplevel="dap_harness", sources=SOURCES, test_module="tests.tb.dap_no_jtag_tb", build_dir=tmp_path,
               generics={"HAS_JTAG": False})


def test_dap_packet(tmp_path):
    run_cocotb(toplevel="dap_packet", sources=rtl("debug/dap_pkg.vhd", "debug/dap_packet.vhd"),
               test_module="tests.tb.dap_packet_tb", build_dir=tmp_path)


def test_dap_sampler(tmp_path):
    run_cocotb(toplevel="dap_harness", sources=SOURCES, test_module="tests.tb.dap_sampler_tb", build_dir=tmp_path)
