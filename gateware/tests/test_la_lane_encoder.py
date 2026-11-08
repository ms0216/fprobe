"""深いバッファの N 並列の符号器（RAW の詰め・RLE・両方を包む符号器）と、端子から DDR のリングまでの端から端。
docs/superpowers/specs/2026-11-01-la-deep-fast-design.md"""

from fprobe_gw.rtl import CORE_GENERATED, RTL_ROOT, rtl
from fprobe_gw.toolchain.sim import run_cocotb

DEEP_LANES_SOURCES = [RTL_ROOT.parent / "tests" / "rtl" / "sim_xilinx_prims.vhd", CORE_GENERATED / "timing_pkg.vhd",
                      *rtl("core/generated/event_pkg.vhd", "core/generated/la_tok_pkg.vhd", "common/util_pkg.vhd", "boards/pynq_z2/pynq_z2_pkg.vhd", "boards/pynq_z2/xilinx_la_iserdes.vhd",
                           "common/sync_bit.vhd", "common/cdc_value.vhd", "common/sync_fifo.vhd", "common/async_fifo.vhd",
                           "common/stream_unpack.vhd", "common/stream_slice.vhd", "la/la_pkg.vhd", "la/la_front.vhd", "la/la_pack.vhd",
                           "la/la_encoder.vhd", "la/la_tok_encoder.vhd", "la/la_lane_raw.vhd", "la/la_lane_rle.vhd", "la/la_lane_encoder.vhd",
                           "mem/mem_ring_pkg.vhd", "mem/la_mem_writer.vhd", "mem/mem_reader.vhd", "la/la_stream_gap.vhd", "common/cdc_request.vhd", "common/hold_one.vhd", "la/la_run_cdc.vhd", "mem/la_write_track.vhd", "mem/mem_cmd_arb.vhd", "la/la_stream_sched.vhd", "la/la_deep_front.vhd", "la/la_deep_buffer.vhd"),
                      RTL_ROOT.parent / "tests" / "rtl" / "la_deep_lanes_harness.vhd"]


def test_la_lane_raw(tmp_path):
    run_cocotb(parallel=True, toplevel="la_lane_raw", sources=rtl("common/util_pkg.vhd", "la/la_lane_raw.vhd"),
               test_module="tests.tb.la_lane_raw_tb", build_dir=tmp_path)


def test_la_lane_rle(tmp_path):
    run_cocotb(parallel=True, toplevel="la_lane_rle", sources=rtl("la/la_pkg.vhd", "la/la_lane_rle.vhd"),
               test_module="tests.tb.la_lane_rle_tb", build_dir=tmp_path)


def test_la_lane_encoder(tmp_path):
    run_cocotb(parallel=True, toplevel="la_lane_encoder",
               sources=rtl("common/util_pkg.vhd", "la/la_pkg.vhd", "la/la_lane_raw.vhd", "la/la_lane_rle.vhd", "la/la_lane_encoder.vhd"),
               test_module="tests.tb.la_lane_encoder_tb", build_dir=tmp_path)


def test_la_deep_buffer_lanes_end_to_end(tmp_path):
    """端子 → ISERDESE2 の模型 → la_deep_buffer（N = 8）→ DDR のリング → PS の役 → ホストの解読。"""
    run_cocotb(parallel=True, toplevel="la_deep_lanes_harness", sources=DEEP_LANES_SOURCES,
               test_module="tests.tb.la_deep_lanes_tb", build_dir=tmp_path)
