from fprobe_gw.rtl import rtl
from fprobe_gw.toolchain.sim import run_cocotb
from tests.rtl import TEST_RTL


def test_csr_decoder(tmp_path):
    run_cocotb(toplevel="csr_decoder_harness",
               sources=[*rtl("core/csr_bus_pkg.vhd", "core/csr_decoder.vhd"),
                        TEST_RTL / "csr_decoder_harness.vhd"],
               test_module="tests.tb.csr_decoder_tb", build_dir=tmp_path)
