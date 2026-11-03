"""深いバッファを 1 周期に 8 サンプルで記録する構成（fprobe_pynq_deep_fast と同じ）の fprobe_core の端から端（ISERDESE2 の模型を通す）。
docs/superpowers/specs/2026-11-01-la-deep-fast-design.md §8"""

import dataclasses

from fprobe_gw import caps
from fprobe_gw.designs import core_sources
from fprobe_gw.rtl import rtl
from fprobe_gw.toolchain.sim import run_cocotb
from tests.rtl import TEST_RTL
from tests.test_caps import TEST_CAPS

DEEP_LANES_CAPS = dataclasses.replace(
    TEST_CAPS, sys_clock_hz=100_000_000, timebase_hz=100_000_000, la_lanes=8, la_depth=0, la_sample_hz=800_000_000,
    cap_ratio=4, la_fine_pins=True, la_filter=False, la_pulse=False, la_testgen=True, mem_bytes=1024 * 16,
    la_stream_events=True, la_stream_gaps=True, la_pack=True, la_stream_bytes_per_s=50_000_000)


def test_fprobe_core_deep_lanes(tmp_path):
    pkg = tmp_path / "test_caps_pkg.vhd"
    pkg.write_text(caps.render_package("test_caps_pkg", DEEP_LANES_CAPS), encoding="utf-8")
    run_cocotb(parallel=True, toplevel="fprobe_core_deep_lanes_harness",
               sources=[TEST_RTL / "sim_xilinx_prims.vhd",
                        *rtl("boards/pynq_z2/pynq_z2_pkg.vhd", "boards/pynq_z2/xilinx_la_iserdes.vhd"),
                        *core_sources(pkg), TEST_RTL / "fprobe_core_deep_lanes_harness.vhd"],
               test_module="tests.tb.fprobe_core_deep_lanes_tb", build_dir=tmp_path / "sim")
