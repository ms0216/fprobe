"""2026-11-03 のレビュー（停止の見張り）: 始動の列の written_words・start_pending の不具合の試験（直す前は落ちた。docs/reports/2026-11-03-cloud-A.md）。"""
from fprobe_gw.toolchain.sim import run_cocotb
from tests.rtl import TEST_RTL
from tests.test_dap_processor import SOURCES


def test_review_haltwatch_injector(tmp_path):
    run_cocotb(toplevel="dap_harness", sources=SOURCES, test_module="tests.tb.review_haltwatch_injector_tb",
               build_dir=tmp_path)
