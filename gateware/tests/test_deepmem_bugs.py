"""外部メモリ経路（深いバッファ・AXI・リング）の不具合の再現（検出専用。strict xfail）。
台帳: docs/reports/bugs/rtl-deep-mem.md。直すと XPASS で失敗するので、直したら xfail を外す。"""

import pytest

from fprobe_gw.rtl import rtl
from fprobe_gw.toolchain.sim import run_cocotb
from tests.rtl import TEST_RTL

RING_SOURCES = ("common/util_pkg.vhd", "common/sync_fifo.vhd", "mem/la_mem_writer.vhd", "mem/mem_reader.vhd")


def _ring(tmp_path, name):
    run_cocotb(toplevel="mem_ring_harness", sources=[*rtl(*RING_SOURCES), TEST_RTL / "mem_ring_harness.vhd"],
               test_module="tests.tb.deepmem_ring_bugs_tb", test_filter=name, build_dir=tmp_path)


def _axi(tmp_path, name):
    run_cocotb(toplevel="axi_mem_port", sources=rtl("mem/axi_mem_port.vhd"),
               test_module="tests.tb.deepmem_axi_bugs_tb", generics={"ADDR_BITS": 21}, test_filter=name,
               build_dir=tmp_path)


def test_mem_1_reader_restart(tmp_path):
    _ring(tmp_path, "bug_mem_1_reader_restart_takes_stale_beats")


def test_mem_2_first_beyond_region(tmp_path):
    _ring(tmp_path, "bug_mem_2_reader_first_beyond_region_leaves_region")


@pytest.mark.xfail(strict=True, reason="BUG-RTL-AXI-1: 途中の rst で未完了の AXI 取引と数が壊れる")
def test_axi_1_reset_outstanding(tmp_path):
    _axi(tmp_path, "bug_axi_1_reset_with_outstanding_transactions")


def test_axi_2_unaligned_base(tmp_path):
    _axi(tmp_path, "bug_axi_2_unaligned_base_crosses_4kb")
