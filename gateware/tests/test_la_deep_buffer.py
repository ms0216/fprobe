from fprobe_gw.rtl import rtl
from fprobe_gw.toolchain.sim import run_cocotb

SOURCES = ("common/util_pkg.vhd", "common/sync_bit.vhd", "common/cdc_value.vhd", "common/sync_fifo.vhd",
           "common/async_fifo.vhd", "common/stream_unpack.vhd", "common/stream_slice.vhd", "la/la_front.vhd",
           "la/la_encoder.vhd", "mem/la_mem_writer.vhd", "mem/mem_reader.vhd", "la/la_deep_buffer.vhd")


def test_la_deep_buffer(tmp_path):
    run_cocotb(parallel=True, toplevel="la_deep_buffer", sources=rtl(*SOURCES),
               generics={"ADDR_BITS": 10}, test_module="tests.tb.la_deep_buffer_tb", build_dir=tmp_path)


def test_la_deep_buffer_stream_events(tmp_path):
    """ストリームにイベントも流す構成（R1。docs/superpowers/specs/2026-10-19-r1-unified-stream-design.md）。"""
    run_cocotb(parallel=True, toplevel="la_deep_buffer", sources=rtl(*SOURCES),
               generics={"ADDR_BITS": 10, "STREAM_EVENTS": True}, test_module="tests.tb.la_deep_buffer_mix_tb",
               build_dir=tmp_path)
