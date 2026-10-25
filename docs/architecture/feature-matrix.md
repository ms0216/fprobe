# 設計ごとの機能の対応表

どの設計（`DESIGNS` の項目）が、どのボード・ホストリンク・サンプルバッファ・機能を持つか。

**正はコード**: 設計の一覧と各設計の申告は `gateware/fprobe_gw/designs.py`（`DESIGNS`、`_primer_usb` / `_pynq` の引数）、
申告の項目と意味は `gateware/fprobe_gw/caps.py`（`Capabilities`）。この表は 2026-10-25 時点の `Capabilities` の値を
`DESIGNS[...].capabilities` から読み出して書いた。設計や caps を変えたら表も更新する
（値の出し方: `cd gateware && python -c "from fprobe_gw.designs import DESIGNS; print(DESIGNS['fprobe'].capabilities)"`）。
実際のデバイスはビルドに埋まったケイパビリティ ROM（TLV、`FCAP`）を PC が読んで動く（`host/fprobe/caps.py`）。

## 1. fprobe_core を含む設計（ケイパビリティを申告する）

「無」は、その設計の `Capabilities` でその機能が無い（False / 0 / 空）こと。

| 設計 | ボード | ホストリンク | 深いバッファ（外部メモリ） | 内蔵ロジアナ（BSRAM） | イベント記録の置き場 |
|---|---|---|---|---|---|
| fprobe | Tang Primer 20K Dock | USB HS（ULPI） | DDR3 128 MB | 無（`la_depth=0`） | 深いバッファ |
| fprobe_jtag | Primer | USB HS | DDR3 128 MB | 無 | 深いバッファ |
| fprobe_core_m | Primer | USB HS | DDR3 128 MB | 無 | 深いバッファ |
| fprobe_la | Primer | USB HS | DDR3 128 MB | 無 | 深いバッファ（ストリームにも流す） |
| memtest | Primer | USB HS | DDR3 128 MB（メモリ試験回路が使う） | 無（`la_channels=0` でロジアナ自体が無い） | 無（試験回路がイベント記録の代わり） |
| fprobe_serial | Primer | シリアル（BL702 の USB-UART、COBS） | 無 | 有（8192 サンプル） | 内蔵 RAM のリング（1024 件） |
| fprobe_eth | Primer | Ethernet（RTL8201F、UDP） | DDR3 128 MB | 無 | 深いバッファ |
| fprobe_nano9k | Tang Nano 9K | シリアル | 無 | 有（8192 サンプル） | 無（`event_ring_bits=0` かつ外部メモリ無し） |
| fprobe_pynq | PYNQ-Z2 | Ethernet（PS の Linux の中継、UDP） | 無 | 有（8192 サンプル） | 内蔵 RAM のリング（1024 件） |
| fprobe_pynq_deep | PYNQ-Z2 | Ethernet（同上） | PS の DDR の 32 MB（S_AXI_HP0） | 無 | 深いバッファ（ストリームにも流す） |

サンプルレート（`la_sample_hz`）= `sys_clock_hz`: Primer 100.285714 MHz、PYNQ-Z2 100 MHz、Nano 9K 54 MHz。
ロジアナの幅は全設計で `LA_WIDTH`（16）、`memtest` だけ無し。

## 2. 機能の有無

SWD は全設計の fprobe_core にある（`debug_port` / `swd_phy`）。DAP プロセッサ（ハードウェア FSM）、UART ブリッジ（CH_UART0）、
SWO の NRZ 受信（`swo_receiver`）、制御プレーン（CSR）、タイムベース、ケイパビリティ ROM も全設計にある
（`caps.channels` は全設計で CONTROL・DAP・UART0。`CH_SWO` は含まれない）。以下は設計で差がある機能。

| 設計 | JTAG | DAP サンプラ | サンプラ値一致 | 故障注入 | クロストリガ | 多段トリガ | LA グリッチフィルタ | LA パルス幅 | SWO Manchester |
|---|---|---|---|---|---|---|---|---|---|
| fprobe | 無 | 有 | 無 | 有 | 有 | 有 | 有 | 有 | 有 |
| fprobe_jtag | 有 | 無 | 無 | 無 | 有 | 有 | 有 | 有 | 有 |
| fprobe_core_m | 有 | 有 | 無 | 無 | 有 | 無 | 無 | 無 | 無 |
| fprobe_la | 無 | 無 | 無 | 無 | 有 | 有 | 有 | 有 | 有 |
| memtest | 有 | 有 | 無 | 有 | 有 | 有 | 無 | 無 | 有 |
| fprobe_serial | 有 | 有 | 無 | 有 | 有 | 有 | 有 | 有 | 有 |
| fprobe_eth | 有 | 有 | 無 | 有 | 有 | 有 | 有 | 有 | 有 |
| fprobe_nano9k | 有 | 有 | 無（注 1） | 無 | 無 | 無 | 無 | 無 | 有 |
| fprobe_pynq | 有 | 有 | 有 | 有 | 有 | 有 | 有 | 有 | 有 |
| fprobe_pynq_deep | 有 | 有 | 有 | 有 | 有 | 有 | 有 | 有 | 有 |

注 1: `fprobe_nano9k` は `sampler_match` の既定（True）のままだが、ケイパビリティに出る値は
`sampler_match and sampler and xtrig`（`caps.py` の `FEATURE_SAMPLER_MATCH`）なので、クロストリガが無い本設計では無になる。

Primer の各設計で `sampler_match` を無にしているのは、配線が混むため（`designs.py` の `_primer_usb` のコメント。
入れると 3 版とも 1200 秒で終わらなかった）。`fprobe` が JTAG を持たず `fprobe_jtag` がサンプラ・故障注入を持たないのは、
Primer に全部は入らないため構成に分けている（`designs.py` の「製品デザイン」のコメント、B22）。

| 設計 | 専用の LA 入力（`la_inputs`） | LA ストリームにイベント | 補助マイコン（aux） | FPGA 観測（fpga_obs） | 観測の値一致 | ボード間同期（sync） | 出力端子（候補数・同時動作数） | UART RTS/CTS（候補数） |
|---|---|---|---|---|---|---|---|---|
| fprobe | 無 | 無 | 有 | 無 | 無 | 無 | 有（4・2） | 有（2） |
| fprobe_jtag | 無 | 無 | 有 | 無 | 無 | 無 | 有（4・2） | 有（2） |
| fprobe_core_m | 無 | 無 | 有 | 無 | 無 | 無 | 有（4・2） | 有（2） |
| fprobe_la | 有（6 本） | 有 | 有 | 無 | 無 | 有 | 有（4・2） | 有（2） |
| memtest | 無 | 無 | 無 | 無 | 無 | 無 | 無 | 無 |
| fprobe_serial | 無 | 無 | 有 | 無 | 無 | 無 | 無 | 無 |
| fprobe_eth | 無 | 無 | 有 | 無 | 無 | 無 | 無 | 無 |
| fprobe_nano9k | 無 | 無 | 無 | 無 | 無 | 無 | 無 | 無 |
| fprobe_pynq | 有（6 本） | 無 | 無 | 有 | 有 | 無 | 有（4・2） | 有（2） |
| fprobe_pynq_deep | 有（6 本） | 有 | 無 | 有 | 有 | 有 | 有（4・2） | 有（2） |

- 「観測の値一致」（`FEATURE_OBS_MATCH`）は `fpga_obs and xtrig` で決まる（`caps.py`）。
- FPGA 観測（`fpga_obs`）を持つのは PYNQ-Z2 の 2 設計だけ。Primer 系には無い（観測用の JTAG ピン群 `obs_jtag` が
  Primer の設計の `pin_groups` に入っていない。`designs.py`）。`caps.py` の記述は「大きい FPGA の構成だけ」。
- 補助マイコン（Pico、CSR `aux`・イベント 6）は Primer の USB / Ethernet / シリアル構成にある。`fprobe_serial` と
  `fprobe_eth` は `aux=True` だが出力端子・RTS/CTS は無い（`outputs` / `uart_flow` を渡していない）。
  PYNQ-Z2・Nano 9K・memtest には無い。
- 出力端子・RTS/CTS・専用の入力の本数は、ボード定義（`fprobe_gw/boards/*.py` の `outputs()` / `uart_flow()` / `la_inputs()`）から来る。

## 3. ケイパビリティを持たない設計（fprobe_core を含まない）

`capabilities=None`。ホストの接続対象ではなく、ボードの確認・試験・対象側の回路。

| 設計 | ボード | 中身 |
|---|---|---|
| pynq_blinky | PYNQ-Z2 | LED0 を約 1 Hz で点滅（P0） |
| obs_target_primer | Primer | 「観測される対象」側。`target_ip/fprobe_obs` の回路（obs_soft_tap）とカウンタ |
| zynq_dut | PYNQ-Z2 | Primer の fprobe が外からデバッグする側（PS が AXI-Lite でサンプル生成、注目信号を PMOD JB に出す）。`ps_axi=True` |
| bringup | Primer | 基板立ち上げ（クロック・LED・UART） |
| ulpi_probe | Primer | ULPI PHY との疎通確認（レジスタ R/W を UART で報告） |
| usbtest | Primer | USB HS デバイスの実機試験（列挙・ループバック・スループット） |

## 4. 構成の選び方に関わる表（コードが正）

- `PROTOCOL_DESIGNS = {"swd": "fprobe", "jtag": "fprobe_jtag"}`: ホストが線のプロトコルに応じて書き替える Primer の設計。
- `FEATURE_DESIGNS = {"la_inputs": "fprobe_la", "la_stream_events": "fprobe_la"}`: 機能のために選ぶ設計。
  どちらも `python -m fprobe_gw gen` がホスト定数（`DESIGN_FOR_SWD` / `DESIGN_FOR_JTAG` / `DESIGN_FOR_<機能>`）に出す。
- ホスト側の解釈は `host/fprobe/caps.py`（TLV の `FEATURES` に bit が無い古いデバイスの扱いも同じファイル）。
