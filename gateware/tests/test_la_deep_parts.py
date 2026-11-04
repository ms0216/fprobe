"""la_deep_buffer を組む小さな部品（cdc_request・hold_one・la_run_cdc・la_write_track・mem_cmd_arb・la_stream_sched）。"""

import pytest

from fprobe_gw.rtl import rtl
from fprobe_gw.toolchain.sim import run_cocotb

CDC = ("common/sync_bit.vhd", "common/cdc_value.vhd")


@pytest.mark.parametrize("same_clock", [False, True])
def test_cdc_request(tmp_path, monkeypatch, same_clock):
    monkeypatch.setenv("PART", "cdc_request")
    monkeypatch.setenv("CDC_SAME_CLOCK", "1" if same_clock else "0")
    run_cocotb(toplevel="cdc_request", sources=rtl(*CDC, "common/cdc_request.vhd"),
               generics={"WIDTH": 32, "SAME_CLOCK": same_clock}, test_module="tests.tb.la_deep_parts_tb", build_dir=tmp_path)


def test_hold_one(tmp_path, monkeypatch):
    monkeypatch.setenv("PART", "hold_one")
    run_cocotb(toplevel="hold_one", sources=rtl("common/util_pkg.vhd", "common/hold_one.vhd"),
               generics={"WIDTH": 16}, test_module="tests.tb.la_deep_parts_tb", build_dir=tmp_path)


@pytest.mark.parametrize("same_clock", [False, True])
def test_la_run_cdc(tmp_path, monkeypatch, same_clock):
    monkeypatch.setenv("PART", "la_run_cdc")
    monkeypatch.setenv("CDC_SAME_CLOCK", "1" if same_clock else "0")
    run_cocotb(toplevel="la_run_cdc", sources=rtl(*CDC, "la/la_run_cdc.vhd"),
               generics={"POST_BITS": 8, "SAME_CLOCK": same_clock}, test_module="tests.tb.la_deep_parts_tb",
               build_dir=tmp_path)


def test_la_write_track(tmp_path, monkeypatch):
    monkeypatch.setenv("PART", "la_write_track")
    run_cocotb(toplevel="la_write_track", sources=rtl("common/util_pkg.vhd", "mem/mem_ring_pkg.vhd", "mem/la_write_track.vhd"),
               generics={"ADDR_BITS": 10}, test_module="tests.tb.la_deep_parts_tb", build_dir=tmp_path)


def test_mem_cmd_arb(tmp_path, monkeypatch):
    monkeypatch.setenv("PART", "mem_cmd_arb")
    run_cocotb(toplevel="mem_cmd_arb", sources=rtl("mem/mem_cmd_arb.vhd"),
               generics={"ADDR_BITS": 10}, test_module="tests.tb.la_deep_parts_tb", build_dir=tmp_path)


def test_la_stream_sched(tmp_path):
    run_cocotb(parallel=True, toplevel="la_stream_sched",
               sources=rtl("common/util_pkg.vhd", "mem/mem_ring_pkg.vhd", "la/la_stream_sched.vhd"),
               generics={"ADDR_BITS": 10, "STREAM_EVENTS": True}, test_module="tests.tb.la_stream_sched_tb", build_dir=tmp_path)
