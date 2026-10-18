import re

import pytest

from fprobe_gw.boards.primer20k import PRIMER20K
from fprobe_gw.rtl import RTL_ROOT
from fprobe_gw.toolchain.pll import PLL_CONSTRAINTS, solve_pll

PKG = RTL_ROOT / "boards" / "primer20k" / "primer20k_pkg.vhd"


def vhdl_constants(path):
    text = path.read_text(encoding="utf-8")
    return {name.upper(): int(value.replace("_", ""))
            for name, value in re.findall(
                r"constant\s+(\w+)\s*:\s*(?:natural|positive|integer)\s*:=\s*([\d_]+)\s*;",
                text, re.I)}


def test_board_pll_matches_solver():
    expected = solve_pll(27.0, 100.0, PLL_CONSTRAINTS["GW2A-18C"])
    assert PRIMER20K.sync_pll == expected


def test_vhdl_package_matches_board_definition():
    c = vhdl_constants(PKG)
    pll = PRIMER20K.sync_pll
    assert c["CLK27_HZ"] == 27_000_000
    assert (c["PLL_IDIV_SEL"], c["PLL_FBDIV_SEL"], c["PLL_ODIV_SEL"]) == (
        pll.idiv_sel, pll.fbdiv_sel, pll.odiv_sel)
    assert c["SYS_CLK_HZ"] == pll.fout_hz
    assert c["SERIAL_BAUD"] == PRIMER20K.serial_baud
    assert c["ETH_UDP_PORT"] == PRIMER20K.eth.udp_port
    text = PKG.read_text(encoding="utf-8")
    assert int(re.search(r'ETH_MAC\s*:.*:= x"(\w+)"', text)[1], 16) == PRIMER20K.eth.mac
    assert int(re.search(r'ETH_IP\s*:.*:= x"(\w+)"', text)[1], 16) == PRIMER20K.eth.ip_value
    mem = PRIMER20K.ddr3.memory_pll
    assert (c["MEM_PLL_IDIV_SEL"], c["MEM_PLL_FBDIV_SEL"], c["MEM_PLL_ODIV_SEL"]) == (
        mem.idiv_sel, mem.fbdiv_sel, mem.odiv_sel)
    assert c["BOARD_REVISION"] == PRIMER20K.revision
    for name, index in PRIMER20K.debug_connector.items():
        assert c[f"DBG_{name.upper()}"] == index, name
    assert c["LA_INPUT_PINS"] == PRIMER20K.la_inputs()
    pins = re.search(r"constant\s+XOUT_PIN_CHOICES\s*:\s*\w+\s*:=\s*\(([\d,\s]+)\)", text)[1]
    assert [int(v) for v in pins.split(",")] == [PRIMER20K.debug_connector[n] for n in PRIMER20K.output_pins]


def test_uart_flow_pins_match_rtl():
    text = PKG.read_text(encoding="utf-8")
    pins = re.search(r"constant\s+UART_FLOW_PIN_CHOICES\s*:\s*\w+\s*:=\s*\(([\d,\s]+)\)", text)[1]
    assert [int(v) for v in pins.split(",")] == list(PRIMER20K.uart_flow()) == [4, 1]


def test_output_pins_without_pull_up_are_the_spare_and_uart_tx():
    # 仕様 §3.6: 出力の候補のうち [4]（予備）・[7]（UART TX）はプル無し、[5]（UART RX）・[6]（SWO）はプルアップ
    assert PRIMER20K.outputs() == ((4, False), (5, True), (6, True), (7, False))
    pulls = {p.port: p.pull for p in PRIMER20K.pins_for(("pmod1",))}
    assert [pulls[f"pmod1[{i}]"] for i in range(8)] == ["UP"] * 4 + ["NONE", "UP", "UP", "NONE"]


def test_pin_groups():
    def locations(*groups):
        return {p.port: p.location for p in PRIMER20K.pins_for(groups)}

    assert locations("clk27") == {"clk27": "H11"}
    assert locations("led")["led[0]"] == "C13" and locations("led")["led[5]"] == "L16"
    assert locations("uart") == {"uart_tx": "M11", "uart_rx": "T13"}
    ulpi = locations("ulpi")
    assert ulpi["ulpi_clk"] == "T15" and ulpi["ulpi_rst"] == "F10"
    assert [ulpi[f"ulpi_data[{i}]"] for i in range(8)] ==         ["G11", "H12", "J12", "H13", "T14", "R13", "P13", "R12"]


def test_ethernet_pins():
    eth = {p.port: p.location for p in PRIMER20K.pins_for(("eth",))}
    assert eth == {"eth_ref_clk": "A9", "eth_crs_dv": "M6", "eth_rxd[0]": "F15", "eth_rxd[1]": "C9",
                   "eth_tx_en": "E16", "eth_txd[0]": "D16", "eth_txd[1]": "E14", "eth_rst": "F10"}


def test_unknown_pin_group_is_rejected():
    with pytest.raises(KeyError, match="nope"):
        PRIMER20K.pins_for(("nope",))


def test_clock_constraints_offered_by_the_board():
    clk27 = PRIMER20K.input_clock()
    assert (clk27.name, clk27.kind, clk27.frequency_hz) == ("clk27", "port", 27_000_000)
    sys = PRIMER20K.sys_clock()
    assert (sys.name, sys.kind, sys.frequency_hz) == ("sys_clk", "net", PRIMER20K.sync_pll.fout_hz)
    ulpi = PRIMER20K.ulpi_clock()
    assert (ulpi.target, ulpi.kind, ulpi.frequency_hz) == ("ulpi_clk", "port", 60_000_000)


def test_jtag_identity():
    assert PRIMER20K.jtag_identity.ftdi_description == "USB Debugger A"
    assert PRIMER20K.jtag_identity.usb_serial == "FACTORYAIOT_PRO"
    assert PRIMER20K.jtag_identity.idcode == 0x0000081B


def test_la_inputs_are_the_free_j14_pins_pulled_down():
    """U22: J14 の 7〜12 番（5 番 = 補助 Pico の UART、6 番 = WS2812 と共用を除く）。入力だけ、プルダウン。"""
    pins = PRIMER20K.pins_for(("la_in",))
    assert [p.location for p in pins] == ["T8", "P8", "R8", "T7", "T6", "P6"]
    assert {(p.pull, p.hysteresis, p.drive) for p in pins} == {("DOWN", "HIGH", None)}
    used = {p.location for g in PRIMER20K.pin_groups if g != "la_in" for p in PRIMER20K.pin_groups[g]}
    assert not used & {p.location for p in pins}
