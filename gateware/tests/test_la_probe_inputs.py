"""la_probe_inputs（R2: 内部の ch の段数をピンに揃える）。"""

import pytest

from fprobe_gw.rtl import CORE_GENERATED, rtl
from fprobe_gw.toolchain.sim import run_cocotb

SOURCES = [CORE_GENERATED / "timing_pkg.vhd", *rtl("la/la_filter.vhd", "la/la_probe_inputs.vhd")]


@pytest.mark.parametrize("filter_, age", [(True, 0), (True, 1), (False, 0)],
                         ids=["filter_sys", "filter_cap", "no_filter"])
def test_la_probe_inputs(tmp_path, monkeypatch, filter_, age):
    # filter_sys = fprobe_core の sys の経路、filter_cap = la_cap_domain（内部の ch は sys → cap の 1 段を通ってから来る）
    monkeypatch.setenv("LA_PROBE_FILTER", "1" if filter_ else "0")
    monkeypatch.setenv("LA_PROBE_AGE", str(age))
    run_cocotb(toplevel="la_probe_inputs", sources=SOURCES, test_module="tests.tb.la_probe_inputs_tb",
               generics={"WIDTH": 16, "LA_PIN_COUNT": 8, "LA_INPUT_COUNT": 0, "FILTER": filter_, "INTERNAL_AGE": age},
               build_dir=tmp_path)
