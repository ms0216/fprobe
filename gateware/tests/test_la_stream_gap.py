"""ロジアナの垂れ流しの欠けの区間（la_stream_gap。docs/superpowers/specs/2026-11-01-la-deep-fast-design.md §12 段 B）。"""

from fprobe_gw.rtl import rtl
from fprobe_gw.toolchain.sim import run_cocotb


def test_la_stream_gap(tmp_path):
    run_cocotb(parallel=True, toplevel="la_stream_gap", sources=rtl("la/la_stream_gap.vhd"),
               generics={"SPB_LOG": 3, "Q_BITS": 10}, test_module="tests.tb.la_stream_gap_tb", build_dir=tmp_path)
