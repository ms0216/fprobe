"""1 周期に複数のサンプルの構成（fprobe_pynq_fast と同じ）の fprobe_core の端から端（ISERDESE2 の模型を通す）。
N = 6（専用の入力だけ。段 1）と N = 8（ピンも。段 2 = いまの fprobe_pynq_fast）。"""

import dataclasses

import pytest

from fprobe_gw import caps
from fprobe_gw.designs import core_sources
from fprobe_gw.rtl import rtl
from fprobe_gw.toolchain.sim import run_cocotb
from tests.rtl import TEST_RTL
from tests.test_caps import TEST_CAPS

def lanes_caps(lanes: int, fine_pins: bool):
    return dataclasses.replace(TEST_CAPS, sys_clock_hz=100_000_000, timebase_hz=100_000_000, la_lanes=lanes,
                               la_depth=32 * lanes, la_sample_hz=100_000_000 * lanes, cap_ratio=lanes // 2,
                               la_fine_pins=fine_pins, la_filter=False, la_pulse=False, la_testgen=True)


@pytest.mark.parametrize("lanes,fine_pins", [(6, False), (8, True)])
def test_fprobe_core_lanes(tmp_path, monkeypatch, lanes, fine_pins):
    monkeypatch.setenv("CORE_LANES", str(lanes))
    monkeypatch.setenv("CORE_PIN_LANES", "1" if fine_pins else "0")
    pkg = tmp_path / "test_caps_pkg.vhd"
    pkg.write_text(caps.render_package("test_caps_pkg", lanes_caps(lanes, fine_pins)), encoding="utf-8")
    run_cocotb(toplevel="fprobe_core_lanes_harness",
               sources=[TEST_RTL / "sim_xilinx_prims.vhd",
                        *rtl("boards/pynq_z2/pynq_z2_pkg.vhd", "boards/pynq_z2/xilinx_la_iserdes.vhd"),
                        *core_sources(pkg), TEST_RTL / "fprobe_core_lanes_harness.vhd"],
               test_module="tests.tb.fprobe_core_lanes_tb", build_dir=tmp_path / "sim")
