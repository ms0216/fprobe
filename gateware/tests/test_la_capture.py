import pytest

from fprobe_gw.rtl import CORE_GENERATED, rtl
from fprobe_gw.toolchain.sim import run_cocotb


@pytest.mark.parametrize("fast", [False, True], ids=["normal", "fast"])
def test_la_capture(tmp_path, fast):
    # FAST = true（取り込み領域の構成）でも、同じ試験（設定と run を同じ周期に変える書き方を含む）が通る
    run_cocotb(toplevel="la_capture", sources=[CORE_GENERATED / "timing_pkg.vhd",
                                                *rtl("common/util_pkg.vhd", "la/la_pkg.vhd", "la/la_front.vhd", "la/la_record.vhd", "la/la_capture.vhd")],
               test_module="tests.tb.la_capture_tb", generics={"WIDTH": 4, "DEPTH": 32, "FAST": fast}, build_dir=tmp_path)
