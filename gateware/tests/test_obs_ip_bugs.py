"""target_ip/fprobe_obs の不具合の再現試験（検出のみ。strict の xfail。台帳 docs/reports/bugs/rtl-obs-ip.md）。
直ると XPASS(strict) で失敗して気づく。直したら xfail を外す。"""

import subprocess

import pytest

from fprobe_gw.rtl import target_ip
from fprobe_gw.toolchain.ghdl import GhdlToolchain
from fprobe_gw.toolchain.sim import SimulationFailed, run_cocotb

CORE = ("fprobe_obs/rtl/obs_async_fifo.vhd", "fprobe_obs/rtl/obs_core.vhd", "fprobe_obs/rtl/obs_soft_tap.vhd")
DEMO = "fprobe_obs/examples/shrike_lite/rtl/"


def test_idcode_bit0_zero_is_rejected(tmp_path):
    with pytest.raises(SimulationFailed):
        run_cocotb(toplevel="obs_snap_core",
                   sources=target_ip("fprobe_obs/rtl/obs_tck_sampler.vhd", "fprobe_obs/rtl/obs_soft_tap.vhd",
                                     "fprobe_obs/rtl/obs_snap_core.vhd"),
                   generics={"IDCODE": "00001111000010110101000000000000"},
                   test_module="tests.tb.obs_ip_bugs_tb", test_filter="idcode_bit0_zero",
                   build_dir=tmp_path)


@pytest.mark.xfail(strict=True, reason="BUG-RTL-OBSIP-02: obs_soft_core は ONE_CLOCK=false でも obs_tck_sampler.vhd が無いと解析できない"
                                       "（README は ONE_CLOCK=true のときだけ足すと読める）")
def test_soft_core_two_clock_needs_no_sampler_file(tmp_path):
    tool = GhdlToolchain.locate()
    work = tmp_path / "work"
    work.mkdir()
    files = [str(p) for p in target_ip(*CORE, "fprobe_obs/rtl/obs_soft_core.vhd")]
    r = subprocess.run([str(tool.ghdl), "-a", "--std=08", f"--workdir={work}", *files], env=tool.environment(),
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_shrike_demo_core_unknown_obs_drives_tdo(tmp_path):
    run_cocotb(toplevel="shrike_demo_core",
               sources=target_ip(*CORE, "fprobe_obs/rtl/obs_tck_sampler.vhd", "fprobe_obs/rtl/obs_soft_core.vhd",
                                 "fprobe_obs/rtl/obs_snap_core.vhd", DEMO + "demo_spi_slave.vhd",
                                 DEMO + "demo_accel.vhd", DEMO + "shrike_demo_core.vhd"),
               generics={"OBS": 3, "STEP_CYCLES": 1}, test_module="tests.tb.obs_ip_bugs_tb",
               test_filter="undeclared_obs_option", build_dir=tmp_path)
