import pytest

from fprobe_gw.toolchain.sim import SimulationFailed, run_cocotb
from tests.rtl import TEST_RTL

SOURCES = [TEST_RTL / "sim_smoke.vhd"]


def test_generic_reaches_the_design(tmp_path):
    run_cocotb(toplevel="sim_smoke", sources=SOURCES, test_module="tests.tb.sim_smoke_tb",
               generics={"STEP": 3}, build_dir=tmp_path)


def test_failing_testbench_raises(tmp_path):
    with pytest.raises(SimulationFailed, match="1/1"):
        run_cocotb(toplevel="sim_smoke", sources=SOURCES,
                   test_module="tests.tb.sim_smoke_failing_tb", generics={"STEP": 3},
                   build_dir=tmp_path)
