"""登録したどのデザインも、トップから GHDL で解析・展開（elaboration）できること。

ボードのトップ（primer20k_fprobe_top・pynq_z2_fprobe_top 等）は cocotb のシミュレーションを通らないので、ソースの並び・
entity のポートと generic のつなぎ・型の誤りを、ベンダーのツールでビルドする前にここで見つける。ベンダーのプリミティブは
試験用の模型（tests/rtl/sim_gowin_prims.vhd・sim_xilinx_prims.vhd）を使う。動作は確かめない（それは各 entity の試験）。
"""

import subprocess

import pytest

from fprobe_gw.designs import DESIGNS
from fprobe_gw.toolchain.ghdl import GhdlToolchain
from tests.rtl import TEST_RTL


def _ghdl(args: list[str], cwd) -> subprocess.CompletedProcess:
    toolchain = GhdlToolchain.locate()
    return subprocess.run([str(toolchain.ghdl), *args], capture_output=True, text=True, cwd=cwd,
                          env=toolchain.environment())


@pytest.mark.parametrize("name", sorted(DESIGNS))
def test_design_elaborates(tmp_path, name):
    design = DESIGNS[name]
    prims = TEST_RTL / ("sim_xilinx_prims.vhd" if design.board.toolchain == "vivado" else "sim_gowin_prims.vhd")
    flags = ["--std=08", "-frelaxed", f"--workdir={tmp_path}"]
    r = _ghdl(["-i", *flags, str(prims), *map(str, design.sources)], tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    r = _ghdl(["-m", *flags, design.top], tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
