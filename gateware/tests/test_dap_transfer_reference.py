"""DAP_Transfer と参照実装（DAP.c）の差分の試験（tests/tb/dap_transfer_reference_tb.py）。"""
from fprobe_gw.toolchain.sim import run_cocotb
from tests.test_dap_processor import SOURCES


def test_dap_transfer_matches_the_reference(tmp_path):
    run_cocotb(parallel=True, toplevel="dap_harness", sources=SOURCES, test_module="tests.tb.dap_transfer_reference_tb",
               build_dir=tmp_path)
