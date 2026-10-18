"""Sipeed Tang Primer 20K Dock（rev 3714）。詳細は docs/boards/tang-primer-20k-dock.md。

ピンの出典は Tang_Primer_20K_Dock_3714_Schematics.pdf。デザインは使う資源名（clk27, led, uart, ulpi）を
選び、.cst はビルド時に生成される。RTL 側の定数（gateware/rtl/boards/primer20k/primer20k_pkg.vhd）は
ここと一致している必要があり、tests/test_boards_primer20k.py が検査する。
"""

from ..toolchain.pll import PLL_CONSTRAINTS, solve_pll
from ..toolchain.programmer import JtagIdentity
from ..toolchain.project import GowinIp, Pin
from . import Board, Ddr3, Ethernet, UlpiTiming

__all__ = ["PRIMER20K"]


def _bus(name: str, locations: str, **attrs) -> tuple[Pin, ...]:
    return tuple(Pin(f"{name}[{i}]", loc, **attrs) for i, loc in enumerate(locations.split()))


# DDR3（コア基板の x16 1 個、1.5V バンク）。出典: Sipeed TangPrimer-20K-example の DDR-test の ddr3.cst。
# 差動の ddr_ck / ddr_dqs は正側に "P,N" で制約する（負側のポート ddr_ck_n / ddr_dqs_n は制約しない）
_SSTL = dict(io_type="SSTL15", pull="NONE", drive=8, bank_vccio="1.5")
_SSTL_D = dict(io_type="SSTL15D", pull="NONE", drive=8, bank_vccio="1.5")
_DDR3_PINS = (
    *_bus("ddr_addr", "F7 A4 D6 F8 C4 E6 B1 D8 A5 F9 K3 B7 A3 C8", **_SSTL),
    *_bus("ddr_ba", "H4 D3 H5", **_SSTL),
    Pin("ddr_cs_n", "P5", **_SSTL), Pin("ddr_ras_n", "R4", **_SSTL), Pin("ddr_cas_n", "R6", **_SSTL),
    Pin("ddr_we_n", "L2", **_SSTL), Pin("ddr_cke", "J2", **_SSTL), Pin("ddr_odt", "R3", **_SSTL),
    Pin("ddr_reset_n", "B9", **_SSTL),
    Pin("ddr_ck", "J1,J3", **_SSTL_D),
    *_bus("ddr_dm", "G1 K5", **_SSTL),
    *_bus("ddr_dq", "G5 F5 F4 F3 E2 C1 E1 B3 M3 K4 N2 L1 P4 H3 R1 M2", **_SSTL),
    Pin("ddr_dqs[0]", "G2,G3", **_SSTL_D), Pin("ddr_dqs[1]", "J5,K6", **_SSTL_D),
)


PRIMER20K = Board(
    name="Tang Primer 20K Dock",
    part="GW2A-LV18PG256C8/I7",
    family="GW2A-18C",
    revision=3714,
    jtag_identity=JtagIdentity(
        board="Tang Primer 20K Dock", usb_id=0x04036010, usb_serial="FACTORYAIOT_PRO",
        ftdi_description="USB Debugger A",
        # 既定の 2.5 MHz は、中断された書き込みの後に応答しなくなったことがある（2026-10-16）。1 MHz は安定
        jtag_frequency="1MHz",
        gowin_cable="USB Debugger A", gowin_device="GW2A-18C", idcode=0x0000081B),
    pin_groups={
        "ddr3": _DDR3_PINS,
        # 27 MHz 発振器
        "clk27": (Pin("clk27", "H11", pull="UP"),),
        # LED（アクティブロー）。led[0]/led[1] は DONE/READY 兼用ピン（options で GPIO 化）
        "led": _bus("led", "C13 A13 N16 N14 L14 L16"),
        # BL702 の仮想 COM（方向は FPGA から見たもの）
        "uart": (Pin("uart_tx", "M11"), Pin("uart_rx", "T13", pull="UP")),
        "uart_tx": (Pin("uart_tx", "M11"),),       # 送信だけ使うデザイン用（診断報告など）
        # 拡張ヘッダ J12（DVP と共用）の信号ピン 5..12 → pmod1[0..7]（docs/boards/tang-primer-20k-dock.md）
        # ターゲットへの配線（ジャンパ線）は長く、隣の SWCLK の変化を拾いやすいので入力にヒステリシスを付ける。
        # [4]（予備）と [7]（UART TX）はプル無し: クロストリガの出力（output_pins）を、動いていない間に本当の
        # 高インピーダンスにする（ターゲットの内部プルダウンの入力を動かせる）。[4] には普段の機能が無く、[7] は普段
        # いつも FPGA が駆動するので失うものが無い。[5]（UART RX）・[6]（SWO / TDO）は、つないでいないときに浮いて
        # ゴミを受けないようプルアップのまま（docs/superpowers/specs/2026-10-18-u41-xtrig-pin-output-design.md §3.6）
        "pmod1": tuple(Pin(f"pmod1[{i}]", loc, pull="NONE" if i in (4, 7) else "UP", drive=4, hysteresis="HIGH")
                       for i, loc in enumerate("J14 J16 M14 M15 T12 R11 T11 P11".split())),
        # USB3317（USB-OTG 側, ULPI）。ulpi_rst（F10）は RTL8201F の RESET と共用
        "ulpi": (
            Pin("ulpi_clk", "T15", pull="NONE"),
            Pin("ulpi_dir", "K12", pull="NONE"),
            Pin("ulpi_nxt", "K13", pull="NONE"),
            Pin("ulpi_stp", "K11", pull="NONE", drive=8),
            Pin("ulpi_rst", "F10", pull="NONE", drive=8),
            *_bus("ulpi_data", "G11 H12 J12 H13 T14 R13 P13 R12", pull="NONE", drive=8),
        ),
        # 補助マイコン（Raspberry Pi Pico）の UART: Pico の GP0（TX）→ J14（pmod0）の 5 番（P9）。
        # J14 の 6 番（T9）はオンボードの WS2812 と共用なので避ける
        "aux": (Pin("aux_rx", "P9", pull="UP"),),
        # ロジアナの専用の入力 in0〜in5（U22）: J14（pmod0）の 7〜12 番。デバッグの線と兼用しない入力だけのピン。
        # 5 番（P9）は補助 Pico の UART、6 番（T9）はオンボードの WS2812 と共用なので使わない。
        # プルダウン: つないでいない入力を 0 に止める（浮いた入力の変化で深いバッファの RLE を無駄にしない）。
        # プルアップと違い、電源の無いターゲットに電流を流し込まない。長いジャンパ線に備えてヒステリシスを付ける。
        # T7 は大域クロック入力（GCLKT_3）だが、ここではただの入力（ETM のトレース受信 U34 は別の構成で使う案）
        "la_in": _bus("la_in", "T8 P8 R8 T7 T6 P6", pull="DOWN", hysteresis="HIGH"),
        # RTL8201F（RMII、PHY アドレス 1）。REF_CLK（50 MHz）は PHY の TXC から入る（CLK_CTL の strap = 出力）。
        # eth_rst（F10）は USB3317 の RESET と共用。MDC/MDIO（F14/F16）は v1 では使わない（既定の設定で 100M 全二重）。
        # 出典: Dock 3714 の回路図 P005、Sipeed の Ethernet 例の .cst、LiteX の Dock 定義（3 つで一致）
        "eth": (
            Pin("eth_ref_clk", "A9", pull="NONE"),
            Pin("eth_crs_dv", "M6", pull="NONE"),
            *_bus("eth_rxd", "F15 C9", pull="NONE"),
            Pin("eth_tx_en", "E16", pull="NONE", drive=8),
            *_bus("eth_txd", "D16 E14", pull="NONE", drive=8),
            Pin("eth_rst", "F10", pull="NONE", drive=8),
        ),
    },
    input_clock_port="clk27",
    input_clock_hz=27_000_000,
    sync_pll=solve_pll(27.0, 100.0, PLL_CONSTRAINTS["GW2A-18C"]),
    # デバッグコネクタ J12（pmod1）の割り当て（docs/boards/tang-primer-20k-dock.md）
    debug_connector={"swclk": 0, "tdi": 1, "nreset": 2, "swdio": 3, "spare": 4, "uart_rx": 5, "swo": 6,
                     "uart_tx": 7},
    # クロストリガの出力の端子の候補 0〜3（ターゲットの線に使っていない 4 本。primer20k_pkg の XOUT_PIN_CHOICES と一致）
    output_pins=("spare", "uart_rx", "swo", "uart_tx"),
    # UART の RTS/CTS の端子の候補 0・1（primer20k_pkg の UART_FLOW_PIN_CHOICES と一致）。J12 の 8 本のうち UART の線
    # でなく、普段 SWD の作業で空いているもの: 予備（プル無し → RTS に向く。使わない間は高インピーダンス）と TDI
    # （JTAG のときだけ使う。プルアップ → CTS に向く: つないでいなければ「出ていない」）。SWO の端子は SWO と取り合う
    # ので候補にしない（docs/superpowers/specs/2026-10-19-u51-u52-swo-uart-design.md §4.2）
    uart_flow_pins=("spare", "tdi"),
    # USB3317 データシート（ULPI 出力クロックモード）
    ulpi_timing=UlpiTiming(output_min_ns=1.5, output_max_ns=3.5, setup_ns=5.0, hold_ns=0.0),
    # ピンの I/O ブロック（IOT14/IOT18 = 上辺の列 14/18、IOR11/IOR20 = 右辺の行 11/20、IOT38）の隣。
    # 行 10/19 は BSRAM の行なので避ける（2026-10-16 の配置結果から。配線 0.5〜0.8 ns の見込み）
    ulpi_output_regions={"data_q_0": "R[2:4]C[13:15]", "data_q_1": "R[2:4]C[13:15]",
                         "data_q_2": "R[2:4]C[17:19]", "data_q_3": "R[2:4]C[17:19]",
                         "data_q_4": "R[20:22]C[51:54]", "data_q_5": "R[20:22]C[51:54]",
                         "data_q_6": "R[11:13]C[51:54]", "data_q_7": "R[11:13]C[51:54]",
                         "stp_q": "R[2:4]C[37:39]"},
    # DDR3-800（メモリクロック 398.25 MHz、ユーザー側 1:4 = 99.56 MHz、128 ビット = BL8 1 回）、1 Gb x16 = 128 MB
    # （行 13 ビット。メモリ試験で 2026-10-16 に確認: 行の 14 ビット目は折り返す）。
    # コントローラは Gowin IDE 同梱の Sipeed の参考デザイン（Cam2HDMI）の生成済み DDR3 IP（V1.9.8 で生成）。
    # V1.9.11 の生成器で同じ設定から作った IP（ddr3_1_4code_hs）はこの基板で較正が終わらなかった（2026-10-16）
    ddr3=Ddr3(memory_pll=solve_pll(27.0, 400.0, PLL_CONSTRAINTS["GW2A-18C"]), clock_ratio=4,
              size_bytes=128 * 1024 * 1024,
              controller=GowinIp(module="DDR3MI", kind="ddr3", config=(
                  ("CLK_Ratio", "1:4"), ("Memory_Clock", "400"), ("Dram_Width", "16"), ("Row_Address", "14"),
                  ("Column_Address", "10"), ("CAS_Latency", "5"), ("CW_Latency", "5")),
                  prebuilt="IDE/bin/Documents/Cam2HDMI/OV5640_HDMI720P_DDR3/src/ddr3_memory_interface/DDR3MI.v")),
    # SerialLink（M11 の移植性の確認用）: BL702 の USB-UART（PC からは COM ポート）
    serial_baud=2_000_000,
    # EthernetLink の既定のアドレス（MAC はローカル管理。PC は同じサブネット 192.168.77.0/24 に置く）
    eth=Ethernet(mac=0x02_66_70_72_00_01, ip=(192, 168, 77, 2), udp_port=63920),
    # LED に使う DONE/READY ピンを汎用 I/O として解放する
    options=("-use_done_as_gpio 1 -use_ready_as_gpio 1",),
)
