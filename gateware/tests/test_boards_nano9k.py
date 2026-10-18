"""Tang Nano 9K のボード定義と VHDL のボード定数パッケージの一致。"""

from fprobe_gw.boards.nano9k import NANO9K
from fprobe_gw.rtl import RTL_ROOT
from tests.test_boards_primer20k import vhdl_constants


def test_vhdl_package_matches_board_definition():
    c = vhdl_constants(RTL_ROOT / "boards" / "nano9k" / "nano9k_pkg.vhd")
    pll = NANO9K.sync_pll
    assert (c["PLL_IDIV_SEL"], c["PLL_FBDIV_SEL"], c["PLL_ODIV_SEL"]) == (pll.idiv - 1, pll.fbdiv - 1, pll.odiv)
    assert c["SYS_CLK_HZ"] == pll.fout_hz and c["SERIAL_BAUD"] == NANO9K.serial_baud
    assert [c[f"DBG_{k.upper()}"] for k in NANO9K.debug_connector] == list(NANO9K.debug_connector.values())
