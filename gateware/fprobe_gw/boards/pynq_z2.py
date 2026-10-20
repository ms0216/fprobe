"""TUL PYNQ-Z2（Zynq-7020, xc7z020clg400-1）。fprobe の移植先（docs/superpowers/specs/2026-10-19-pynq-z2-port-design.md）。

ピンの出典: TUL の PYNQ-Z2 v1.0 の master XDC（PMOD JA/JB・sysclk H16・LED）。JA の SWCLK（Y18）・SWDIO（Y17）は
2026-10-20 に XIAO nRF52840 と SWD で DPIDR 0x2BA01477 を読んで確かめた。
PL の書き込みは JTAG（Vivado の hw_server。シリアル 1234-TUL・IDCODE を照合）で行い、PS と PL の間のレベルシフタを
SSH で有効にする（fprobe_gw/toolchain/pynq_loader.py）。PC とのリンクは PS の Linux の中継（UDP 63920、Ethernet 版と同じ形式）。
"""

from ..toolchain.project import Pin
from ..toolchain.vivado import MmcmSolution
from . import Board, Ethernet

__all__ = ["PYNQ_Z2"]


def _bus(name: str, locations: str, **attrs) -> tuple[Pin, ...]:
    return tuple(Pin(f"{name}[{i}]", loc, **attrs) for i, loc in enumerate(locations.split()))


# デバッグの線（PMOD JA）。番号は Primer の J12（pmod1）と同じ並び（= ロジアナの ch 0〜7）。
# 基板に引き上げ・引き下げの抵抗がある（PYNQ の pmod の資料: PMOD の 0・1・4・5 番 = 引き下げ、2・3・6・7 番 = 引き上げ。
# 2026-10-20 に実機で確認: JA10 = W19 は FPGA の弱い引き下げに勝って High に戻る）。FPGA の端子のプルは基板と同じ向きに
# 揃える（ケイパビリティ OUTPUTS のプルアップの有無もこれで正しくなる）。
# キーの行のように「浮いたら困る・High で押される」線は、引き下げのある予備 [4]（U18）と UART RX [5]（U19）につなぐ。
# 引き上げのある [7]（W19）に XIAO の D6 をつないだら、走査の間に High に保たれてキーが押され続けた（2026-10-20）
_BOARD_PULL = ("DOWN", "DOWN", "UP", "UP", "DOWN", "DOWN", "UP", "UP")
_JA = "Y18 Y19 Y16 Y17 U18 U19 W18 W19"

PYNQ_Z2 = Board(
    name="PYNQ-Z2",
    part="xc7z020clg400-1",
    family="XC7Z020",
    revision=1,
    jtag_identity=None,
    pin_groups={
        "clk125": (Pin("sys_clk_in", "H16"),),
        "led": _bus("led", "R14 P14 N16 M14"),
        "pmod1": tuple(Pin(f"pmod1[{i}]", loc, pull=_BOARD_PULL[i], drive=4) for i, loc in enumerate(_JA.split())),
        # ロジアナの専用の入力（PMOD JB の 1〜4・7〜8 = PMOD の 0〜5 番）。プルは JA と同じく基板に合わせる
        # （つないでいない入力は 0・1 番・4・5 番が 0、2・3 番が 1）
        "la_in": tuple(Pin(f"la_in[{i}]", loc, pull=_BOARD_PULL[i]) for i, loc in enumerate("W14 Y14 T11 T10 V16 W16".split())),
    },
    input_clock_port="sys_clk_in",
    input_clock_hz=125_000_000,
    sync_pll=MmcmSolution(fin_mhz=125.0, mult=8.0, divclk=1, out_div=10.0),     # VCO 1000 MHz → 100 MHz
    sys_clock_pin="clock_i/mmcm_i/CLKOUT0",
    debug_connector={"swclk": 0, "tdi": 1, "nreset": 2, "swdio": 3, "spare": 4, "uart_rx": 5, "swo": 6,
                     "uart_tx": 7},
    output_pins=("spare", "uart_rx", "swo", "uart_tx"),
    uart_flow_pins=("spare", "tdi"),
    # PS の Linux の中継の宛先（PC の USB の LAN に直結。PYNQ の eth0:2）
    eth=Ethernet(mac=0, ip=(192, 168, 77, 2), udp_port=63920),
    toolchain="vivado",
)
