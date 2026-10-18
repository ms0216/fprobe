# Tang Primer 20K Dock — ボードメモ

出典: Sipeed `Tang_Primer_20K_Dock_3714_Schematics.pdf`（rev 3714, 2026-05-20）。
実装は `gateware/fprobe_gw/platform/primer20k.py`。

## FPGA とクロック

| 項目 | 値 |
|---|---|
| デバイス | GW2A-LV18PG256C8/I7（ファミリ GW2A-18C, IDCODE `0x0000081B`） |
| 発振器 | 27 MHz → `H11` |
| `sync` ドメイン | rPLL 27 × 26 / 7 = **100.2857 MHz**（VCO 802.3 MHz, PFD 3.857 MHz）。2026-10-16 に一時 90 MHz に下げたが、dap_processor の分割で戻した |

## USB ポート

| 基板表記 | チップ | FPGA との接続 | 本プロジェクトでの用途 |
|---|---|---|---|
| USB-JTAG（手前右） | BL702（FT2232 互換, Full-Speed） | JTAG + UART（FPGA TX=`M11`, RX=`T13`） | ビットストリーム書き込み・デバッグコンソール |
| USB-OTG（手前左） | USB3317（ULPI, High-Speed） | ULPI（下表） | fprobe の主ホストリンク |

- 両ポートとも VBUS は基板の +5V に直結（ダイオードなし）。USB-OTG だけで給電可能。
- USB-OTG 上の DEVICE/HOST スライドスイッチは **DEVICE** にする（USB_ID を決める）。

ULPI: clk=`T15`（60 MHz）, dir=`K12`, nxt=`K13`, stp=`K11`, rst=`F10`,
data[0..7]=`G11 H12 J12 H13 T14 R13 P13 R12`。

**`F10` は USB3317 と RTL8201F（Ethernet PHY）のリセット共用**。片方だけをリセットすることはできない。

## 拡張ヘッダ（2×6, 3.3V）

各ヘッダの 1..4 番は 3.3V/GND。fprobe では 5..12 番をコネクタピン 1..8 として扱う。

| コネクタ | 基板 / 用途名 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12 |
|---|---|---|---|---|---|---|---|---|---|
| `pmod0` | J14（MIC ARRAY） | P9 | T9 ※ | T8 | P8 | R8 | T7 | T6 | P6 |
| `pmod1` | J12（DVP） | J14 | J16 | M14 | M15 | T12 | R11 | T11 | P11 |
| `pmod2` | J6（RGB LCD B） | B12 | C12 | B13 | A14 | B14 | A15 | D14 | E15 |
| `pmod3` | J5（RGB LCD R/G） | L9 | N8 | N9 | N7 | N6 | D11 | A11 | B11 |

※ `T9` はオンボード WS2812 の DIN と共用（100Ω 経由）。高速信号には使わない。
各ヘッダの信号は対応する FPC コネクタ（カメラ・LCD・マイク）にもつながっているため、
それらのモジュールは外しておくこと。

## fprobe の J14（`pmod0`）: 補助 Pico とロジアナの専用の入力（U22）

| J14 | FPGA ピン | 信号 | ロジアナの ch |
|---|---|---|---|
| 5 | `P9` | 補助 Pico の UART（`aux_rx`） | — |
| 6 | `T9` | 使わない（WS2812 と共用） | — |
| 7 | `T8` | 専用の入力 in0 | 10 |
| 8 | `P8` | 専用の入力 in1 | 11 |
| 9 | `R8` | 専用の入力 in2 | 12 |
| 10 | `T7` | 専用の入力 in3（GCLKT_3） | 13 |
| 11 | `T6` | 専用の入力 in4 | 14 |
| 12 | `P6` | 専用の入力 in5 | 15 |

専用の入力は構成 `fprobe_la` だけが使う（標準に入れるとタイミングを満たさない）。
入力だけ（FPGA は駆動しない）・プルダウン・ヒステリシス付き・**3.3 V まで**（5 V の信号は入れない）。
ch の並びはケイパビリティ LA_LAYOUT で申告する（`docs/superpowers/specs/2026-10-19-u22-la-inputs-design.md`）。

## fprobe のデバッグコネクタ（J12 = `pmod1`）

M4 で決めた割り当て（2026-10-16、Nucleo F401RE を配線して `fprobe.wiring` で自動判定した結果を正とした）。
ゲートウェアでは `primer20k_pkg` の `DBG_*` 定数がこの番号を持つ。

| pmod1 | FPGA ピン | 信号 | Nucleo F401RE の接続先 |
|---|---|---|---|
| 0 | `J14` | SWCLK / TCK | CN7-15（PA14） |
| 1 | `J16` | TDI | CN7-17（PA15） |
| 2 | `M14` | nRESET（オープンドレイン） | CN7-14（NRST） |
| 3 | `M15` | SWDIO / TMS | CN7-13（PA13） |
| 4 | `T12` | 予備（ロジアナ・汎用入出力） | CN10-11（PA5 = LD2。2026-10-16 追加、ターゲットの GPIO を見る試験用） |
| 5 | `R11` | UART RX（ターゲットの TX） | CN10-21（PA9） |
| 6 | `T11` | SWO / TDO | CN10-31（PB3） |
| 7 | `P11` | UART TX（ターゲットの RX） | CN10-33（PA10） |

GND は両基板が同じ PC に USB でつながっているため共通（専用の GND 線なし）。Nucleo の CN2 ジャンパは外す（ST-LINK を切り離す）。

信号品質: SWCLK 25 MHz ではジャンパ線の反射・クロストークでターゲット側の SWCLK が二重に数えられることがある（読みが 1 ビットずれる）。J12 の出力は 4 mA・入力はヒステリシス付きにして防いでいる（docs/superpowers/specs/2026-10-16-swd-25mhz-design.md）。**J12 の GND とターゲットの GND を信号線の隣に 1 本つなぐ**と帰路が短くなり、さらに余裕が増す（推奨）。

## LED・ボタン

| 資源 | ピン | 極性・電圧 |
|---|---|---|
| LED0..5 | `C13`(DONE), `A13`(READY), `N16`, `N14`, `L14`, `L16` | アクティブロー, 3.3V。DONE/READY の GPIO 化に `-use_done_as_gpio 1 -use_ready_as_gpio 1` が必要 |
| Key_0 | `T10` | アクティブロー, 3.3V |
| Key_1..3 | `T3`, `T2`, `D7` | アクティブロー, **1.5V バンク（LVCMOS15）** |
| Key_4 / DIP SW0 | rev 3714 で入れ替え（`B10`=RECONFIG と `C7`） | **使用しない**（リビジョン差と RECONFIG ピンの危険） |
