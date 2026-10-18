"""ケイパビリティの TLV 符号化と ROM。"""

import struct

from fprobe_gw import caps
from fprobe_gw.rtl import CORE_GENERATED, rtl
from fprobe_gw.toolchain.sim import run_cocotb
from tests.rtl import TEST_RTL

TEST_CAPS = caps.Capabilities(protocol_version=1, board="Test Board", design="unit",
                              sys_clock_hz=100_285_714, timebase_hz=100_285_714,
                              host_link=caps.LINK_USB_HS_ULPI, channels=(caps.CH_CONTROL, caps.CH_UART0),
                              aux=True, outputs=((4, False), (5, True), (6, True), (7, False)),
                              # ロジアナ: 拡張ヘッダの 8 本、内部 2 本、専用の入力 6 本（U22）
                              la_channels=16, la_depth=8192, la_sample_hz=100_285_714, la_inputs=6)


def test_encoding_layout():
    data = caps.encode(TEST_CAPS)
    assert data[:4] == b"FCAP" and struct.unpack_from("<I", data, 4)[0] == len(data)
    assert len(data) % 4 == 0
    kind, length = struct.unpack_from("<HH", data, 8)
    assert (kind, length) == (caps.TYPE_PROTOCOL_VERSION, 4)
    assert data[-4:] == bytes(4)                                   # 終端 (0, 0)
    assert b"Test Board" in data


def test_too_large_capabilities_are_rejected_at_generation():
    import dataclasses
    import pytest
    huge = dataclasses.replace(TEST_CAPS, board="x" * 5000)
    with pytest.raises(ValueError, match="収まらない"):
        caps.render_package("p", huge)


def test_rom_simulation(tmp_path):
    pkg = tmp_path / "test_caps_pkg.vhd"
    pkg.write_text(caps.render_package("test_caps_pkg", TEST_CAPS), encoding="utf-8")
    run_cocotb(toplevel="caps_rom_harness",
               sources=[*rtl("core/csr_bus_pkg.vhd"), CORE_GENERATED / "build_info_pkg.vhd", pkg, *rtl("core/caps_rom.vhd"),
                        TEST_RTL / "caps_rom_harness.vhd"],
               test_module="tests.tb.caps_rom_tb", build_dir=tmp_path / "sim")


def test_host_library_parses_what_the_gateware_encodes():
    from fprobe.caps import parse
    parsed = parse(caps.encode(TEST_CAPS))
    assert (parsed.protocol_version, parsed.board, parsed.design) == (1, "Test Board", "unit")
    assert (parsed.sys_clock_hz, parsed.timebase_hz, parsed.host_link) == (100_285_714, 100_285_714, 1)
    assert parsed.channels == {caps.CH_CONTROL, caps.CH_UART0} and parsed.unknown == {}


def test_memory_capabilities_round_trip():
    import dataclasses

    from fprobe.caps import parse
    with_test = dataclasses.replace(TEST_CAPS, mem_bytes=128 << 20, mem_test=True)
    parsed = parse(caps.encode(with_test))
    assert (parsed.mem_bytes, parsed.mem_test, parsed.unknown) == (128 << 20, True, {})
    assert parse(caps.encode(dataclasses.replace(with_test, mem_test=False))).mem_test is False
    assert "constant MEM_TEST : boolean := true;" in caps.render_package("p", with_test)


def test_features_round_trip_and_default_to_present():
    import dataclasses

    from fprobe.caps import parse
    small = dataclasses.replace(TEST_CAPS, xtrig=False, trig_seq=False, inject=False)
    parsed = parse(caps.encode(small))
    assert (parsed.xtrig, parsed.trig_seq) == (False, False)
    assert (parse(caps.encode(TEST_CAPS)).xtrig, parse(caps.encode(TEST_CAPS)).trig_seq) == (True, True)
    assert "constant XTRIG : boolean := false;" in caps.render_package("p", small)


def test_debug_port_configuration_round_trip_and_old_devices_have_everything():
    import dataclasses
    import struct

    import pytest
    from fprobe.caps import parse
    standard = dataclasses.replace(TEST_CAPS, jtag=False)
    jtag = dataclasses.replace(TEST_CAPS, sampler=False, inject=False)
    assert (parse(caps.encode(standard)).jtag, parse(caps.encode(standard)).inject) == (False, True)
    assert (parse(caps.encode(jtag)).jtag, parse(caps.encode(jtag)).inject) == (True, False)
    assert "constant HAS_JTAG : boolean := false;" in caps.render_package("p", standard)
    assert "constant INJECTOR : boolean := false;" in caps.render_package("p", jtag)
    # DEBUG の TLV が無い古いデバイス（この TLV の前のビットストリーム）は JTAG も故障注入もある
    data = caps.encode(TEST_CAPS)
    debug = struct.pack("<HHI", caps.TYPE_DEBUG, 4, caps.DEBUG_JTAG | caps.DEBUG_INJECT | caps.DEBUG_SWO_MANCHESTER)
    i = data.index(debug)
    old = data[:i] + data[i + len(debug):]
    old = old[:4] + struct.pack("<I", len(old)) + old[8:]
    assert (parse(old).jtag, parse(old).inject) == (True, True)
    assert parse(old).swo_manchester is False                      # Manchester は U51 から
    with pytest.raises(ValueError, match="クロストリガ"):
        dataclasses.replace(TEST_CAPS, xtrig=False)                 # 故障注入はクロストリガの一部


def test_swo_manchester_and_uart_flow_round_trip():
    import dataclasses

    import pytest
    from fprobe.caps import parse
    from fprobe_gw.designs import DESIGNS
    assert parse(caps.encode(TEST_CAPS)).swo_manchester is True
    assert parse(caps.encode(dataclasses.replace(TEST_CAPS, swo_manchester=False))).swo_manchester is False
    assert "constant SWO_MANCHESTER : boolean := false;" in caps.render_package(
        "p", dataclasses.replace(TEST_CAPS, swo_manchester=False))
    flow = dataclasses.replace(TEST_CAPS, uart_flow=(4, 1))
    assert parse(caps.encode(flow)).uart_flow == (4, 1)
    assert parse(caps.encode(TEST_CAPS)).uart_flow == ()                  # 候補が無い = 回路が無い
    assert "constant UART_FLOW : boolean := true;" in caps.render_package("p", flow)
    assert "constant UART_FLOW : boolean := false;" in caps.render_package("p", TEST_CAPS)
    with pytest.raises(ValueError, match="RTS/CTS"):
        dataclasses.replace(TEST_CAPS, uart_flow=(0,) * 5)
    for name in ("fprobe", "fprobe_jtag"):                                # Primer の USB の製品デザイン: 予備・TDI
        assert DESIGNS[name].capabilities.uart_flow == (4, 1), name
    for name in ("fprobe_serial", "fprobe_eth", "fprobe_nano9k", "memtest"):
        assert DESIGNS[name].capabilities.uart_flow == (), name


def test_outputs_round_trip_and_absent_means_none():
    import dataclasses

    import pytest
    from fprobe.caps import Output, parse
    parsed = parse(caps.encode(TEST_CAPS))
    assert parsed.outputs == (Output(0, 4, False), Output(1, 5, True), Output(2, 6, True), Output(3, 7, False))
    assert parsed.output_actions == 2
    one = dataclasses.replace(TEST_CAPS, output_actions=1)
    assert parse(caps.encode(one)).output_actions == 1
    assert "constant XOUT_COUNT : natural := 1;" in caps.render_package("p", one)
    none = dataclasses.replace(TEST_CAPS, outputs=())
    assert (parse(caps.encode(none)).outputs, parse(caps.encode(none)).output_actions) == ((), 0)   # 出力なし
    assert "constant XOUT_COUNT : natural := 2;" in caps.render_package("p", TEST_CAPS)
    assert "constant XOUT_COUNT : natural := 0;" in caps.render_package("p", none)
    with pytest.raises(ValueError, match="4 本まで"):
        dataclasses.replace(TEST_CAPS, outputs=((0, False),) * 5)
    with pytest.raises(ValueError, match="1〜2 本"):
        dataclasses.replace(TEST_CAPS, output_actions=3)


def test_la_filter_and_pulse_features_round_trip_and_default_to_absent():
    import dataclasses

    from fprobe.caps import parse
    parsed = parse(caps.encode(TEST_CAPS))
    assert (parsed.la_filter, parsed.la_pulse) == (True, True)
    none = dataclasses.replace(TEST_CAPS, la_filter=False, la_pulse=False)
    assert (parse(caps.encode(none)).la_filter, parse(caps.encode(none)).la_pulse) == (False, False)
    assert "constant LA_FILTER : boolean := false;" in caps.render_package("p", none)
    assert "constant LA_PULSE : boolean := true;" in caps.render_package("p", TEST_CAPS)


def test_build_info_is_a_placeholder_in_the_rom_filled_from_the_build_package():
    """U49: 版（入力の要約・ビルドの日時・git）は TLV BUILD。ROM の該当語は build_info_pkg の BUILD_INFO を参照し、
    ビルドのたびに builder が書いた build_info_pkg に差し替わる（生成物の caps は変わらない）。"""
    from fprobe.caps import Build, parse
    from fprobe_gw import build_info
    from fprobe_gw.toolchain.project import BuildInfo
    text = caps.render_package("p", TEST_CAPS)
    assert "use work.build_info_pkg.all;" in text
    assert all(f"BUILD_INFO({k})" in text for k in range(build_info.WORDS))
    assert parse(caps.encode(TEST_CAPS)).build is None                    # 既定（シミュレーション）は版なし
    info = BuildInfo(digest="0123456789abcdef" + "0" * 48, time=1_790_000_000, git="89abcdef01234567", dirty=True)
    data = caps.encode(TEST_CAPS, build_info.words(info))
    assert parse(data).build == Build(id="0123456789abcdef", time=1_790_000_000, git="89abcdef", dirty=True)
    no_git = BuildInfo(digest="f" * 64, time=5, git=None, dirty=False)
    assert parse(caps.encode(TEST_CAPS, build_info.words(no_git))).build == Build(id="f" * 16, time=5, git=None,
                                                                                    dirty=False)


def test_build_info_package_renders_the_words():
    from fprobe_gw import build_info
    from fprobe_gw.toolchain.project import BuildInfo
    info = BuildInfo(digest="0123456789abcdef" + "0" * 48, time=0x6AB0_0000, git="89abcdef", dirty=False)
    text = build_info.render_package(info)
    assert 'x"6AB00000"' in text and 'x"01234567"' in text and 'x"89ABCDEF"' in text
    assert build_info.render_package(None).count('x"00000000"') == build_info.WORDS


def test_primer_designs_declare_four_output_pins_and_two_actions():
    from fprobe_gw.designs import DESIGNS
    for name in ("fprobe", "fprobe_jtag"):
        assert DESIGNS[name].capabilities.outputs == ((4, False), (5, True), (6, True), (7, False)), name
        assert DESIGNS[name].capabilities.output_actions == 2, name
    for name in ("memtest", "fprobe_serial", "fprobe_eth", "fprobe_nano9k"):
        assert DESIGNS[name].capabilities.outputs == (), name


def test_la_layout_declares_pins_internal_probes_and_dedicated_inputs():
    """U22: ロジアナの ch の並び（TLV LA_LAYOUT）。拡張ヘッダのピン 0〜7、内部の 2 本、専用の入力。残りは無し。"""
    import dataclasses

    import pytest
    from fprobe.caps import LaChannel, parse
    parsed = parse(caps.encode(TEST_CAPS))
    assert parsed.la_layout == (*(LaChannel(i, "pin", i) for i in range(8)),
                                LaChannel(8, "internal", 0), LaChannel(9, "internal", 1),
                                *(LaChannel(10 + i, "input", i) for i in range(6)))
    assert "constant LA_INPUTS : natural := 6;" in caps.render_package("p", TEST_CAPS)
    fewer = dataclasses.replace(TEST_CAPS, la_inputs=2)
    layout = parse(caps.encode(fewer)).la_layout
    assert [c.kind for c in layout] == ["pin"] * 8 + ["internal"] * 2 + ["input"] * 2 + ["none"] * 4
    assert "constant LA_INPUTS : natural := 0;" in caps.render_package(
        "p", dataclasses.replace(TEST_CAPS, la_inputs=0))
    with pytest.raises(ValueError, match="ロジアナの幅"):
        dataclasses.replace(TEST_CAPS, la_inputs=7)                 # 8 + 2 + 7 > 16


def test_la_layout_of_old_devices_is_eight_pins_and_two_internal_probes():
    """LA_LAYOUT の無い古いデバイス（U22 の前）は ch 0〜7 = ピン、8・9 = 内部、残りは無し。"""
    import struct

    from fprobe.caps import parse
    no_inputs = caps.Capabilities(**{**TEST_CAPS.__dict__, "la_inputs": 0})
    data = caps.encode(no_inputs)
    kind = struct.pack("<H", caps.TYPE_LA_LAYOUT)
    i = next(i for i in range(8, len(data), 4) if data[i:i + 2] == kind)
    length = struct.unpack_from("<H", data, i + 2)[0]
    size = 4 + length + (-length % 4)
    old = data[:i] + data[i + size:]
    old = old[:4] + struct.pack("<I", len(old)) + old[8:]
    layout = parse(old).la_layout
    assert [c.kind for c in layout] == ["pin"] * 8 + ["internal"] * 2 + ["none"] * 6
    assert parse(old).unknown == {}


def test_the_la_design_declares_the_j14_inputs():
    """U22: J14 の専用の入力は構成 fprobe_la だけ（標準に足すと sys のタイミングを満たさない）。fprobe_la は SWD で、
    サンプラ・故障注入・JTAG を外す。ホストは機能 la_inputs の構成として知る（DESIGN_FOR_LA_INPUTS）。"""
    from fprobe import generated as g
    from fprobe.caps import parse
    from fprobe_gw.boards.primer20k import PRIMER20K
    from fprobe_gw.designs import DESIGNS, FEATURE_DESIGNS
    count = len(PRIMER20K.pins_for(("la_in",)))
    assert count == 6
    assert FEATURE_DESIGNS == {"la_inputs": "fprobe_la", "la_stream_events": "fprobe_la"}   # R1 も同じ構成
    assert g.DESIGN_FOR_LA_INPUTS == "fprobe_la" and g.DESIGN_FOR_LA_STREAM_EVENTS == "fprobe_la"
    la = DESIGNS["fprobe_la"].capabilities
    assert la.la_inputs == count and (la.jtag, la.sampler, la.inject) == (False, False, False)
    layout = parse(caps.encode(la)).la_layout
    assert [c.channel for c in layout if c.kind == "input"] == list(range(10, 16))
    assert "la_in" in DESIGNS["fprobe_la"].pin_groups
    for name in ("fprobe", "fprobe_jtag", "memtest", "fprobe_serial", "fprobe_eth", "fprobe_nano9k"):
        assert DESIGNS[name].capabilities.la_inputs == 0, name
