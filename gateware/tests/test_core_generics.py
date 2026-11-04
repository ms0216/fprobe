"""ボードのトップの fprobe_core の構成（ジェネリック）が、デザインの生成パッケージ（design_caps_pkg。ケイパビリティと同じ定義元）
から全部渡されていること。

トップが渡し忘れたジェネリックは fprobe_core の既定の値になり、ケイパビリティ（PC が見る構成）と回路が黙って食い違う。
fprobe_core にジェネリックを足したら、ここで「構成の機能（生成パッケージから渡す）」か「ボードで決まるもの」かを決める。"""

import re

from fprobe_gw.rtl import rtl

# fprobe_core のジェネリック → design_caps_pkg の定数
FROM_CAPS = {
    "CAPS": "CAPS_ROM", "MEM_ADDR_BITS": "MEM_ADDR_BITS", "MEM_TEST": "MEM_TEST", "XTRIG": "XTRIG", "TRIG_SEQ": "TRIG_SEQ",
    "SAMPLER": "SAMPLER", "SAMPLER_MATCH": "SAMPLER_MATCH", "SAMPLER_CHECK_MAX": "SAMPLER_CHECK_MAX", "HAS_JTAG": "HAS_JTAG",
    "INJECTOR": "INJECTOR", "HAS_SWO_MANCHESTER": "SWO_MANCHESTER", "UART_FLOW": "UART_FLOW", "AUX": "AUX", "SYNC": "SYNC",
    "OBS_PORT": "OBS_PORT", "XOUT_COUNT": "XOUT_COUNT", "LA_INPUT_COUNT": "LA_INPUTS", "LA_FILTER": "LA_FILTER",
    "LA_PULSE": "LA_PULSE", "LA_STREAM_EVENTS": "LA_STREAM_EVENTS", "LA_STREAM_GAPS": "LA_STREAM_GAPS",
    "EVENT_RING_BITS": "EVENT_RING_BITS", "EVENT_BYTE_PACK": "EVENT_BYTE_PACK", "CAP_RATIO": "CAP_RATIO",
    "LA_TESTGEN": "LA_TESTGEN", "LA_LANES": "LA_LANES", "LA_LANE_ADDR_BITS": "LA_LANE_ADDR_BITS",
    "LA_LANE_IN_AGE": "LA_LANE_IN_AGE",
}
# ボードで決まるもの（トップが自分で渡すか、既定の値のまま）
BOARD = {"CLK_HZ", "LA_INTERNAL", "LA_FINE_PINS", "RESET_SPLIT", "MEM_SAME_CLOCK", "LA_PIN_COUNT",
         "EVENT_COUNT_START_BELOW_4G"}
TOPS = ("designs/serial_fprobe.vhd", "boards/primer20k/primer20k_fprobe_top.vhd",
        "boards/primer20k/primer20k_fprobe_eth_top.vhd", "boards/pynq_z2/pynq_z2_fprobe_top.vhd")


def _strip_comments(text: str) -> str:
    return re.sub(r"--[^\n]*", "", text)


def core_generics() -> set[str]:
    text = _strip_comments(rtl("core/fprobe_core.vhd")[0].read_text(encoding="utf-8"))
    block = re.search(r"entity fprobe_core is\s+generic \((.*?)\);\s+port", text, re.S).group(1)
    return set(re.findall(r"^\s*(\w+)\s*:", block, re.M))


def generic_map(path) -> dict[str, str]:
    text = _strip_comments(path.read_text(encoding="utf-8"))
    block = re.search(r"entity work\.fprobe_core\s+generic map \((.*?)\)\s+port map", text, re.S).group(1)
    return {k: v.strip() for k, v in re.findall(r"(\w+)\s*=>\s*([^,]+)", block)}


def test_every_core_generic_is_classified():
    assert core_generics() == set(FROM_CAPS) | BOARD, "fprobe_core のジェネリックを FROM_CAPS か BOARD に分けること"


def test_tops_pass_every_configuration_generic_from_the_design_package():
    for top in TOPS:
        mapping = generic_map(rtl(top)[0])
        for generic, const in FROM_CAPS.items():
            assert mapping.get(generic, "").lower() == f"work.design_caps_pkg.{const}".lower(), (top, generic, mapping.get(generic))
        assert set(mapping) <= set(FROM_CAPS) | BOARD, (top, set(mapping) - set(FROM_CAPS) - BOARD)
