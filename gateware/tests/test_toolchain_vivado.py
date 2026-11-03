import re
import subprocess
from pathlib import Path

import pytest

from fprobe_gw.toolchain.build import BuildError
from fprobe_gw.toolchain.project import ClockConstraint, Pin
from fprobe_gw.toolchain.vivado import (DEFAULT_VIVADO_HOME, VivadoProject, VivadoToolchain, parse_vivado_timing,
                                        render_vivado_tcl, render_xdc, vivado_undriven,
                                        vivado_unmatched_constraints)
from fprobe_gw.toolchain.vivado_build import VivadoBuilder

REPORT = (Path(__file__).parent / "data" / "vivado_timing_summary.rpt").read_text(encoding="utf-8")


@pytest.fixture
def project(tmp_path):
    src = tmp_path / "top.vhd"
    src.write_text("-- top")
    return VivadoProject(
        name="top", part="xc7z020clg400-1", top="top", sources=(src,),
        pins=(Pin("sys_clk_in", "H16"), Pin("pmod[0]", "Y18", pull="DOWN")),
        clocks=(ClockConstraint("sys_clk_in", "sys_clk_in", "port", 125_000_000),))


def test_tcl_reads_vhdl2008_and_sets_the_part(project, tmp_path):
    tcl = render_vivado_tcl(project, tmp_path, "Explore")
    assert "read_vhdl -vhdl2008" in tcl
    assert "synth_design -top top -part xc7z020clg400-1" in tcl
    assert "place_design -directive Explore" in tcl
    assert "write_bitstream" in tcl


def test_tcl_reads_a_module_constraint_next_to_its_source_scoped_to_the_entity(project, tmp_path):
    # ソースの隣の <entity>.xdc を read_xdc -ref <entity> で読む（cdc_value の多周期の経路など）。無いソースは何もしない
    sub = tmp_path / "m"
    sub.mkdir()
    (sub / "cdc_x.vhd").write_text("-- x\n", encoding="utf-8")
    (sub / "cdc_x.xdc").write_text("# x\n", encoding="utf-8")
    p = VivadoProject(name="top", part=project.part, top="top", sources=(*project.sources, sub / "cdc_x.vhd"), pins=project.pins)
    lines = render_vivado_tcl(p, tmp_path, "Explore").splitlines()
    # -unmanaged: Tcl として読む（generic で FF が無い実体を if で飛ばす）
    scoped = [line for line in lines if line.startswith("read_xdc -unmanaged -ref")]
    assert len(scoped) == 1 and scoped[0].startswith("read_xdc -unmanaged -ref cdc_x ") and "cdc_x.xdc" in scoped[0]
    assert lines.index(scoped[0]) < lines.index("synth_design -top top -part xc7z020clg400-1")


def test_the_cdc_value_constraint_is_found_for_the_pynq_designs():
    from fprobe_gw.designs import DESIGNS
    from fprobe_gw.toolchain.vivado import module_constraints
    names = {x.name for x in module_constraints(DESIGNS["fprobe_pynq_fast"].project().sources)}
    assert "cdc_value.xdc" in names


def test_tcl_optimizes_physically_after_placement_and_after_routing(project, tmp_path):
    """配置の後と配線の後に、物理最適化（高負荷の信号の複製・再配置）をかける。配線の後のかけ直しは、配線で分かった遅い
    経路だけを直す（2026-10-29、PYNQ の高い周波数の試し: 遅い経路の多くは論理 3〜5 段で、遅れの原因は配られる先の数だった）。"""
    lines = render_vivado_tcl(project, tmp_path, "Explore").splitlines()
    place, route = lines.index("place_design -directive Explore"), lines.index("route_design -directive Explore")
    assert "phys_opt_design -directive AggressiveExplore" in lines[place + 1:route]
    assert "phys_opt_design -directive AggressiveExplore" in lines[route + 1:lines.index("report_timing_summary -file timing.rpt")]


def test_xdc_has_pins_and_clocks(project):
    xdc = render_xdc(project)
    assert "set_property PACKAGE_PIN H16 [get_ports {sys_clk_in}]" in xdc
    assert "set_property IOSTANDARD LVCMOS33 [get_ports {sys_clk_in}]" in xdc
    assert "set_property PULLDOWN true [get_ports {pmod[0]}]" in xdc
    assert "create_clock -name sys_clk_in -period 8.000 [get_ports {sys_clk_in}]" in xdc


def test_xdc_sets_a_fast_slew_for_a_pin_that_toggles_at_100_mhz_and_more(project):
    # 出力の立ち上がり・立ち下がりの速さ（SLEW）。既定の SLOW では、150 MHz の方形波が振幅を保てない
    fast = VivadoProject(name=project.name, part=project.part, top=project.top, sources=project.sources,
                         pins=(Pin("testgen_pin", "B20", drive=8, slew="FAST"), Pin("sync_out", "A20", drive=8)),
                         clocks=project.clocks)
    xdc = render_xdc(fast)
    assert "set_property SLEW FAST [get_ports {testgen_pin}]" in xdc
    assert "SLEW" not in [line for line in xdc.splitlines() if "sync_out" in line and "SLEW" in line]
    assert xdc.count("SLEW") == 1                                     # 指定のないピンには出さない


def test_xdc_renames_a_generated_clock_on_a_pin():
    p = VivadoProject(name="top", part="xc7z020clg400-1", top="top", sources=(), pins=(),
                      clocks=(ClockConstraint("sys_clk", "clock_i/mmcm_i/CLKOUT0", "pin", 100_000_000),))
    assert "create_generated_clock -name sys_clk [get_pins {clock_i/mmcm_i/CLKOUT0}]" in render_xdc(p)


def test_timing_report_is_parsed():
    timing = parse_vivado_timing(REPORT)
    (clock,) = timing.clocks
    assert clock.name == "sys_clk_in"
    assert clock.constraint_mhz == pytest.approx(125.0)
    assert clock.fmax_mhz == pytest.approx(1000 / (8.0 - 4.357))
    assert clock.setup_tns == 0.0 and clock.hold_tns == 0.0
    assert timing.met


def test_negative_slack_is_not_met():
    bad = REPORT.replace("sys_clk_in          4.357        0.000", "sys_clk_in         -0.200       -1.500")
    assert not parse_vivado_timing(bad).met


def test_undriven_warnings_are_found():
    log = "WARNING: [Synth 8-3848] Net foo in module/entity top does not have driver. [top.vhd:12]\nINFO: ok\n"
    assert vivado_undriven(log) == ["WARNING: [Synth 8-3848] Net foo in module/entity top does not have driver. [top.vhd:12]"]


def test_unmatched_constraints_of_our_own_files_are_found_but_not_those_of_generated_ip():
    ours = ("CRITICAL WARNING: [Vivado 12-4739] set_multicycle_path:No valid object(s) found for "
            "'-to [get_cells -quiet {value_r_reg[*]}]'. [C:/x/gateware/rtl/common/cdc_value.xdc:9]")
    ip = ("CRITICAL WARNING: [Vivado 12-4739] set_output_delay:No valid object(s) found for "
          "'-clock [get_clocks PJTAG_TCK]'. [c:/fw83/Default/.gen/sources_1/bd/ps7_hp_bd/ip/ps7_hp_bd_ps_0/ps7_hp_bd_ps_0.xdc:20]")
    assert vivado_unmatched_constraints("\n".join([ours, ip, ours, "INFO: ok"])) == [ours]


@pytest.mark.parametrize("xdc", ["cdc_value.xdc", "sync_bit.xdc"])
def test_module_constraints_match_the_registers_inside_the_cross_clock_generate(xdc):
    # Vivado は generate の中の FF を <generate のラベル>.<信号>_reg と名付ける（cross_g.held_reg[3] 等）。接頭辞なしの名前は当たらない
    text = (Path(__file__).parents[1] / "rtl" / "common" / xdc).read_text(encoding="utf-8")
    patterns = re.findall(r"get_cells(?: -quiet)? \{([^}]*)\}", text)
    assert patterns and all(p.startswith("*") for p in patterns)


class FakeVivado:
    def __init__(self, stdout="ok", report=REPORT):
        self.stdout, self.report, self.calls = stdout, report, []

    def __call__(self, args, *, cwd, **kwargs):
        self.calls.append(list(map(str, args)))
        Path(cwd, "timing.rpt").write_text(self.report, encoding="utf-8")
        Path(cwd, "top.bit").write_bytes(b"bit")
        return subprocess.CompletedProcess(args, 0, stdout=self.stdout, stderr="")


def test_build_runs_vivado_in_batch_and_checks_timing(project, tmp_path):
    run = FakeVivado()
    result = VivadoBuilder(VivadoToolchain(tmp_path / "v"), run=run).build(project, tmp_path / "b")
    assert result.bitstream == tmp_path / "b" / "top.bit" and result.timing.met
    assert run.calls[0][:3] == [str(tmp_path / "v" / "bin" / "vivado.bat"), "-mode", "batch"]
    assert (tmp_path / "b" / "inputs.sha256").is_file()


def test_undriven_warnings_fail_the_build(project, tmp_path):
    run = FakeVivado(stdout="WARNING: [Synth 8-3848] Net x in module/entity top does not have driver. [a.vhd:1]")
    with pytest.raises(BuildError, match="駆動されない"):
        VivadoBuilder(VivadoToolchain(tmp_path / "v"), run=run).build(project, tmp_path / "b")
    assert not (tmp_path / "b" / "top.bit").exists()


def test_unmatched_constraints_fail_the_build(project, tmp_path):
    run = FakeVivado(stdout="CRITICAL WARNING: [Vivado 12-4739] set_false_path:No valid object(s) found for "
                            "'-to [get_cells -quiet {stage_reg[0]}]'. [C:/x/gateware/rtl/common/sync_bit.xdc:7]")
    with pytest.raises(BuildError, match="対象の見つからない制約"):
        VivadoBuilder(VivadoToolchain(tmp_path / "v"), run=run).build(project, tmp_path / "b")
    assert not (tmp_path / "b" / "top.bit").exists()


def test_build_best_stops_at_the_first_directive_that_meets_timing(project, tmp_path):
    bad = REPORT.replace("sys_clk_in          4.357        0.000", "sys_clk_in         -0.200       -1.500")
    reports = iter([bad, REPORT])

    def run(args, *, cwd, **kw):
        Path(cwd, "timing.rpt").write_text(next(reports), encoding="utf-8")
        Path(cwd, "top.bit").write_bytes(b"bit")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    result = VivadoBuilder(VivadoToolchain(tmp_path / "v"), run=run).build_best(
        project, tmp_path / "b", directives=("Default", "Explore", "ExtraTimingOpt"))
    assert result.timing.met
    assert (tmp_path / "b" / "top.bit").is_file()


def test_locate_uses_env_and_default(tmp_path):
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "vivado.bat").write_bytes(b"")
    assert VivadoToolchain.locate(env={"FPROBE_VIVADO_HOME": str(tmp_path)}).home == tmp_path
    assert DEFAULT_VIVADO_HOME == Path(r"C:\AMDDesignTools\2025.2\Vivado")


def test_a_clock_with_only_pulse_width_checks_is_reported():
    # MMCM の入力クロック（sys_clk_in）は経路が無く、パルス幅の列しか無い。報告から消えて「無い」と誤らない
    report = (Path(__file__).parent / "data" / "vivado_timing_mmcm.rpt").read_text(encoding="utf-8")
    timing = parse_vivado_timing(report)
    names = {c.name: c for c in timing.clocks}
    assert {"sys_clk_in", "sys_clk"} <= set(names)
    assert names["sys_clk"].constraint_mhz == pytest.approx(100.0)
    assert names["sys_clk"].fmax_mhz == pytest.approx(1000 / (10.0 - 1.556))
    assert names["sys_clk_in"].fmax_mhz is None and timing.met
