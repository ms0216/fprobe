"""構成 fprobe_pynq（PYNQ-Z2、全機能を 1 つに）の申告・ソース・ピン。"""
from fprobe_gw import caps
from fprobe_gw.designs import DESIGNS
from fprobe_gw.toolchain.vivado import VivadoProject


def test_fprobe_pynq_declares_everything_in_one_design():
    d = DESIGNS["fprobe_pynq"]
    c = d.capabilities
    assert c.board == "PYNQ-Z2" and c.design == "fprobe_pynq" and c.host_link == caps.LINK_ETH
    assert c.jtag and c.sampler and c.inject and c.la_filter and c.la_pulse and c.swo_manchester
    assert c.uart_flow and c.outputs and c.la_inputs == 6 and c.la_depth > 0
    assert not c.aux and c.mem_bytes == 0


def test_fprobe_pynq_is_a_vivado_project_with_the_ps7_block_design():
    p = DESIGNS["fprobe_pynq"].project()
    assert isinstance(p, VivadoProject) and p.top == "pynq_z2_fprobe_top"
    names = {s.name for s in p.sources}
    assert {"zynq_frame_link.vhd", "frame_mux.vhd", "fprobe_core.vhd", "pynq_z2_fprobe_top.vhd"} <= names
    assert [b.name for b in p.block_designs] == ["ps7_bd.tcl"]
    ports = {pin.port for pin in p.pins}
    assert "sys_clk_in" in ports and "pmod1[7]" in ports and "la_in[5]" in ports
    assert {c.name for c in p.clocks} == {"sys_clk_in", "sys_clk"}


def test_fprobe_pynq_deep_uses_the_ps_ddr_as_the_deep_buffer():
    # P3: PS の DDR の上端 32 MB（uEnv.txt の mem=479M で Linux から外す）を深いバッファに。内蔵ロジアナは持たない
    d = DESIGNS["fprobe_pynq_deep"]
    c = d.capabilities
    assert c.mem_bytes == 32 * 1024 * 1024 and c.la_depth == 0 and c.la_stream_events
    p = d.project()
    assert [b.name for b in p.block_designs] == ["ps7_hp_bd.tcl"]
    assert "axi_mem_port.vhd" in {s.name for s in p.sources}


def test_only_the_pynq_designs_have_the_fpga_observation_port():
    for name, d in DESIGNS.items():
        assert getattr(d.capabilities, "fpga_obs", False) == name.startswith("fprobe_pynq"), name   # 申告の無い構成もある
    for name in ("fprobe_pynq", "fprobe_pynq_deep"):
        ports = {pin.port for pin in DESIGNS[name].project().pins}
        assert {"obs_tck", "obs_tms", "obs_tdi", "obs_tdo"} <= ports
        assert {"ps_pjtag_tck", "ps_pjtag_tms", "ps_pjtag_tdi", "ps_pjtag_tdo"} <= ports
