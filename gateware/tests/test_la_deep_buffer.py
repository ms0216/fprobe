from fprobe_gw.rtl import CORE_GENERATED, rtl
from fprobe_gw.toolchain.sim import run_cocotb

SOURCES = ("core/generated/event_pkg.vhd", "core/generated/la_tok_pkg.vhd", "common/util_pkg.vhd", "common/sync_bit.vhd", "common/cdc_value.vhd", "common/sync_fifo.vhd",
           "common/async_fifo.vhd", "common/stream_unpack.vhd", "common/stream_slice.vhd", "la/la_pkg.vhd", "la/la_front.vhd",
           "la/la_pack.vhd", "la/la_encoder.vhd", "la/la_tok_encoder.vhd", "la/la_lane_raw.vhd", "la/la_lane_rle.vhd",
           "la/la_lane_encoder.vhd", "mem/mem_ring_pkg.vhd", "mem/la_mem_writer.vhd", "mem/mem_reader.vhd", "la/la_stream_gap.vhd", "common/cdc_request.vhd", "common/hold_one.vhd", "la/la_run_cdc.vhd", "mem/la_write_track.vhd", "mem/mem_cmd_arb.vhd", "la/la_stream_sched.vhd", "la/la_deep_front.vhd", "la/la_deep_buffer.vhd")


def test_la_deep_buffer(tmp_path):
    run_cocotb(parallel=True, toplevel="la_deep_buffer", sources=[CORE_GENERATED / "timing_pkg.vhd", *rtl(*SOURCES)],
               generics={"ADDR_BITS": 10}, test_module="tests.tb.la_deep_buffer_tb", build_dir=tmp_path)


def test_la_deep_buffer_ps_read(tmp_path, monkeypatch):
    """PS が DDR のリングを直接読むモード（docs/superpowers/specs/2026-11-03-ps-stream-position-design.md）。"""
    monkeypatch.setenv("LDB_SAME_CLOCK", "0")
    run_cocotb(parallel=True, toplevel="la_deep_buffer", sources=[CORE_GENERATED / "timing_pkg.vhd", *rtl(*SOURCES)],
               generics={"ADDR_BITS": 10, "STREAM_EVENTS": True}, test_module="tests.tb.la_deep_buffer_ps_tb",
               build_dir=tmp_path)


def test_la_deep_buffer_ps_read_same_clock(tmp_path, monkeypatch):
    """同じ。mem_clk = clk（PYNQ）: 受け渡しに同期化を使わない構成（SAME_CLOCK。P7）。"""
    monkeypatch.setenv("LDB_SAME_CLOCK", "1")
    run_cocotb(parallel=True, toplevel="la_deep_buffer", sources=[CORE_GENERATED / "timing_pkg.vhd", *rtl(*SOURCES)],
               generics={"ADDR_BITS": 10, "STREAM_EVENTS": True, "SAME_CLOCK": True},
               test_module="tests.tb.la_deep_buffer_ps_tb", build_dir=tmp_path)


def test_la_deep_buffer_ps_read_totals_wrap(tmp_path, monkeypatch):
    """総数を 2^32 の 1500 拍手前から数え、折り返しをまたいで読む（C2）。"""
    monkeypatch.setenv("LDB_SAME_CLOCK", "0")
    monkeypatch.setenv("LDB_TOTAL_BELOW_4G", "1500")
    run_cocotb(toplevel="la_deep_buffer", sources=[CORE_GENERATED / "timing_pkg.vhd", *rtl(*SOURCES)],
               generics={"ADDR_BITS": 10, "STREAM_EVENTS": True, "TOTAL_START_BELOW_4G": 1500},
               test_module="tests.tb.la_deep_buffer_ps_tb", test_filter="totals_wrap", build_dir=tmp_path)


def test_la_deep_buffer_stream_events(tmp_path):
    """ストリームにイベントも流す構成（R1。docs/superpowers/specs/2026-10-19-r1-unified-stream-design.md）。"""
    run_cocotb(parallel=True, toplevel="la_deep_buffer", sources=[CORE_GENERATED / "timing_pkg.vhd", *rtl(*SOURCES)],
               generics={"ADDR_BITS": 10, "STREAM_EVENTS": True}, test_module="tests.tb.la_deep_buffer_mix_tb",
               build_dir=tmp_path)


GAP_GENERICS = {"ADDR_BITS": 12, "STREAM_EVENTS": True, "STREAM_GAPS": True}


def test_la_deep_buffer_stream_gaps(tmp_path):
    """欠けの区間を記録して続ける（段 B。docs/superpowers/specs/2026-11-01-la-deep-fast-design.md §12）: 1 周期 1 サンプル。"""
    run_cocotb(parallel=True, toplevel="la_deep_buffer", sources=[CORE_GENERATED / "timing_pkg.vhd", *rtl(*SOURCES)], generics=GAP_GENERICS,
               test_module="tests.tb.la_deep_buffer_gap_tb", build_dir=tmp_path)


def test_la_deep_buffer_stream_gaps_lanes(tmp_path):
    """同じ、1 周期に 8 サンプル（la_lane_encoder）。"""
    run_cocotb(parallel=True, toplevel="la_deep_buffer", sources=[CORE_GENERATED / "timing_pkg.vhd", *rtl(*SOURCES)],
               generics={**GAP_GENERICS, "LANES": 8, "IN_AGE": 5},
               test_module="tests.tb.la_deep_buffer_gap_tb", build_dir=tmp_path)


def test_la_deep_buffer_rle_tokens(tmp_path):
    """RLE のトークン形式（docs/superpowers/specs/2026-11-07-la-rle2-design.md）: 端子から PS の読みまで、欠け・tick を含めて。"""
    run_cocotb(parallel=True, toplevel="la_deep_buffer", sources=[CORE_GENERATED / "timing_pkg.vhd", *rtl(*SOURCES)],
               generics={**GAP_GENERICS, "RLE_TOKENS": True}, test_module="tests.tb.la_deep_buffer_tok_tb", build_dir=tmp_path)


def test_la_deep_buffer_rle_tokens_repeat(tmp_path, monkeypatch):
    """同じ、トークン形式の REPEAT を使う（TOK_REPEAT_EN = true・rle_repeat = 1。設計書 §3.7）: 拍の位置（欠けの長さ q）も REPEAT の展開後の時刻で合う。"""
    monkeypatch.setenv("LDB_REPEAT", "1")
    run_cocotb(parallel=True, toplevel="la_deep_buffer", sources=[CORE_GENERATED / "timing_pkg.vhd", *rtl(*SOURCES)],
               generics={**GAP_GENERICS, "RLE_TOKENS": True, "TOK_REPEAT_EN": True}, test_module="tests.tb.la_deep_buffer_tok_tb", build_dir=tmp_path)
