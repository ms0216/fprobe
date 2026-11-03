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
# 出力の駆動は 16 mA・SLEW は FAST（端を速く）。ジャンパ線で Raspberry Pi 2B の JTAG を動かすと、端が遅いほど転送が化ける
# （TDO が全部 0、ときに DP の CTRL/STAT が 0 に書かれる = 対象が別の書き込みを受けた）。4 MHz の 1 万回の失敗: 8 mA SLOW 約 2700・
# 12 mA SLOW 33〜52・16 mA SLOW 19〜29・16 mA FAST 3〜5（2026-11-02、Pi の TRST はプルアップ。docs/boards/pynq-z2.md）
_BOARD_PULL = ("DOWN", "DOWN", "UP", "UP", "DOWN", "DOWN", "UP", "UP")
_JA = "Y18 Y19 Y16 Y17 U18 U19 W18 W19"

# 対象の FPGA の観測の JTAG（Raspberry Pi のヘッダ。docs/superpowers/specs/2026-10-20-fpga-observation-design.md）。
# 出典: Xilinx/PYNQ v2.7.0 の boards/Pynq-Z2/base/vivado/constraints/base.xdc の rpi_gpio_tri_io[0〜19]
# = F19 V10 V8 W10 B20 W8 V6 Y6 B19 U7 C20 Y8 A20 Y9 U8 W6 Y7 F20 W9 V7（Raspberry Pi の GPIO8〜27。GPIO2〜7 と
# ID_SD/ID_SC は PMOD JA と共有）。GPIO22〜25 = ヘッダの 15・16・18・22 番（GND は 20 番）を使う。
# 基板の抵抗は未確認（PMOD と違い制約ファイルにプルの指定が無い）。FPGA のプルは JTAG の慣例（TCK は下げ、他は上げ）
_OBS_JTAG = (("obs_tck", "U8", "DOWN"), ("obs_tms", "W6", "UP"), ("obs_tdi", "Y7", "UP"), ("obs_tdo", "F20", "UP"))
# PS の DAP の PJTAG（EMIO）を出す端子（docs/superpowers/specs/2026-10-23-zynq-pjtag-design.md）: Raspberry Pi のヘッダの
# GPIO11 = 23 番（TCK）・GPIO8 = 24 番（TMS）・GPIO10 = 19 番（TDI）・GPIO9 = 21 番（TDO）、GND は 25 番。出典は上と同じ。
# DAP がこの線につながるのは independent の起動（MIO2 = 1、JTAG_JP1）のときだけ。cascade では PS は入力を見ない
_PS_PJTAG = (("ps_pjtag_tck", "W10", "DOWN"), ("ps_pjtag_tms", "F19", "UP"), ("ps_pjtag_tdi", "V8", "UP"))

PYNQ_Z2 = Board(
    name="PYNQ-Z2",
    part="xc7z020clg400-1",
    family="XC7Z020",
    revision=1,
    jtag_identity=None,
    pin_groups={
        "clk125": (Pin("sys_clk_in", "H16"),),
        "led": _bus("led", "R14 P14 N16 M14"),
        "pmod1": tuple(Pin(f"pmod1[{i}]", loc, pull=_BOARD_PULL[i], drive=16, slew="FAST") for i, loc in enumerate(_JA.split())),
        # ロジアナの専用の入力（PMOD JB の 1〜4・7〜8 = PMOD の 0〜5 番）。プルは JA と同じく基板に合わせる
        # （つないでいない入力は 0・1 番・4・5 番が 0、2・3 番が 1）
        "la_in": tuple(Pin(f"la_in[{i}]", loc, pull=_BOARD_PULL[i]) for i, loc in enumerate("W14 Y14 T11 T10 V16 W16".split())),
        # ボードの間の時刻の同期: Raspberry Pi のヘッダの GPIO16（36 番、B19）= 親から、GPIO20（38 番、A20）= 子へ
        # （出典は上の _OBS_JTAG と同じ base.xdc の rpi_gpio_tri_io[8]・[12]）
        "sync": (Pin("sync_in", "B19", pull="DOWN"), Pin("sync_out", "A20", drive=8)),
        # 外部ピン入力の限界を測る試験用の方形波（RPi ヘッダの GPIO12 = 32 番 = ピン B20。上の GPIO8〜27 の並びの 5 番目。下の dut_rpi の先頭と同じ）。
        # 150 MHz まで toggle するので SLEW は FAST（既定の SLOW では振幅が出ない）。有効にしたとき以外は Z
        "testgen": (Pin("testgen_pin", "B20", drive=8, slew="FAST"),),
        "obs_jtag": tuple(Pin(port, loc, pull=pull, drive=8) if port != "obs_tdo" else Pin(port, loc, pull=pull)
                          for port, loc, pull in _OBS_JTAG),
        # Zynq DUT 回路の注目信号（PMOD JB の 1〜4・7〜8 番 = la_in と同じ 6 つの位置。DUT 回路が出力する。プルは引き下げ）
        "dut_dbg": tuple(Pin(f"dut_dbg[{i}]", loc, pull="DOWN", drive=8)
                         for i, loc in enumerate("W14 Y14 T11 T10 V16 W16".split())),
        # Zynq DUT のピン診断（RPi ヘッダの GPIO12〜27 の 16 本を、PS から出力・入力に切り替えて読み戻す）。添字 i = GPIO(12 + i) =
        # ヘッダ 32・33・8・10・36・11・12・35・38・40・15・16・18・22・37・13 番（出典は上の base.xdc）。GPIO8〜11（ヘッダ 24・21・19・23 番）は
        # PS の PJTAG の素通しが使うので入れない。プルは付けない（診断の間、端子が浮いているかを読み戻しで見る）
        "dut_rpi": tuple(Pin(f"rpi_pin[{i}]", loc, drive=8) for i, loc in enumerate("B20 W8 V6 Y6 B19 U7 C20 Y8 A20 Y9 U8 W6 Y7 F20 W9 V7".split())),
        "ps_pjtag": (*(Pin(port, loc, pull=pull) for port, loc, pull in _PS_PJTAG), Pin("ps_pjtag_tdo", "V10", drive=8)),
    },
    input_clock_port="sys_clk_in",
    input_clock_hz=125_000_000,
    # VCO 125 / 5 × 48 = 1200 MHz（-1 の VCO の上限）→ sys 100 MHz（÷ 12）。取り込みのクロック cap_clk（CLKOUT1）は構成が
    # sys の整数倍を選ぶ: 比 2 = 200 MHz（÷ 6）、3 = 300 MHz（÷ 4。fprobe_pynq_cap）、4 = 400 MHz（÷ 3。fprobe_pynq_fast の
    # ISERDESE2 の CLK）。12 の約数が多いので、sys と整数比で BUFG の上限 464 MHz 以下の速いクロックの組が最も多く作れる
    # （docs/superpowers/specs/2026-11-01-la-resolution-design.md §3.2 の案 B・§11。900 MHz の版は 400 MHz と 200 MHz を作れなかった）
    sync_pll=MmcmSolution(fin_mhz=125.0, mult=48.0, divclk=5, out_div=12.0),
    sys_clock_pin="clock_i/mmcm_i/CLKOUT0",
    cap_clock_pin="clock_i/mmcm_i/CLKOUT1",
    debug_connector={"swclk": 0, "tdi": 1, "nreset": 2, "swdio": 3, "spare": 4, "uart_rx": 5, "swo": 6,
                     "uart_tx": 7},
    output_pins=("spare", "uart_rx", "swo", "uart_tx"),
    uart_flow_pins=("spare", "tdi"),
    # PS の Linux の中継の宛先（PC の USB の LAN に直結。PYNQ の eth0:2）
    eth=Ethernet(mac=0, ip=(192, 168, 77, 2), udp_port=63920),
    toolchain="vivado",
)
