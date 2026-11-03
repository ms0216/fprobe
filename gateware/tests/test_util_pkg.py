from fprobe_gw.rtl import rtl
from fprobe_gw.toolchain.sim import run_cocotb
from tests.rtl import TEST_RTL


def test_util_pkg_modular_counters(tmp_path):
    """総数（2^n で折り返す）の差・比べと、飽和の和（util_pkg）。"""
    run_cocotb(toplevel="util_mod_harness", sources=[*rtl("common/util_pkg.vhd"), TEST_RTL / "util_mod_harness.vhd"],
               test_module="tests.tb.util_mod_tb", build_dir=tmp_path)
