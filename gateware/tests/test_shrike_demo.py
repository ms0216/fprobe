"""Shrike-Lite のデモ（target_ip/fprobe_obs/examples/shrike_lite）の回路。"""

from pathlib import Path

from fprobe_gw.rtl import target_ip
from fprobe_gw.toolchain.sim import run_cocotb

DEMO = "fprobe_obs/examples/shrike_lite/rtl/"


def test_demo_spi_slave(tmp_path):
    run_cocotb(toplevel="demo_spi_slave", sources=target_ip(DEMO + "demo_spi_slave.vhd"),
               test_module="tests.tb.demo_spi_slave_tb", build_dir=tmp_path)


def test_demo_accel_with_the_planted_bug(tmp_path):
    run_cocotb(toplevel="demo_accel", sources=target_ip(DEMO + "demo_accel.vhd"), generics={"STEP_CYCLES": 1},
               test_module="tests.tb.demo_accel_tb", build_dir=tmp_path, test_filter="^(?!.*when_fixed)")


def test_demo_accel_fixed(tmp_path):
    run_cocotb(toplevel="demo_accel", sources=target_ip(DEMO + "demo_accel.vhd"), generics={"CLEAR_ON_RESTART": True, "STEP_CYCLES": 1},
               test_module="tests.tb.demo_accel_tb", build_dir=tmp_path, test_filter="^(?!.*keeps_the_partial)")


CORE = target_ip("fprobe_obs/rtl/obs_async_fifo.vhd", "fprobe_obs/rtl/obs_core.vhd", "fprobe_obs/rtl/obs_tck_sampler.vhd",
                 "fprobe_obs/rtl/obs_soft_tap.vhd",
                 "fprobe_obs/rtl/obs_soft_core.vhd", "fprobe_obs/rtl/obs_snap_core.vhd", DEMO + "demo_spi_slave.vhd", DEMO + "demo_accel.vhd",
                 DEMO + "shrike_demo_core.vhd")


def test_shrike_demo_core(tmp_path):
    run_cocotb(toplevel="shrike_demo_core", sources=CORE, generics={"OBS": 2, "FIFO_DEPTH": 16, "STEP_CYCLES": 1, "CLOCK_HZ": 50_000_000},
               test_module="tests.tb.shrike_demo_core_tb", build_dir=tmp_path, test_filter="^(?!.*probes_)")


def test_shrike_demo_core_probes(tmp_path):
    # 計算中に SPI で開始し直す（S4）には、計算が SPI の 1 回の書き込みより長くかかる必要がある: STEP_CYCLES = 256
    run_cocotb(toplevel="shrike_demo_core", sources=CORE, generics={"OBS": 1, "STEP_CYCLES": 256},
               test_module="tests.tb.shrike_demo_core_tb", build_dir=tmp_path, test_filter="probes_")
