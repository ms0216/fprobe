"""2026-11-03 のレビュー（DAP の線の速さ）: SWD の TransferBlock を遅れ・idle_cycles・失敗ありで回す。idle_cycles > 0 で読みの
最後の取り込みの前に idle 相を駆動した不具合の試験を含む（直す前は落ちた。docs/reports/2026-11-03-cloud-A.md）。"""
from fprobe_gw.toolchain.sim import run_cocotb
from tests.test_dap_processor import SOURCES
from tests.rtl import TEST_RTL


def test_review_dapline_swd_block(tmp_path):
    run_cocotb(toplevel="dap_harness", sources=SOURCES, test_module="tests.tb.review_dapline_swd_block_tb",
               build_dir=tmp_path)
