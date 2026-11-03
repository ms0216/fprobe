"""2026-11-03 のレビュー（DAP の線の速さ）: JTAG の TransferBlock を 50 MHz・遅れ・WAIT ありで回す（docs/reports/2026-11-03-cloud-A.md）。"""
from fprobe_gw.toolchain.sim import run_cocotb
from tests.rtl import TEST_RTL
from tests.test_dap_processor import SOURCES


def test_review_dapline_jtag_block(tmp_path):
    run_cocotb(toplevel="dap_harness", sources=SOURCES, test_module="tests.tb.review_dapline_jtag_block_tb",
               build_dir=tmp_path)
