"""la_lane_inputs（1 周期に複数のサンプルのレーンの並び。時刻の正しさ S1b の N2: 内部の ch を細かい ch の時間軸に揃える）。"""

import pytest

from fprobe_gw.designs import pynq_z2_iserdes_newest_age
from fprobe_gw.rtl import rtl
from fprobe_gw.toolchain.sim import run_cocotb


@pytest.mark.parametrize("lanes, age, fine", [(8, pynq_z2_iserdes_newest_age(8), True), (6, pynq_z2_iserdes_newest_age(6), False),
                                              (8, 0, False), (4, 6, False)],
                         ids=["fast", "n6_sys_pins", "age0", "age_over_lanes"])
def test_la_lane_inputs(tmp_path, monkeypatch, lanes, age, fine):
    monkeypatch.setenv("LANE_N", str(lanes))
    monkeypatch.setenv("LANE_AGE", str(age))
    monkeypatch.setenv("LANE_FINE", "1" if fine else "0")
    run_cocotb(toplevel="la_lane_inputs", sources=rtl("la/la_lane_inputs.vhd"), test_module="tests.tb.la_lane_inputs_tb",
               generics={"WIDTH": 16, "LANES": lanes, "LA_PIN_COUNT": 8, "LA_INPUT_COUNT": 2, "FINE_PINS": fine,
                         "IN_AGE": age},
               build_dir=tmp_path)
