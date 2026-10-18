"""Sipeed Tang Nano 9K（GW1NR-LV9QN88PC6/I5）。簡易版（ホストリンク = BL702 の USB-UART）の移植先候補。

まだどこからも import しない（RTL のトップ・定数パッケージ・テストは未作成）。

出典:
- [SCH] 回路図 Tang_nano_9K_3674_schematics.pdf
  https://dl.sipeed.com/fileList/TANG/Nano%209K/2_Schematic/Tang_nano_9K_3674_schematics.pdf
  （ネット名 PIN<番号>_<I/O 名>_<用途>。電源ネット VCCO0_1_2_3V3 / VCCO3_1V8）
- [EX] Sipeed 公式サンプルの .cst / .gprj https://github.com/sipeed/TangNano-9K-example
  （led/src/9K_LED_project.cst, uart/src/top.cst, uart/uart.gprj, lcd_led/src/Tang_nano_9K_LCD.cst,
  picotiny/project/picotiny.cst, picotiny/gowin_ip/gowin_rpll/gowin_rpll.v）
- [APIC] https://github.com/YosysHQ/apicula examples/tangnano9k.cst（バンク 3 のピンを LVCMOS18 で制約）
- [WIKI] https://wiki.sipeed.com/hardware/en/tang/Tang-Nano-9K/Nano-9K.html

I/O バンクの電圧（[SCH]）: バンク 0/1/2 は 3.3V（VCCO0_1_2_3V3）、バンク 3 は 1.8V（VCCO3_1V8）。
1.8V なのは 3（S1）, 4（S2）, 10〜16（LED）と 79〜88（ヘッダ J6 の 2〜9 番を含む。ネット名 *_1V8）。
デバッグコネクタはバンク 2（下辺 IOB）の 3.3V ピンに置く。1.8V のピンをターゲットにつながないこと。
"""

from ..toolchain.pll import PLL_CONSTRAINTS, solve_pll
from ..toolchain.programmer import JtagIdentity
from ..toolchain.project import Pin
from . import Board

__all__ = ["NANO9K"]


def _bus(name: str, locations: str, **attrs) -> tuple[Pin, ...]:
    return tuple(Pin(f"{name}[{i}]", loc, **attrs) for i, loc in enumerate(locations.split()))



# デバッグコネクタ用の入出力の属性（Primer 20K の pmod1 と同じ考え方: 長いジャンパ線の入力にヒステリシス）
_DEBUG_IO = dict(pull="UP", drive=4, hysteresis="HIGH")


NANO9K = Board(
    name="Tang Nano 9K",
    # [EX] uart/uart.gprj: <Device name="GW1NR-9C" pn="GW1NR-LV9QN88PC6/I5">（.cst のヘッダも同じ）
    part="GW1NR-LV9QN88PC6/I5",
    family="GW1NR-9C",
    revision=3674,                           # 回路図の版（[SCH] のファイル名）
    jtag_identity=JtagIdentity(
        board="Tang Nano 9K", usb_id=0x04036010,
        # TODO: 実機の BL702 の USB シリアル番号を OS の機器一覧で確認して入れる（未確認）。
        # Primer 20K と同じ "FACTORYAIOT_PRO" の可能性があり、その場合はシリアルで区別できないので
        # 書き込みツールの識別方法を見直す必要がある。照合が絶対に一致しない値にしておく
        usb_serial="UNVERIFIED-TANG-NANO-9K",
        ftdi_description="USB Debugger A",
        jtag_frequency="1MHz",
        # IDCODE: openFPGALoader src/part.hpp {0x0100481b, "GW1N(R)-9C"}
        gowin_cable="USB Debugger A", gowin_device="GW1NR-9C", idcode=0x0100481B),
    pin_groups={
        # 27 MHz 発振器（[SCH] PIN52_XTAL_IN = IOR17A/GCLKT_3、[EX] 全サンプルで 52）
        "clk27": (Pin("clk27", "52", pull="UP"),),
        # LED1〜6（[SCH] 陽極が VCCO3_1V8 側 → アクティブロー。[EX] led.cst / [APIC] は LVCMOS18）
        "led": _bus("led", "10 11 13 14 15 16", io_type="LVCMOS18", drive=8),
        # BL702 の仮想 COM（方向は FPGA から見たもの）。[SCH] PIN17_IOB2A_FPGA_TX → BL_UART1_RX、
        # PIN18_IOB2B_FPGA_RX ← BL_UART1_TX。[EX] uart/src/top.cst: uart_tx 17, uart_rx 18（LVCMOS33）
        "uart": (Pin("uart_tx", "17"), Pin("uart_rx", "18", pull="UP")),
        "uart_tx": (Pin("uart_tx", "17"),),
        # ボタン S1（[SCH] PIN3_IOT2A_BUTTON_S1_1V8。押すと Low）。
        # S2 は 4 番（IOL5A/JTAGSEL_N、1.8V）。サンプルはリセットに使うが、汎用 I/O にするには
        # JTAGSEL_N の兼用ピン設定が要る可能性があり未確認なのでここには入れない
        "button": (Pin("button", "3", io_type="LVCMOS18", pull="UP"),),
        # デバッグコネクタ: ヘッダ J5 の 5〜12 番（隣接 8 本、全てバンク 2 = 3.3V）→ pmod1[0..7]。
        #   J5-5..10 = 25 26 27 28 29 30（PIN25_IOB8A〜PIN30_IOB13B、基板上で未使用）
        #   J5-11, 12 = 33 34（PIN33_IOB23A_RGB_DE / PIN34_IOB23B_RGB_VS。RGB LCD の FPC と共用 →
        #   LCD を挿さないこと）
        # J5-1..4 は TF カード（10k プルアップあり）、J5-13 以降は RGB LCD と共用なので避けた
        "pmod1": _bus("pmod1", "25 26 27 28 29 30 33 34", **_DEBUG_IO),
    },
    input_clock_port="clk27",
    input_clock_hz=27_000_000,
    # 54 MHz（IDIV 1, FBDIV 2, ODIV 8: PFD 27 MHz, VCO 432 MHz）。100 MHz は 27 MHz から整数比で作れず
    # （最良 100.29 MHz で PFD 3.86 MHz・VCO が下限際）、GW1NR-9 C6 の論理は GW2A C8 より遅いので控えめにする。
    # 2 Mbaud の分周比も 27 で割り切れる。タイミング余裕が分かれば 72 MHz（PFD 9, VCO 576）・81 MHz も候補
    sync_pll=solve_pll(27.0, 54.0, PLL_CONSTRAINTS["GW1NR-9C"]),
    # デバッグコネクタ（pmod1）の割り当て（Primer 20K と同じ並び）
    debug_connector={"swclk": 0, "tdi": 1, "nreset": 2, "swdio": 3, "spare": 4, "uart_rx": 5, "swo": 6,
                     "uart_tx": 7},
    # SerialLink: BL702 の USB-UART。上限は公式資料に記載が無く未確認（Primer 20K の BL702 では 2 Mbaud で動作）
    serial_baud=2_000_000,
)
