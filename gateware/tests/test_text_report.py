from fprobe_gw.rtl import rtl
from fprobe_gw.toolchain.sim import run_cocotb
from tests.rtl import TEST_RTL


def test_text_report(tmp_path):
    run_cocotb(toplevel="text_report_harness",
               sources=[*rtl("common/text_report_pkg.vhd", "common/text_report.vhd"),
                        TEST_RTL / "text_report_harness.vhd"],
               test_module="tests.tb.text_report_tb", build_dir=tmp_path)
