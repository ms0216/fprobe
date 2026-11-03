"""2026-11-03 のレビュー（PS 直読みの流れの位置）の B1: PS 直読みの流れを止めた後にイベントが来ても、流れは止めの命令が効いた
時点で終わり、それより後のイベントはイベントのリングに残る（docs/superpowers/specs/2026-11-03-ps-stream-position-design.md、
docs/reports/2026-11-03-cloud-A.md）。前は reading・ACTIVE が下がらなかった。"""
from fprobe_gw.rtl import CORE_GENERATED, rtl
from fprobe_gw.toolchain.sim import run_cocotb

from tests.test_la_deep_buffer import SOURCES


def _run(tmp_path, monkeypatch, name):
    monkeypatch.setenv("LDB_SAME_CLOCK", "1")
    run_cocotb(toplevel="la_deep_buffer", sources=[CORE_GENERATED / "timing_pkg.vhd", *rtl(*SOURCES)],
               generics={"ADDR_BITS": 10, "STREAM_EVENTS": True, "SAME_CLOCK": True},
               test_module="tests.tb.review_psstream_evstop_tb", build_dir=tmp_path, test_filter=name)


def test_reading_falls_when_events_stop_with_the_run(tmp_path, monkeypatch):
    _run(tmp_path, monkeypatch, "reading_falls_when_events_stop_with_the_run")


def test_reading_falls_even_if_events_keep_coming_after_the_run_is_stopped(tmp_path, monkeypatch):
    _run(tmp_path, monkeypatch, "reading_falls_even_if_events_keep_coming_after_the_run_is_stopped")
