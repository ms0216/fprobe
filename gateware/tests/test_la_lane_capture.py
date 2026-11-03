"""la_lane_capture（1 周期に N サンプルのロジアナ）: 端子 → ISERDESE2 の模型 → トリガ・記録の端から端（N = 6・8）。"""

import pytest

from fprobe_gw.rtl import CORE_GENERATED, RTL_ROOT, rtl
from fprobe_gw.toolchain.sim import run_cocotb

SOURCES = [RTL_ROOT.parent / "tests" / "rtl" / "sim_xilinx_prims.vhd", CORE_GENERATED / "timing_pkg.vhd",
           *rtl("common/util_pkg.vhd", "boards/pynq_z2/pynq_z2_pkg.vhd", "boards/pynq_z2/xilinx_la_iserdes.vhd",
                "la/la_record.vhd", "la/la_lane_front.vhd", "la/la_lane_capture.vhd"),
           RTL_ROOT.parent / "tests" / "rtl" / "la_lane_e2e_harness.vhd"]


@pytest.mark.parametrize("lanes", [6, 8])
def test_la_lane_capture_end_to_end(tmp_path, monkeypatch, lanes):
    monkeypatch.setenv("LANE_N", str(lanes))
    run_cocotb(toplevel="la_lane_e2e_harness", sources=SOURCES, test_module="tests.tb.la_lane_e2e_tb",
               generics={"LANES": lanes, "DEPTH": 32}, build_dir=tmp_path)
