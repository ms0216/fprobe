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
    assert c.la_stream_ps                     # PS が DDR のリングを直接読める（中継の窓の版 2）
    assert not DESIGNS["fprobe_pynq"].capabilities.la_stream_ps
    assert c.la_pack                          # 使うチャンネルだけ詰めて流せる（深いバッファのある構成）
    assert not DESIGNS["fprobe_pynq"].capabilities.la_pack
    p = d.project()
    assert [b.name for b in p.block_designs] == ["ps7_hp_bd.tcl"]
    assert "axi_mem_port.vhd" in {s.name for s in p.sources}


def test_la_testgen_is_in_the_cap_and_deep_designs_only():
    # 試験用の方形波（ケイパビリティ la_testgen）。取り込みが速い構成と垂れ流しの構成にあり、内蔵 BRAM 版・Primer には無い
    assert {n for n, d in DESIGNS.items() if d.capabilities and d.capabilities.la_testgen} == {
        "fprobe_pynq_cap", "fprobe_pynq_deep", "fprobe_pynq_fast", "fprobe_pynq_deep_fast"}


def test_fprobe_pynq_fast_takes_n_samples_per_sys_cycle_on_the_dedicated_inputs():
    # 拡張ヘッダのピン（PMOD JA）と専用の入力（PMOD JB）を ISERDESE2（CLK = cap_clk 400 MHz の両方の辺、CLKDIV = sys）で
    # 1 周期に 8 サンプル（docs/superpowers/specs/2026-11-01-la-resolution-design.md の段 2。N = 2 × cap_ratio）
    from fprobe_gw import caps
    d = DESIGNS["fprobe_pynq_fast"]
    c = d.capabilities
    assert c.la_lanes == 8 and c.cap_ratio == 4 and c.la_fine_pins
    assert c.la_sample_hz == 800_000_000 and c.la_depth == 16384 * 8 and c.mem_bytes == 0
    assert not c.la_filter and not c.la_pulse and c.la_testgen
    assert caps.la_fine_channels(c) == 0xFCFF and caps.la_testgen_hz(c) == 400_000_000
    # 時間軸は la_cap.info.latency（細かいサンプル）で決まる。ch ごとの時刻: ピン・専用の入力 = 軸そのもの（同じ包みの同じ段数）、
    # 内部の ch（sys の信号）も la_lane_inputs が軸に揃える（ずれ 0・位相の幅 0。時刻の正しさ S1b の N2）
    from fprobe_gw.timing import la_channel_timing
    rows = la_channel_timing(c)
    assert all(r.internal == 0 for r in rows[:16]) and all(r.span == 0 for r in rows[8:10])
    p = d.project()
    assert {x.name for x in p.clocks} == {"sys_clk_in", "sys_clk", "cap_clk"}
    cap = next(x for x in p.clocks if x.name == "cap_clk")
    assert (cap.target, cap.frequency_hz) == ("clock_i/mmcm_i/CLKOUT1", 400_000_000)
    assert {"xilinx_la_iserdes.vhd", "la_lane_capture.vhd", "la_lane_front.vhd", "la_record.vhd"} <= {s.name for s in p.sources}
    pkg = caps.render_package("design_caps_pkg", c)
    assert "constant LA_LANES : positive := 8;" in pkg and "constant LA_LANE_ADDR_BITS : positive := 14;" in pkg
    assert "constant LA_FINE_PINS : boolean := true;" in pkg and "constant CAP_RATIO : natural := 4;" in pkg
    # 他の PYNQ の構成は 1 周期 1 サンプルのまま
    assert all(DESIGNS[n].capabilities.la_lanes == 1 for n in ("fprobe_pynq", "fprobe_pynq_deep", "fprobe_pynq_cap"))


def test_fprobe_pynq_deep_fast_streams_the_deep_buffer_at_n_samples_per_sys_cycle():
    # fprobe_pynq_fast と同じ端子の ISERDESE2（800 MS/s）を、深いバッファの垂れ流しに N = 8 並列で符号にする
    # （docs/superpowers/specs/2026-11-01-la-deep-fast-design.md）。内蔵バッファは無い
    from fprobe_gw import caps
    from fprobe_gw.timing import la_channel_timing
    d = DESIGNS["fprobe_pynq_deep_fast"]
    c = d.capabilities
    assert c.la_lanes == 8 and c.cap_ratio == 4 and c.la_fine_pins and c.la_sample_hz == 800_000_000
    assert c.mem_bytes == 32 * 1024 * 1024 and c.la_depth == 0 and c.la_stream_ps and c.la_pack and c.la_stream_events
    assert not c.la_filter and not c.la_pulse and caps.la_fine_channels(c) == 0xFCFF
    assert c.la_stream_bytes_per_s == 50_000_000 == DESIGNS["fprobe_pynq_deep"].capabilities.la_stream_bytes_per_s
    assert not DESIGNS["fprobe_pynq_fast"].capabilities.la_stream_bytes_per_s       # 深いバッファの無い構成は申告しない
    rows = la_channel_timing(c)
    assert all(r.deep == 0 and r.internal is None for r in rows[:16])                # 内部の ch も揃える（S1b の N2）
    p = d.project()
    assert [b.name for b in p.block_designs] == ["ps7_hp_bd.tcl"]
    assert {"xilinx_la_iserdes.vhd", "la_lane_encoder.vhd", "la_lane_raw.vhd", "la_lane_rle.vhd"} <= {s.name for s in p.sources}
    assert next(x for x in p.clocks if x.name == "cap_clk").frequency_hz == 400_000_000
    pkg = caps.render_package("design_caps_pkg", c)
    assert "constant LA_LANES : positive := 8;" in pkg and "constant MEM_ADDR_BITS : natural := 21;" in pkg


def test_fprobe_pynq_cap_samples_the_logic_analyzer_at_the_cap_clock():
    # 内蔵ロジアナの取り込みを、sys（100 MHz）の 3 倍の cap_clk（300 MHz。MMCM の 2 本目の出力。VCO 1200 MHz / 4）で動かす
    from fprobe_gw.boards.pynq_z2 import PYNQ_Z2
    d = DESIGNS["fprobe_pynq_cap"]
    c = d.capabilities
    assert c.cap_ratio == 3 and c.sys_clock_hz == 100_000_000 and c.timebase_hz == 100_000_000
    assert c.la_sample_hz == 300_000_000 == c.sys_clock_hz * c.cap_ratio and c.la_depth > 0 and c.mem_bytes == 0
    p = d.project()
    assert {x.name for x in p.clocks} == {"sys_clk_in", "sys_clk", "cap_clk"}
    cap = next(x for x in p.clocks if x.name == "cap_clk")
    assert (cap.kind, cap.target, cap.frequency_hz) == ("pin", "clock_i/mmcm_i/CLKOUT1", 300_000_000)
    assert PYNQ_Z2.sync_pll.cap_div(c.cap_ratio) == 4 and not c.la_fine_pins
    # 他の構成は取り込みのクロックを使わない（sys と同じ）
    assert all(DESIGNS[n].capabilities.cap_ratio == 1 for n in ("fprobe_pynq", "fprobe_pynq_deep"))
    assert "cap_clk" not in {x.name for x in DESIGNS["fprobe_pynq"].project().clocks}


def test_the_mmcm_cap_output_must_be_an_integer_multiple_of_sys():
    import pytest
    from fprobe_gw.toolchain.vivado import MmcmSolution
    m = MmcmSolution(fin_mhz=125.0, mult=48.0, divclk=5, out_div=12.0)
    assert (m.fout_hz, m.cap_div(3), m.cap_hz(3), m.cap_div(4), m.cap_hz(4)) == (100_000_000, 4, 300_000_000, 3, 400_000_000)
    with pytest.raises(ValueError):
        m.cap_div(5)                                    # 12 が 5 で割り切れない
    with pytest.raises(ValueError):
        m.cap_div(6)                                    # 600 MHz は BUFG の上限 464 MHz を超える
    with pytest.raises(ValueError):
        MmcmSolution(fin_mhz=125.0, mult=36.0, divclk=5, out_div=9.0).cap_div(2)   # 450 MHz は sys の 4.5 倍
    with pytest.raises(ValueError):
        MmcmSolution(fin_mhz=125.0, mult=50.0, divclk=5, out_div=12.0)            # VCO 1250 MHz は範囲外


def test_only_the_pynq_designs_have_the_fpga_observation_port():
    for name, d in DESIGNS.items():
        assert getattr(d.capabilities, "fpga_obs", False) == name.startswith("fprobe_pynq"), name   # 申告の無い構成もある
    for name in ("fprobe_pynq", "fprobe_pynq_deep", "fprobe_pynq_cap", "fprobe_pynq_fast", "fprobe_pynq_deep_fast"):
        ports = {pin.port for pin in DESIGNS[name].project().pins}
        assert {"obs_tck", "obs_tms", "obs_tdi", "obs_tdo"} <= ports
        assert {"ps_pjtag_tck", "ps_pjtag_tms", "ps_pjtag_tdi", "ps_pjtag_tdo"} <= ports


def test_zynq_dut_is_a_vivado_project_without_the_relay():
    # Primer の fprobe が外からデバッグする側の回路（docs/superpowers/specs/2026-10-24-zynq-dut-environment-design.md）
    d = DESIGNS["zynq_dut"]
    assert d.capabilities is None and d.ps_axi
    assert not any(other.ps_axi for name, other in DESIGNS.items() if name != "zynq_dut")
    p = d.project()
    assert isinstance(p, VivadoProject) and p.top == "pynq_z2_dut_top"
    assert [b.name for b in p.block_designs] == ["ps7_bd.tcl"]
    names = {s.name for s in p.sources}
    assert {"dut_stream_core.vhd", "dut_pins.vhd", "dut_axil_slave.vhd", "dut_axil_core.vhd", "pynq_z2_dut_top.vhd",
            "xilinx_mmcm.vhd", "reset_sync.vhd"} <= names
    assert not names & {"zynq_frame_link.vhd", "fprobe_core.vhd"}
    ports = {pin.port for pin in p.pins}
    assert {f"dut_dbg[{i}]" for i in range(6)} <= ports
    assert {f"rpi_pin[{i}]" for i in range(16)} <= ports
    assert {"ps_pjtag_tck", "ps_pjtag_tms", "ps_pjtag_tdi", "ps_pjtag_tdo", "sys_clk_in"} <= ports
    assert not any(port.startswith(("pmod1", "la_in")) for port in ports)      # JA は持たない（キーの線を動かさない）
