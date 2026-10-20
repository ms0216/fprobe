"""PYNQ-Z2 のボードの記述と、VHDL のボード定数・MMCM の包み・PS7 のブロック設計の一致。"""

import re

from fprobe_gw.cli import BOARDS
from fprobe_gw.boards.primer20k import PRIMER20K
from fprobe_gw.boards.pynq_z2 import PYNQ_Z2
from fprobe_gw.rtl import RTL_ROOT, rtl
from fprobe_gw.toolchain.sim import run_cocotb
from tests.test_boards_primer20k import vhdl_constants

PKG = RTL_ROOT / "boards" / "pynq_z2" / "pynq_z2_pkg.vhd"


def test_pmod_ja_follows_the_primer_j12_order():
    # デバッグの信号の番号（= ロジアナの ch 0〜7）は Primer と同じ。JA1〜4 = Y18 Y19 Y16 Y17、JA7〜10 = U18 U19 W18 W19
    assert PYNQ_Z2.debug_connector == PRIMER20K.debug_connector
    ja = [p.location for p in PYNQ_Z2.pin_groups["pmod1"]]
    assert ja == ["Y18", "Y19", "Y16", "Y17", "U18", "U19", "W18", "W19"]
    assert ja[PYNQ_Z2.debug_connector["swclk"]] == "Y18" and ja[PYNQ_Z2.debug_connector["swdio"]] == "Y17"


def test_fpga_pulls_follow_the_board_resistors():
    # 基板の抵抗: PMOD の 0・1・4・5 番 = 引き下げ、2・3・6・7 番 = 引き上げ（JA10 = W19 の引き上げで XIAO のキーが
    # 押され続けた。2026-10-20）。キーの行に使える = 引き下げのある出力の候補は予備と UART RX
    pmod = PYNQ_Z2.pin_groups["pmod1"]
    assert [p.pull for p in pmod] == ["DOWN", "DOWN", "UP", "UP", "DOWN", "DOWN", "UP", "UP"]
    assert [ch for ch, pull_up in PYNQ_Z2.outputs() if not pull_up] == [PYNQ_Z2.debug_connector["spare"],
                                                                        PYNQ_Z2.debug_connector["uart_rx"]]
    assert [p.pull for p in PYNQ_Z2.pin_groups["la_in"]] == ["DOWN", "DOWN", "UP", "UP", "DOWN", "DOWN"]


def test_pins_are_unique_and_lvcmos33():
    pins = [p for g in PYNQ_Z2.pin_groups.values() for p in g]
    assert len({p.location for p in pins}) == len(pins)
    assert len({p.port for p in pins}) == len(pins)
    assert all(p.io_type == "LVCMOS33" for p in pins)


def test_board_is_built_with_vivado():
    assert PYNQ_Z2.toolchain == "vivado" and PYNQ_Z2.part == "xc7z020clg400-1"
    assert BOARDS["pynq_z2"] is PYNQ_Z2
    sys_clock = PYNQ_Z2.sys_clock()
    assert sys_clock.kind == "pin" and sys_clock.frequency_hz == 100_000_000


def test_vhdl_package_matches_board_definition():
    c = vhdl_constants(PKG)
    assert c["SYS_CLK_HZ"] == PYNQ_Z2.sync_pll.fout_hz
    assert c["LA_INPUT_PINS"] == PYNQ_Z2.la_inputs()
    assert [c[f"DBG_{k.upper()}"] for k in PYNQ_Z2.debug_connector] == list(PYNQ_Z2.debug_connector.values())
    text = PKG.read_text(encoding="utf-8")
    m = PYNQ_Z2.sync_pll
    assert re.search(rf"MMCM_MULT\s*:\s*real\s*:=\s*{m.mult}\s*;", text)
    assert re.search(rf"MMCM_OUT_DIV\s*:\s*real\s*:=\s*{m.out_div}\s*;", text)
    choices = re.search(r"XOUT_PIN_CHOICES\s*:.*:=\s*\(([\d, ]+)\)", text)[1]
    assert [int(x) for x in choices.split(",")] == [PYNQ_Z2.debug_connector[n] for n in PYNQ_Z2.output_pins]


def test_ps7_block_design_script_exists_and_names_gp0():
    tcl = (RTL_ROOT / "boards" / "pynq_z2" / "ps7_bd.tcl").read_text(encoding="utf-8")
    assert "set fprobe_bd_name ps7_bd" in tcl and "create_bd_design $fprobe_bd_name" in tcl
    hp = (RTL_ROOT / "boards" / "pynq_z2" / "ps7_hp_bd.tcl").read_text(encoding="utf-8")
    assert "set fprobe_bd_name ps7_hp_bd" in hp and "set fprobe_hp0 1" in hp and "PCW_USE_S_AXI_HP0 1" in tcl
    assert "PCW_USE_M_AXI_GP0 1" in tcl and "PCW_EN_CLK0_PORT 0" in tcl
    assert "0x40000000" in tcl


def test_xilinx_mmcm_model_locks_and_runs(tmp_path):
    run_cocotb(toplevel="xilinx_mmcm", sources=[RTL_ROOT.parent / "tests" / "rtl" / "sim_xilinx_prims.vhd",
                                                *rtl("boards/pynq_z2/xilinx_mmcm.vhd")],
               test_module="tests.tb.xilinx_mmcm_tb", build_dir=tmp_path)
