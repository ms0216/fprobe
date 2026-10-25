"""cocotb テストを複数プロセスに分けて走らせる仕組み（fprobe_gw.toolchain.sim）の、分け方の試験。"""

import pytest

from fprobe_gw.rtl import rtl
from fprobe_gw.toolchain.sim import SimulationFailed, cocotb_test_names, run_cocotb, sim_jobs


def test_test_names_are_read_in_file_order_and_match_the_decorator_count():
    names = cocotb_test_names("tests.tb.dap_sampler_tb")
    assert names is not None and len(names) == len(set(names))
    assert names[0] == "samples_slots_and_restores_host_state"
    assert names.index("nothing_is_read_without_a_connection") + 1 == names.index("jitter_varies_the_period_but_keeps_the_mean")
    source = open("tests/tb/dap_sampler_tb.py", encoding="utf-8").read()
    assert len(names) == source.count("@cocotb.test()")


def test_a_module_that_cannot_be_counted_exactly_is_not_split():
    assert cocotb_test_names("tests.tb_does_not_exist") is None


def test_the_job_count_follows_the_environment(monkeypatch):
    monkeypatch.setenv("FPROBE_SIM_JOBS", "3")
    assert sim_jobs() == 3
    monkeypatch.setenv("FPROBE_SIM_JOBS", "1")
    assert sim_jobs() == 1


def test_a_split_run_still_fails_when_one_test_fails(tmp_path, monkeypatch):
    # 2 件に分けた一方が失敗する条件（存在しない generic）でも、全体が失敗として返る
    monkeypatch.setenv("FPROBE_SIM_JOBS", "2")
    with pytest.raises(SimulationFailed):
        run_cocotb(toplevel="async_fifo", sources=rtl("common/util_pkg.vhd", "common/async_fifo.vhd"),
                   test_module="tests.tb.async_fifo_tb", generics={"WIDTH": 8, "DEPTH": 12},
                   build_dir=tmp_path, parallel=True)
