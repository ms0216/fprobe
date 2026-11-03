import pytest

from fprobe_gw.rtl import CORE_GENERATED, rtl
from fprobe_gw.toolchain.sim import run_cocotb

SOURCES = [CORE_GENERATED / "timing_pkg.vhd",
           *rtl("common/util_pkg.vhd", "la/la_front.vhd", "la/la_filter.vhd", "la/la_probe_inputs.vhd", "la/la_pulse.vhd",
                "la/la_record.vhd", "la/la_capture.vhd", "la/la_cap_domain.vhd")]


@pytest.mark.parametrize("ratio", [2, 3, 4])
def test_la_cap_domain(tmp_path, monkeypatch, ratio):
    monkeypatch.setenv("LA_CAP_RATIO", str(ratio))
    run_cocotb(toplevel="la_cap_domain", sources=SOURCES, test_module="tests.tb.la_cap_domain_tb",
               generics={"WIDTH": 16, "DEPTH": 32, "LA_PIN_COUNT": 8, "LA_INPUT_COUNT": 0, "RATIO": ratio},
               build_dir=tmp_path)


def test_la_cap_domain_without_the_filter_keeps_the_latency_and_the_alignment(tmp_path, monkeypatch):
    # LA_FILTER = false でも同期化と揃えの段は同じ（latency の申告・内部の ch の揃えが構成に依らない）。フィルタそのものの試験は除く
    monkeypatch.setenv("LA_CAP_RATIO", "3")
    run_cocotb(toplevel="la_cap_domain", sources=SOURCES, test_module="tests.tb.la_cap_domain_tb",
               generics={"WIDTH": 16, "DEPTH": 32, "LA_PIN_COUNT": 8, "LA_INPUT_COUNT": 0, "RATIO": 3, "LA_FILTER": False},
               build_dir=tmp_path, test_filter=r"\.(?!the_pin_glitch_filter)\w+$")
