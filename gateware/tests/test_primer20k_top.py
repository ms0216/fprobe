from fprobe_gw.boards.primer20k import PRIMER20K
from fprobe_gw.designs import DESIGNS
from fprobe_gw.toolchain.sim import run_cocotb
from tests.rtl import TEST_RTL


def test_bringup_top_simulates_with_primitive_models(tmp_path):
    design = DESIGNS["bringup"]
    run_cocotb(toplevel=design.top, sources=[*design.sources, TEST_RTL / "sim_gowin_prims.vhd"],
               test_module="tests.tb.primer20k_bringup_top_tb", build_dir=tmp_path)


def test_bringup_project_targets_primer(tmp_path):
    project = DESIGNS["bringup"].project()
    assert project.top == "primer20k_bringup_top"
    assert project.part == "GW2A-LV18PG256C8/I7"
    assert any(p.name == "primer20k_bringup_top.vhd" for p in project.sources)
    assert {c.name for c in project.clocks} == {"clk27", "sys_clk"}
    assert "-use_done_as_gpio 1 -use_ready_as_gpio 1" in project.options


def test_ulpi_probe_project_constrains_ulpi_clocks():
    project = DESIGNS["ulpi_probe"].project()
    assert project.top == "primer20k_ulpi_probe_top"
    assert {c.name for c in project.clocks} == {"clk27", "ulpi_clk"}
    assert {p.port for p in project.pins} >= {"ulpi_clk", "ulpi_data[7]", "uart_tx", "led[0]"}


def test_bringup_project_has_no_ulpi_pins():
    ports = {p.port for p in DESIGNS["bringup"].project().pins}
    assert "uart_tx" in ports and not any(p.startswith("ulpi") for p in ports)


def test_fprobe_project_constrains_usb_io_and_declares_capabilities():
    design = DESIGNS["fprobe"]
    project = design.project()
    assert project.top == "primer20k_fprobe_top"
    assert {c.name for c in project.clocks} == {"clk27", "sys_clk", "ulpi_clk", "ddr3_memory_clk", "mem_clk"}
    assert [ip.module for ip in project.ips] == ["DDR3MI"]
    assert design.capabilities.mem_bytes == PRIMER20K.ddr3.size_bytes
    assert {d.direction for d in project.io_delays} == {"input", "output"}
    assert not any(p.port.startswith("uart") for p in project.pins)
    assert design.capabilities.timebase_hz == design.capabilities.sys_clock_hz == PRIMER20K.sync_pll.fout_hz
