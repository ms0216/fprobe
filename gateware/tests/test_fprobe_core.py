import dataclasses

import pytest

from fprobe_gw import caps
from fprobe_gw.designs import core_sources
from fprobe_gw.toolchain.sim import run_cocotb
from tests.rtl import TEST_RTL
from tests.test_caps import TEST_CAPS

# 全部ある構成と、Primer の 2 構成（B22: 標準 = JTAG なし、JTAG = サンプラ・故障注入なし）
CONFIGURATIONS = {
    "full": TEST_CAPS,
    "standard": dataclasses.replace(TEST_CAPS, jtag=False),
    "jtag": dataclasses.replace(TEST_CAPS, sampler=False, inject=False),
    "no_outputs": dataclasses.replace(TEST_CAPS, outputs=()),           # 小さい FPGA: 出力の端子なし
    "no_la_front": dataclasses.replace(TEST_CAPS, la_filter=False, la_pulse=False),   # U21 の前段なし
    "no_la_inputs": dataclasses.replace(TEST_CAPS, la_inputs=0),       # U22 の専用の入力なし
    "event_ring": dataclasses.replace(TEST_CAPS, event_ring_bits=6),   # 外部メモリ無しのイベント記録（64 件）
    "no_sampler_match": dataclasses.replace(TEST_CAPS, sampler_match=False),   # R21 を外した構成（Primer）
    "sync": dataclasses.replace(TEST_CAPS, sync=True, event_ring_bits=6),   # ボードの間の時刻の同期（組をイベントで読む）
}


@pytest.mark.parametrize("configuration", CONFIGURATIONS)
def test_fprobe_core(tmp_path, configuration):
    pkg = tmp_path / "test_caps_pkg.vhd"
    pkg.write_text(caps.render_package("test_caps_pkg", CONFIGURATIONS[configuration]), encoding="utf-8")
    run_cocotb(toplevel="fprobe_core_harness",
               sources=[*core_sources(pkg), TEST_RTL / "fprobe_core_harness.vhd"],
               test_module="tests.tb.fprobe_core_tb", build_dir=tmp_path / "sim")
