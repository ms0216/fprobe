"""CSR events の written / written_hi（64 ビット）。外部メモリ無しのイベント記録（event_ring）の構成。"""
import dataclasses

from fprobe_gw import caps
from fprobe_gw.designs import core_sources
from fprobe_gw.toolchain.sim import run_cocotb
from tests.rtl import TEST_RTL
from tests.test_caps import TEST_CAPS


def test_event_csr_64(tmp_path):
    pkg = tmp_path / "test_caps_pkg.vhd"
    pkg.write_text(caps.render_package("test_caps_pkg", dataclasses.replace(TEST_CAPS, event_ring_bits=6)),
                   encoding="utf-8")
    run_cocotb(toplevel="fprobe_core_harness", sources=[*core_sources(pkg), TEST_RTL / "fprobe_core_harness.vhd"],
               test_module="tests.tb.event_csr_64_tb", generics={"EVENT_COUNT_START_BELOW_4G": 3},   # 記録を始めた周期の設定（種類 16）が 1 件
               build_dir=tmp_path / "sim")
