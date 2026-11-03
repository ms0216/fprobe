"""書いたイベントの数の 64 ビット化（event_ring・la_mem_writer）。2**32 の手前から数える generic で境をまたぐ。"""
from fprobe_gw.rtl import rtl
from fprobe_gw.toolchain.sim import run_cocotb
from tests.rtl import TEST_RTL


def test_event_ring_count_past_4g(tmp_path):
    run_cocotb(toplevel="event_ring", sources=rtl("common/util_pkg.vhd", "common/sdp_ram.vhd", "la/event_ring.vhd"),
               test_module="tests.tb.event_count_64_tb", test_filter="event_ring_counts_past_2_pow_32",
               generics={"DEPTH_BITS": 4, "ADDR_BITS": 8, "COUNT_START_BELOW_4G": 16}, build_dir=tmp_path)


def test_mem_writer_count_past_4g(tmp_path):
    run_cocotb(toplevel="mem_ring_harness",
               sources=[*rtl("common/util_pkg.vhd", "common/sync_fifo.vhd", "mem/la_mem_writer.vhd",
                             "mem/mem_reader.vhd"), TEST_RTL / "mem_ring_harness.vhd"],
               test_module="tests.tb.event_count_64_tb", test_filter="mem_writer_counts_past_2_pow_32_instead_of_saturating",
               generics={"COUNT_START_BELOW_4G": 64}, build_dir=tmp_path)
