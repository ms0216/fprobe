"""xilinx_la_iserdes（PYNQ-Z2 のロジアナの ISERDESE2 の包み）と、GHDL の ISERDESE2 の模型（Vivado の UNISIM の記録と比べる）。"""

import pytest

from fprobe_gw.rtl import RTL_ROOT, rtl
from fprobe_gw.toolchain.sim import run_cocotb


@pytest.mark.parametrize("lanes", [6, 8])
def test_xilinx_la_iserdes(tmp_path, monkeypatch, lanes):
    monkeypatch.setenv("ISERDES_LANES", str(lanes))
    monkeypatch.setenv("ISERDES_CHANNELS", "3")
    run_cocotb(toplevel="xilinx_la_iserdes",
               sources=[RTL_ROOT.parent / "tests" / "rtl" / "sim_xilinx_prims.vhd",
                        *rtl("boards/pynq_z2/xilinx_la_iserdes.vhd")],
               test_module="tests.tb.xilinx_la_iserdes_tb", generics={"CHANNELS": 3, "LANES": lanes},
               build_dir=tmp_path)
