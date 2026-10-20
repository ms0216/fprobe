import subprocess
from pathlib import Path

import pytest

from fprobe_gw.toolchain.build import BuildError
from fprobe_gw.toolchain.project import ClockConstraint, Pin
from fprobe_gw.toolchain.vivado import (DEFAULT_VIVADO_HOME, VivadoProject, VivadoToolchain, parse_vivado_timing,
                                        render_vivado_tcl, render_xdc, vivado_undriven)
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


def test_xdc_has_pins_and_clocks(project):
    xdc = render_xdc(project)
    assert "set_property PACKAGE_PIN H16 [get_ports {sys_clk_in}]" in xdc
    assert "set_property IOSTANDARD LVCMOS33 [get_ports {sys_clk_in}]" in xdc
    assert "set_property PULLDOWN true [get_ports {pmod[0]}]" in xdc
    assert "create_clock -name sys_clk_in -period 8.000 [get_ports {sys_clk_in}]" in xdc


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
