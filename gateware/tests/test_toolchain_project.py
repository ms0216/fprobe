from pathlib import Path

import pytest

from fprobe_gw.toolchain.project import (ClockConstraint, GowinProject, IoDelay, Pin, render_cst,
                                         render_sdc, render_tcl)


@pytest.fixture
def project(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    for name in ("a.vhd", "top.vhd"):
        (src / name).write_text("-- x")
    return GowinProject(
        name="top", part="GW2A-LV18PG256C8/I7", family="GW2A-18C", top="board_top",
        sources=(src / "a.vhd", src / "top.vhd"),
        pins=(Pin("clk27", "H11", pull="UP"), Pin("led[0]", "C13"),
              Pin("ulpi_stp", "K11", pull="NONE", drive=8)),
        clocks=(ClockConstraint("clk27", "clk27", "port", 27_000_000),
                ClockConstraint("sys_clk", "sys_clk", "net", 100_285_714, source="clk27"),
                ClockConstraint("ulpi_clk", "ulpi_clk", "port", 60_000_000)),
        options=("-use_done_as_gpio 1 -use_ready_as_gpio 1",))


def test_tcl_adds_sources_with_absolute_paths(project):
    tcl = render_tcl(project)
    lines = tcl.splitlines()
    assert f"add_file -type vhdl {{{project.sources[0].as_posix()}}}" in lines
    assert "add_file -type cst top.cst" in lines
    assert "add_file -type sdc top.sdc" in lines


def test_tcl_selects_device_vhdl2008_and_top(project):
    tcl = render_tcl(project)
    assert "set_device -name GW2A-18C GW2A-LV18PG256C8/I7" in tcl
    assert "set_option -vhdl_std vhd2008" in tcl
    assert "set_option -top_module board_top" in tcl
    assert "set_option -use_done_as_gpio 1 -use_ready_as_gpio 1" in tcl
    assert tcl.splitlines()[-1] == "file copy -force impl/pnr/project.fs top.fs"


def test_sdc_periods(project):
    sdc = render_sdc(project)
    assert "create_clock -name clk27 -period 37.037037 [get_ports {clk27}]" in sdc
    assert "create_clock -name sys_clk -period 9.971510 [get_nets {sys_clk}]" in sdc


def test_sdc_separates_clocks_of_different_sources(project):
    sdc = render_sdc(project).splitlines()
    assert ("set_clock_groups -asynchronous -group [get_clocks {clk27 sys_clk}] "
            "-group [get_clocks {ulpi_clk}]") in sdc


def test_sdc_io_delays(project):
    delayed = GowinProject(**{**project.__dict__, "io_delays": (
        IoDelay("ulpi_clk", "input", ("ulpi_dir", "ulpi_data[*]"), 1.5, 3.5),
        IoDelay("ulpi_clk", "output", ("ulpi_stp",), -1.0, 5.0))})
    sdc = render_sdc(delayed).splitlines()
    assert "set_input_delay -clock ulpi_clk -max 3.500 [get_ports {ulpi_dir ulpi_data[*]}]" in sdc
    assert "set_input_delay -clock ulpi_clk -min 1.500 [get_ports {ulpi_dir ulpi_data[*]}]" in sdc
    assert "set_output_delay -clock ulpi_clk -max 5.000 [get_ports {ulpi_stp}]" in sdc
    assert "set_output_delay -clock ulpi_clk -min -1.000 [get_ports {ulpi_stp}]" in sdc


def test_cst_lists_each_pin_with_attributes(project):
    cst = render_cst(project).splitlines()
    assert 'IO_LOC "clk27" H11;' in cst
    assert 'IO_PORT "clk27" IO_TYPE=LVCMOS33 PULL_MODE=UP;' in cst
    assert 'IO_PORT "led[0]" IO_TYPE=LVCMOS33;' in cst
    assert 'IO_PORT "ulpi_stp" IO_TYPE=LVCMOS33 PULL_MODE=NONE DRIVE=8;' in cst


def test_duplicate_pin_assignment_is_rejected(project, tmp_path):
    clash = GowinProject(**{**project.__dict__, "pins": project.pins + (Pin("other", "H11"),)})
    with pytest.raises(ValueError, match="H11"):
        clash.prepare(tmp_path / "build")


def test_prepare_writes_all_files(project, tmp_path):
    out = tmp_path / "build"
    tcl_path = project.prepare(out)
    assert tcl_path == out / "top.tcl"
    assert (out / "top.sdc").read_text() == render_sdc(project)
    assert (out / "top.cst").read_text() == render_cst(project)


def test_missing_source_is_rejected(project, tmp_path):
    broken = GowinProject(**{**project.__dict__, "sources": (tmp_path / "nope.vhd",)})
    with pytest.raises(FileNotFoundError, match="nope.vhd"):
        broken.prepare(tmp_path / "build")


def test_cst_bank_voltage_and_differential_pairs():
    project = GowinProject(name="top", part="p", family="GW2A-18C", top="top", sources=(),
                           pins=(Pin("ddr_ck", "J1,J3", io_type="SSTL15D", pull="NONE", bank_vccio="1.5"),))
    assert render_cst(project).splitlines() == [
        'IO_LOC "ddr_ck" J1,J3;', 'IO_PORT "ddr_ck" IO_TYPE=SSTL15D PULL_MODE=NONE BANK_VCCIO=1.5;']


def test_cst_drive_and_hysteresis():
    project = GowinProject(name="top", part="p", family="GW2A-18C", top="top", sources=(),
                           pins=(Pin("swclk", "J14", pull="UP", drive=4, hysteresis="HIGH"),))
    assert render_cst(project).splitlines() == [
        'IO_LOC "swclk" J14;', 'IO_PORT "swclk" IO_TYPE=LVCMOS33 PULL_MODE=UP DRIVE=4 HYSTERESIS=HIGH;']


def test_cst_appends_placement_constraints():
    from fprobe_gw.boards.primer20k import PRIMER20K
    lines = PRIMER20K.ulpi_placements("device_i/link_i")
    assert lines[:2] == ('GROUP ulpi_out0 = { "device_i/link_i/data_q_0_s?" };', "GRP_LOC ulpi_out0 R[2:4]C[13:15];")
    project = GowinProject(name="top", part="p", family="GW2A-18C", top="top", sources=(),
                           pins=(Pin("clk27", "H11"),), placements=lines)
    assert render_cst(project).splitlines()[2:] == list(lines)


def test_sdc_bounds_clock_domain_crossings_when_requested():
    clocks = (ClockConstraint("sys_clk", "sys_clk", "net", 100_000_000),
              ClockConstraint("ulpi_clk", "ulpi_clk", "port", 60_000_000),
              ClockConstraint("ddr3_memory_clk", "ddr3_memory_clk", "net", 400_000_000, internal=True))
    project = GowinProject(name="top", part="p", family="GW2A-18C", top="top", sources=(), pins=(),
                           clocks=clocks, cdc_max_delay_ns=8.0)
    sdc = render_sdc(project).splitlines()
    assert "set_clock_groups -asynchronous -group [get_clocks {ddr3_memory_clk}]" in sdc
    assert "set_max_delay -from [get_clocks {sys_clk}] -to [get_clocks {ulpi_clk}] 8.000" in sdc
    assert "set_false_path -hold -from [get_clocks {ulpi_clk}] -to [get_clocks {sys_clk}]" in sdc
    assert not any("ddr3_memory_clk" in line for line in sdc if line.startswith("set_max_delay"))
    assert sum(line.startswith("set_clock_groups") for line in sdc) == 1
