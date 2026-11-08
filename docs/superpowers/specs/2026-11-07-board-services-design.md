# ボードサービスと宣言からの生成（新しいボードを宣言 + 薄い top だけで足せる構造）設計

日付: 2026-11-07 / 状態: 設計案（利用者の確認待ち。**今回はコードを変えない。仕様書と棚卸しだけ**）

方針は利用者が承認済み（fprobe_core は PYNQ-Z2 と Primer で共通、機能は generic、ケイパビリティで申告）。
この書は、残る 4 つの問題を、実コードの事実から棚卸しし、直す構造と移行の段階を決める。
事実はファイルと行で引く（行は 2026-11-07 時点、ブランチ `worktree-agent-a3e85dae57ce26d58`）。
**確かめていないことは「未確認」と書き、確かめる実験を §9 に並べる。** 推測を事実のように書かない。

## 0. 問題と、この書の範囲

| # | 問題 | この書での扱い |
|---|---|---|
| 1 | ボード固有部品（メモリの口・ホストリンク・LA 入力段・クロック）と core の境が、名前付きの口として固定されていない | §2 で口ごとに「2 実装以上で同じ約束にできるか」を事実で調べ、§3 で固定する口を決める |
| 2 | `gateware/fprobe_gw/designs.py` がデザインごとにソース・ピン・generic・ケイパビリティを手書きで重複している | §1.4 に重複を一覧、§4 の宣言から生成する |
| 3 | 機能が要るボード資源（800 MS/s は ISERDES、トークン形式は 1 周期 1 サンプル）の宣言が無い | §4.3 の機能レジストリ（要る資源・両立しない組み合わせ・理由）。足りなければ生成時に理由つきで断る |
| 4 | 面積に収める判断が人の経験 | §6。見積もらない。ビルドの事実（LUT・WNS・EX1998）で確かめ、足りなければ失敗する |

範囲外（今回は触れない）: RTL・`designs.py` など既存コードの変更、実機接続、push。別の agent が `gateware/rtl/la/la_tok_encoder.vhd` 等を変更中のため、
**この worktree にはそのファイルが無く（`ls gateware/rtl/la | grep tok` が空）、読んでいない。** トークン形式についての記述は、依頼文の
「トークン形式は 1 周期 1 サンプル」を前提にした宣言の例にとどめ、その中身は**未確認**。

設計原則との関係:
- ヒューリスティック禁止: 生成時の判定は「宣言された事実の充足」だけ（要る資源が有る / 無い）。「入りそうだから残す」「余裕があるから外す」は入れない。
  面積・時間はビルドの測定結果で確かめる。閾値が要るときは、使う側が引数に明示して渡す（既定の暗黙の値にしない）。
- 実装が 1 つしかない口を増やさない（§3.3）。
- 画面・API・MCP への影響: 無い（ゲートウェアの構成の話）。ただし宣言から出す「ボード × 構成 × 機能 × 資源」の表は、ケイパビリティ TLV と
  同じ定義元から生成し、`docs/` に置く（人も AI も同じ表を見る）。

## 1. 実コードの棚卸し (a)

### 1.1 fprobe_core の generic（`gateware/rtl/core/fprobe_core.vhd` 行 31〜91）の分類

`gateware/tests/test_core_generics.py` が既に「構成の機能 = `design_caps_pkg` から全部渡す（`FROM_CAPS`）」と「ボードで決まるもの（`BOARD`）」に
二分し、4 つのトップ（`serial_fprobe.vhd`・`primer20k_fprobe_top.vhd`・`primer20k_fprobe_eth_top.vhd`・`pynq_z2_fprobe_top.vhd`）が全部渡していることを検査している。
**つまり generic とケイパビリティの一致は、既に 1 つの定義元（`caps.Capabilities` → `designs/generated/<design>_caps_pkg.vhd`）から機械的に出ている。**
残る問題は「トップごとに 28 行の generic map を写している」ことと「ボードで決まる 7 個が手書き」なこと。

| generic | 行 | 分類 | 値の出どころ（現状） |
|---|---|---|---|
| CAPS | 32 | 構成（生成） | `caps.render_package` の CAPS_ROM |
| CLK_HZ | 33 | **ボード固有** | トップが `SYS_CLK_HZ`（ボードのパッケージ `primer20k_pkg`/`pynq_z2_pkg`/`nano9k_pkg`）を渡す。Python 側の `Capabilities.sys_clock_hz`（= `Board.sync_pll.fout_hz`）と二重（`tests/test_boards_*.py` が一致を検査） |
| LA_PIN_COUNT | 34 | ボード固有（トップは渡さない。既定 8） | `caps.la_pins`（既定 8）と **generic の既定 8 が別々**。トップが渡さないので、`la_pins` を変えても回路に伝わらない（手同期の穴） |
| LA_INPUT_COUNT | 35 | 構成（生成） | `LA_INPUTS` ← `Capabilities.la_inputs` ← `Board.la_inputs()`（= 資源 `la_in` のピン数） |
| MEM_ADDR_BITS | 37 | 構成（生成） | `_mem_addr_bits(mem_bytes)`（`caps.py` 行 359） |
| LA_INTERNAL | 38 | **ボード固有（実際は構成から決まる）** | トップが直接書く: PYNQ は `not DEEP`（行 299）、Primer USB/eth は `false`、serial は `true`。全デザインで `LA_INTERNAL = (mem_bytes = 0)` が成り立つ（memtest も外部メモリありで false）が、どこにも宣言も検査も無い |
| MEM_TEST | 39 | 構成（生成） | `Capabilities.mem_test` |
| XTRIG / TRIG_SEQ | 42〜43 | 構成（生成） | |
| AUX / SAMPLER / SAMPLER_MATCH / OBS_PORT / SYNC | 45〜49 | 構成（生成） | SAMPLER_MATCH は `caps.sampler_match and caps.sampler and caps.xtrig`（`caps.py` 行 404）。同じ式が FEATURES の bit10 にも（行 338）。二箇所 |
| HAS_JTAG / HAS_SWO_MANCHESTER / UART_FLOW / XOUT_COUNT | 51〜54 | 構成（生成） | |
| LA_FILTER / LA_PULSE / LA_STREAM_EVENTS / LA_STREAM_GAPS | 57〜62 | 構成（生成） | |
| EVENT_RING_BITS | 64 | 構成（生成） | |
| EVENT_COUNT_START_BELOW_4G | 66 | 試験用（トップは渡さない） | |
| MEM_SAME_CLOCK | 67 | **ボード固有** | PYNQ のトップだけ `true`（行 299）。実体は「`mem_clk` を `sys_clk` につないでいる」（行 ~ `mem_clk => sys_clk`）ことと同じ事実で、二箇所に書いてある |
| RESET_SPLIT | 70 | **ボード固有（タイミング調整）** | PYNQ だけ `true`（行 299）。最悪経路が reset の扇出だった（`docs/reports/fprobe-core-notes.md` §2）。Primer に入れたときの面積・タイミングは**未確認** |
| INJECTOR | 71 | 構成（生成） | |
| CAP_RATIO | 73 | 構成（生成）+ ボード固有の制約 | 比は構成が選ぶが、使えるかは MMCM の分周（`pynq_z2_pkg.mmcm_cap_div`、割り切れない比は合成で止める）。Python 側の `PYNQ_Z2_CAP_RATIO`（`designs.py` 行 132）・`Board.cap_clock(ratio)` と VHDL が二重 |
| LA_TESTGEN | 75 | 構成（生成） | |
| LA_LANES / LA_LANE_ADDR_BITS / LA_LANE_IN_AGE | 81〜83 | 構成（生成）+ ボード固有の事実 | `IN_AGE` は ISERDES の特性。Python の `pynq_z2_iserdes_newest_age`（`designs.py` 行 136）と VHDL の `iserdes_newest_age` が二重（`pynq_z2_fprobe_top.vhd` 行 139 の assert と cocotb の `xilinx_la_iserdes_tb` で検査） |
| LA_FINE_PINS | 86 | 構成（生成）だがトップが再計算 | caps に `LA_FINE_PINS` が出ているのに、PYNQ のトップは `FINE_PINS := LA_FINE_PINS and LANES > 1` を自分で作って渡す（行 299）。`test_core_generics.py` はこれを「ボードで決まるもの」に入れている |
| EVENT_BYTE_PACK / SAMPLER_CHECK_MAX | 87, 90 | 構成（生成） | |

### 1.2 fprobe_core のポート（行 92〜223）の分類

| 群 | ポート | 分類 | メモ |
|---|---|---|---|
| クロック・リセット | clk, cap_clk, rst（93〜95） | ボード固有（供給側） | cap_clk は CAP_RATIO ≥ 2 のときだけ（PYNQ の MMCM 2 本目の出力） |
| ホストリンク（sys ドメインの流れ） | ctrl_in/out（96〜104）、packet_size・dap_in/out（105〜114）、swo_out・swo_flush（130〜136）、uart_baud/uart_baud_load・uart_in/out（137〜145）、uart_host_rts/break・uart_cts_n 他（156〜164）、stream_out（199〜201） | **ホストリンク口（§2.2）** | すべて `<prefix>_valid/ready/data(/last)` の流れ。クロックまたぎはアダプタ側で済んでいる |
| ホストリンクの副路 | ps_bus_*（204〜208） | PYNQ だけ | `zynq_frame_link` の窓（`ps_window`）と `la_ps_port` を直結 |
| デバッグポート（ターゲットの線） | swclk/swdio/tdi/tdo/nreset/swo/uart_rx/tx（116〜129, 146〜150） | ボード固有（端子の段） | 端子の段の口（§2.4） |
| UART の制御線・クロストリガ出力・gpio | uart_rts/cts_claim・pin（159〜164）、xout_*（182〜185）、gpio_*（176〜178） | 端子の段 + 端子の割り当て（`pin_select`・`debug_pin_func`、既に board 非依存の entity） | |
| ロジアナ入力 | la_pins（166）、la_inputs（168）、la_input_lanes（171）、la_pin_lanes（174） | **LA 入力段口（§2.3）** | |
| 外部メモリ | mem_clk, mem_rst, mem_ready, mem_cmd_*, mem_rd_*, mem_wr_pending（187〜197） | **メモリ口（§2.1）** | |
| 補助の端子 | aux_rx（210）、obs_*（212〜216）、sync_in/out（218〜219）、testgen_out/oe（221〜222） | ボードの資源（ピン）。アダプタは無く、ピンの有無だけが条件 | |

### 1.3 各トップが何を配線しているか

凡例: ○ = 配線あり、− = なし、値は generic/定数。行はそのファイル内。

| 項目 | PYNQ-Z2 `pynq_z2_fprobe_top.vhd`（401 行） | Primer USB `primer20k_fprobe_top.vhd`（286 行） | Primer eth `primer20k_fprobe_eth_top.vhd`（200 行） | serial 共通 `designs/serial_fprobe.vhd`（134 行）+ Primer `primer20k_fprobe_serial_top`（36 行）/ Nano 9K `nano9k_fprobe_top`（35 行） |
|---|---|---|---|---|
| クロック | `xilinx_mmcm`（行 128、125 → sys 100 MHz、CAP_DIV で cap_clk）、`reset_sync` | `gowin_rpll`（122、27 → 100.286 MHz）、ulpi_clk（usb_clk）、`reset_sync` ×2 | `gowin_rpll`（77）、eth_ref_clk、`reset_sync` ×2（link_rst 別） | `gowin_rpll` は各ボードの top、`serial_fprobe` が `reset_sync` |
| ホストリンク | `zynq_frame_link`（246）+ `frame_mux`（261）+ `ps7_bd_wrapper`/`ps7_hp_bd_wrapper`（AXI-Lite） | `usb_device`（131）+ `usb_core_cdc`（155） | `eth_link`（87、中に `frame_mux`+`udp_stack`） | `serial_link`（58、中に `frame_mux`） |
| packet_size | `MAX_PAYLOAD` = 512（定数） | `usb_core_cdc` が速度で選ぶ（信号） | 定数 512 | 定数 512 |
| swo_flush | open | ○（ss_flush → usb） | −（未接続） | − |
| uart_host_rts/break/cts | `'0'` / `'0'` / open | ○（COM ポートの RTS・ブレーク、CTS → DSR） | − | − |
| メモリ | `axi_mem_port`（230）+ HP0（深い構成のみ）、MEM_SAME_CLOCK、`mem_base`/`mem_errors` は `zynq_frame_link` と往復 | `primer20k_ddr3`（191）+ メモリ用 rPLL、mem_clk ≠ sys | 同左（108） | 無し（MEM_ADDR_BITS = 0） |
| LA 入力 | `xilinx_la_iserdes`（145、LANES > 1 のとき）、それ以外は端子直結。JA の入力は ISERDES の組み合わせ出力 `ja_in` | 端子直結（la_pins = pmod1、la_inputs = la_in） | 端子直結（la_inputs 無し） | 端子直結 |
| 端子の出力段 | `debug_pin_func` → `pin_select` ×2（前半用）→ `xilinx_oddr` → IOB の T FF → `pmod1`（行 348〜385）。出力は +1 周期 | `debug_pin_func` → `pin_select` → 三状態（255〜271）。組み合わせ | `debug_pin_func` → gpio > func > Z（164〜）。`pin_select` 無し | `debug_pin_func` → 同左（114〜） |
| 補助 | obs JTAG・sync・testgen・ps_pjtag の端子 | aux_rx・la_in・sync | aux_rx | aux_rx（Primer の serial のみ） |
| コアの generic | `design_caps_pkg` から 28 + CLK_HZ・LA_INTERNAL・LA_FINE_PINS・RESET_SPLIT・MEM_SAME_CLOCK（行 280〜299） | 28 + CLK_HZ・LA_INTERNAL（201〜219） | 28 + CLK_HZ・LA_INTERNAL（118〜136） | 28 + CLK_HZ・LA_INTERNAL（73〜90） |

事実として読み取れること:
- **コア側の 28 行の generic map と、sys ドメインのホストリンクの port map（約 35 本）が 4 つのトップに写してある**（`test_core_generics.py` が generic 側だけ検査する）。
- 端子の出力段は 3 通り（Primer USB = `pin_select` 付き三状態、Primer eth/serial/Nano = 簡易の優先、PYNQ = ODDR + IOB FF）。eth/serial/Nano は **ケイパビリティに出力の端子・RTS/CTS が無い** ので `pin_select` が要らない。
- Nano 9K は専用のトップ 1 つが `serial_fprobe` を呼ぶだけの薄い構造（35 行）。**これが目指す形の先例**（`serial_fprobe.vhd` 先頭「ボードのトップはクロック（PLL）とピンをつなぐだけ」）。

### 1.4 `designs.py` の重複と、手で同期している値

重複（`gateware/fprobe_gw/designs.py`）:

1. `Capabilities(...)` に `protocol_version`・`board`・`design`・`sys_clock_hz`・`timebase_hz`・`channels=(CONTROL, DAP, UART0)` が fprobe 系の `Capabilities(...)` の手組み（`_primer_usb`・`_pynq`・memtest・serial・eth・nano9k の 6 箇所、行 107〜117, 187〜201, 286〜305, 307〜324, 325〜347, 351〜366）に毎回書かれる。`la_sample_hz` も Primer 系では sys と同じ値を毎回書く。
2. `sampler_match=False` を `_primer_usb` の `setdefault`（行 96）と memtest（302）・serial（319）・eth（344）が **別々に** 持つ。理由の文（配線が混んで 3 版とも 1200 秒で終わらなかった）が 3 箇所にコピーされている。面積のための外し方が「各デザインの引数」に散在し、一覧できない。
3. Primer の DDR3 を使う 3 件（`_primer_usb`・memtest・eth）で、`ips=(PRIMER20K.ddr3.controller,)`・`mem_clocks()`・`mem_bytes=PRIMER20K.ddr3.size_bytes`・`pin_groups` の `"ddr3"`・ソースの `primer20k_ddr3.vhd` が組で要るのに、毎回手で並べる（行 98〜117, 286〜305, 325〜347）。メモリ口の定義が無いので、組のうち 1 つを忘れてもビルドまで分からない。
4. `cdc_max_delay_ns=8.0`・`options=("-place_option 1 -route_option 1",)` の写し（Primer 5 件）。
5. ソース一覧: `_primer_usb`・memtest の `rtl(*_COMMON, *_PRIMER20K, *_USB, ...)` がほぼ同一の写し。依存順は手で並べ、`dict.fromkeys` で重複除去（行 100〜104）。`tests/test_design_elaboration.py` が全デザインの GHDL 展開で順序の誤りを見つける。
6. 端子の群の名前（`pin_groups`）とトップ entity のポート名は、`Pin.port` の文字列規約（`"pmod1[0]"` 等）で結ぶ。向き（in/out/inout）は `Pin` に無く、トップのポート宣言（手書き）にだけある。

手で同期している値（誰が検査しているかも書く）:

| # | 値 | 片方 | もう片方 | 検査 |
|---|---|---|---|---|
| S1 | fprobe_core の generic ↔ ケイパビリティ | `Capabilities` | トップの generic map | 生成パッケージ + `test_core_generics.py`（**解決済みに近い**。残りは 4 トップへの写し） |
| S2 | ボード定数 | `boards/*.py` の PLL・デバッグコネクタ・`output_pins`・`uart_flow_pins`・`la_in` | `rtl/boards/*/\*_pkg.vhd` の `SYS_CLK_HZ`・`DBG_*`・`XOUT_PIN_CHOICES`・`UART_FLOW_PIN_CHOICES`・`LA_INPUT_PINS`・PLL/MMCM 定数 | `tests/test_boards_{primer20k,pynq_z2,nano9k}.py`（値を VHDL から正規表現で読む） |
| S3 | ISERDES の遅れ | `designs.pynq_z2_iserdes_newest_age` | `pynq_z2_pkg.iserdes_newest_age` | トップの assert（行 139）+ `xilinx_la_iserdes_tb` |
| S4 | LANES = 2 × CAP_RATIO | `Capabilities.__post_init__`（la_sample_hz の式） | トップの assert（行 135） | 合成の elaboration |
| S5 | cap_clk の比 | `PYNQ_Z2_CAP_RATIO`・`Board.cap_clock(ratio)`（制約） | `pynq_z2_pkg.mmcm_cap_div`（MMCM の分周） | `tests/test_designs_pynq.py::test_the_mmcm_cap_output_must_be_an_integer_multiple_of_sys` |
| S6 | 出力の FF の段数 | `timing.BOARD_OUTPUT_TICKS = {"PYNQ-Z2": 1}`（**ボード名の文字列がキー**、行 108） | `pynq_z2_fprobe_top.vhd` の ODDR + IOB FF（コメントで対応） | **機械的な検査なし**（コメントだけ） |
| S7 | メモリ番地の幅 | `_mem_addr_bits(mem_bytes)` | Primer のトップの `mem_cmd_addr : unsigned(22 downto 0)`（行 54/eth 同）と `primer20k_ddr3` の `cmd_addr 22 downto 0` | 型の不一致なら elaboration で失敗するが、`mem_bytes` を変えて 23 以外にしても型の幅で止まるだけ（意図の検査ではない） |
| S8 | PYNQ の深いバッファ領域 | `PYNQ_Z2_DEEP_BYTES` = 32 MiB（行 120） | `host/pynq/uEnv.txt` の `mem=479M`、トップの `MEM_SPAN`、ブロック設計 `ps7_hp_bd.tcl` | **機械的な検査なし**（コメントだけ。ホストの設定ファイルをまたぐ） |
| S9 | ホストリンクの種類 | `Capabilities.host_link`（`LINK_USB_HS_ULPI`/`LINK_ETH`/`LINK_SERIAL`） | ソース一覧とトップが実際に使うリンク entity | **検査なし**。PYNQ は `LINK_ETH` と申告するが実体は `zynq_frame_link`（PS の中継越しの UDP）で、申告は「線上の形式が Ethernet 版と同じ」という意味 |
| S10 | FEATURES の bit ↔ 回路 | `caps.py` 行 324〜339 | 回路の条件 | **`FEATURE_WATCH_START`（`caps.py` 行 339）に対応する VHDL の generic が無い**（`rg -i watch_start gateware/rtl --glob '*.vhd'` が 0 件）。申告は `sampler_match and sampler and xtrig and inject` の論理積だが、回路が同じ条件で始動の列を持つかは**未確認**（CSR 側 `inject.start` の有効化条件を読む必要がある） |
| S11 | 機能が要る資源 | 例: `la_stream_ps` は PS の直接読み | `zynq_frame_link` の `ps_bus_*` と `axi_mem_port.wr_pending` | `Capabilities.__post_init__` は「外部メモリとストリームのイベント」までしか見ない（行 203〜208）。**リンクの種類を見ない**ので、Primer に `la_stream_ps=True` を書いてもケイパビリティは通り、回路に窓が無いまま PC に「できる」と申告する（現状は `_pynq` が深い構成にだけ書いているので顕在化していない） |
| S12 | DDR3 の物理 | `PRIMER20K.ddr3`（サイズ・PLL）+ `ips` + `mem_clocks()` | `primer20k_ddr3.vhd`・`gowin_rpll` の定数 `MEM_PLL_*` | `tests/test_boards_primer20k.py`（PLL の定数）。IP のネットリストは Gowin 同梱で GHDL に模型が無い |
| S13 | ピン群 ↔ トップのポート | `Design.pin_groups` | トップ entity のポートの名前・幅 | ビルドの制約適用でしか分からない（未接続の制約は警告か失敗か**未確認**） |

## 2. 口の候補ごとの検討 (b)：2 実装以上を同じ約束にできるか

判定の基準は、CLAUDE.md の「2 ボード以上で共通なものだけ固定」に従い、**現に実装が 2 つ以上あり、約束を 1 つの文で言えるか**。

### 2.1 メモリ口（16 バイト拍の読み書き）

**実装は 2 つ**: `rtl/mem/axi_mem_port.vhd`（PYNQ。AXI3 64 ビットのバースト 2 拍）と `rtl/boards/primer20k/primer20k_ddr3.vhd`（Gowin DDR3 IP）。
どちらも、コア側の約束は `rtl/mem/mem_test.vhd` 先頭に書いてある同じ形（1 コマンド = 128 ビットの 1 拍、読みの結果は**発行順**に `rd_valid` 1 周期 + `rd_data`、受け側は止められない）で、`fprobe_core` の `mem_*` ポート（行 187〜197）はそれを使う。AXI のバースト（`AxLEN = 1`）は `axi_mem_port` の内側で隠れている。
→ **約束は同じにできる。固定する。**

同じにできない差（事実）:

| 差 | PYNQ `axi_mem_port` | Primer `primer20k_ddr3` | 口での扱い |
|---|---|---|---|
| クロック | `mem_clk` = `sys_clk`（`MEM_SAME_CLOCK = true`） | `mem_clk` は IP のユーザー側クロック（約 99.6 MHz、sys と非同期）。コア内で同期化（`core_storage`） | 口は `mem_clk`/`mem_rst` を持つ。ボードが「sys と同一か」を宣言（`same_clock`）し、generic `MEM_SAME_CLOCK` を**そこから生成**する |
| ready の意味 | `base` が 0 でなく 16 の倍数の間だけ ready（PS が `MEM_BASE` を書くまで 0。`axi_mem_port.vhd` 先頭） | DDR3 の初期化・較正が済むと ready | 口は「`mem_ready` が 1 になるまでコマンドを出さない」までを約束。ready が 1 になる条件は約束しない（宣言の説明文に書く） |
| 副路 | `base`（入力）・`errors`（出力）・`wr_pending`（出力）。`zynq_frame_link` の `mem_base`・`mem_errors` と往復（行 246〜259） | 無し | **メモリは PYNQ ではホストリンク（`zynq_frame_link`）と結合している**。副路を口に入れず、「ボード内の複合」（§3.1）として宣言する |
| `mem_wr_pending` | 実値 | 未接続（コアの既定 0） | 任意の副路 `wr_pending`。PS の直接読み（`la_stream_ps`）が要る。**Primer は提供しない**ことを宣言し、要る機能（§4.3）が断る |
| 番地幅 | 21（32 MiB / 16 バイト） | 23（128 MiB） | `MEM_ADDR_BITS` は `mem_bytes` から生成（既に `caps.py`）。ポート型 `unsigned(22 downto 0)` の手書き（S7）はやめ、生成する |
| 書きの持続帯域 | 上限 100 MHz × 8 バイト/2 周期 = 800 MB/s（`axi_mem_port.vhd` 先頭のコメント）。読みの前に書きの応答を全部待つ | **未確認**（測定値を探したが、この書の範囲では見つけていない。`docs/superpowers/specs/2026-10-16-m8-ddr3-design.md` に帯域測定の記録がある可能性） | 口の約束にしない。**測定した値をボード/アダプタの宣言に書く欄**（`sustained_write_bytes_per_s`、測っていなければ空）を持ち、要る機能が使う側の明示で比べる（§4.3） |
| 読みの遅延 | 書きの B 待ちを含む（不定） | IP の遅延 | 約束しない（`rd_valid` の順序だけ） |

### 2.2 ホストリンク口（`frame_mux` の `<prefix>_valid/ready/data` の流れ）

**実装は 4 つ**（USB・Ethernet・serial・Zynq AXI-Lite）。うち 3 つ（eth・serial・zynq）は `hostlink/frame_mux.vhd` を共有し、コア側の port が**完全に同一**（`frame_mux.vhd` 行 29〜81 の ctrl/dap/swo/stream/uart/line_rate）。USB は `usb_device` + `usb_core_cdc` が同じ sys ドメインの流れ（`s_*`）を出す。
→ **約束は同じにできる。固定する。** 固定するのは「sys ドメインの流れの束」＝ `fprobe_core` の ctrl/dap/swo/uart/stream ポートそのもの（既に名前が揃っている）。クロックまたぎ・フレーム化・USB/UDP/COBS はアダプタの内側。

同じにできない差:

| 差 | 事実 | 扱い |
|---|---|---|
| `packet_size` | USB は速度（HS/FS）で 512/64 を切り替える信号（`usb_core_cdc`）。他は定数 512 | 口に `packet_size` を入れる。固定のリンクは定数を返す |
| `swo_flush` | USB だけが使う（SWO のバッファを捨てる）。frame_mux 系にポートが無い（`rg swo_flush gateware/rtl/hostlink` が 0 件） | 任意の副路（`swo_flush`）。無いリンクは open |
| UART の制御線（host_rts / host_break / cts） | USB だけ。PYNQ は `uart_host_rts => '0'`・`uart_cts => open`（行 ~320）。eth/serial は未接続（コアの既定） | 任意の副路 `uart_ctrl_lines`。`UART_FLOW`（RTS/CTS の CSR）が**リンクの `host_rts` を要するのか、要らないのか**は未確認 → §9 の確かめ項目。確かめるまで「要る資源」には書かない（推測で決めない） |
| ストリームの `last` | USB は `stream_in_last => '0'`、frame_mux 系にポートが無い | 口に含めない（常に 0） |
| 線上の速さ・パケットの大きさ | USB HS 480 Mbit/s、eth 10/100M、serial 2 Mbaud、Zynq は AXI-Lite 経由（PS の中継が UDP） | 約束しない。リンクの宣言に**測った値**（持続スループット）を書く欄（PYNQ の `PYNQ_Z2_STREAM_BYTES_PER_S` が先例）。測っていなければ空 |
| PS の窓（`ps_bus_*`） | Zynq だけ | 副路 `ps_window`。PYNQ の複合（§3.1）の中に閉じる |
| 時刻の同期・並び | `timebase` はコアの中で共通 | 無関係 |

境のレジスタ: 受信側は `core/fprobe_core.vhd` が ctrl_in にレジスタスライス（`ctrl_s`、行 257〜258）、`frame_mux` はバッファ RAM を挟む。**新しいスライスを足すと DAP の往復が 1 周期延びる**。延びが DAP のスループットに効くかは**未確認**（`dap_in` が律速か未測定）。よって「境にはレジスタ段」は、**新しいアダプタが自分の境に持つ**規則とし、既存のものに一律に足さない（§6）。

### 2.3 LA 入力段（2 種）

- **生入力（raw）**: 端子の値をそのまま `la_pins`/`la_inputs` に渡し、コアが 2 段同期化する。実装は「端子直結」で、Primer（USB/eth/serial）・Nano・PYNQ（lanes 無し）が全部。
- **レーン（lanes）**: 端子の ISERDESE2 が sys の 1 周期に N = 2 × `CAP_RATIO` サンプルを出し、`la_input_lanes`/`la_pin_lanes` に渡す。**実装は 1 つ**（`boards/pynq_z2/xilinx_la_iserdes.vhd`）。

→ raw は固定（既にコアのポートで固定されている）。**lanes は 2 つ目の実装が無いので、新しい抽象を作らない。** 既存のコアのポートの約束（`la_input_lanes` のレイアウト、`LA_LANE_IN_AGE`、「レーン 0 が最も古い」）がそのまま口である。
ただし以下は宣言に出す（事実として）:
- そのボードの lanes 段が出せる N と、それに要る資源（PYNQ: ISERDESE2 の数 = 14 本、`cap_clk` = sys × N/2、MMCM の VCO 1200 MHz）。
- `IN_AGE`（S3）。Python/VHDL の二重を、宣言（関数名 → 値の表）に一本化し、VHDL の関数は生成する or 検査する。
- PYNQ では JA の入力が ISERDES の組み合わせ出力 `ja_in` になり、**DAP・UART・gpio の入力も同じ経路を通る**（`pynq_z2_fprobe_top.vhd` 行 15〜16 のコメント）。LA 入力段が端子の入力を握る結合であり、§2.4 の端子の段と一体の複合になる。

### 2.4 端子の出力段（候補として追加）

依頼文の 4 つの口には無いが、**3 通りの手書きがあり、ボード時刻の事実（S6）が絡む**ので候補にする。
実装は 2 つ（Primer = 三状態の組み合わせ、PYNQ = ODDR + IOB の T FF + 前半/後半の 2 組の `pin_select`）。共通部分（`debug_pin_func`・`pin_select`）は既に board 非依存の entity で、`pin_func_i`/`pin_select_i` の 2 行の instantiation が Primer USB・PYNQ・eth・serial に写してある。
約束: 「`pin_o`（後半）・`pin_o_lead`（前半）・`pin_oe` を受け、端子へ出す。端子の入力を `pins_in` で返す。**コアの出力 FF から端子までの FF の段数 `output_ticks` を宣言する**」。PYNQ = 1、Primer = 0。
→ **固定する**（2 実装 + 3 つの写しの解消 + `BOARD_OUTPUT_TICKS` の手書きの解消）。`output_ticks` は宣言し、アダプタの cocotb 試験が実測する（§7）。

### 2.5 クロック/リセット

- 実装は 2 系統（Gowin `rPLL` ×3 のトップに写し、Xilinx `MMCM`）。供給するもの: `sys_clk`、`sys_rst`（`reset_sync`、同期解除）、任意で `cap_clk`（PYNQ のみ。1 実装）。
- Primer eth は `link_rst`（PHY のリセット解除まで止める）を別に持つ（`primer20k_fprobe_eth_top.vhd` 行 84）— リンクの内側の事情。
→ `sys_clk`/`sys_rst` は固定する。`cap_clk` は 1 実装なので「任意の追加出力」としてだけ宣言し、口として一般化しない。制約（SDC/XDC）は既に `Board.sys_clock()`/`cap_clock(ratio)` が出している（ここは既に宣言から生成）。

### 2.6 補助の端子（aux・sync・obs・testgen）

アダプタが無く、**ピン資源の有無**だけが条件。例: `aux` は資源群 `aux`（Primer だけ）、`sync` は `sync`（Primer・PYNQ）、`fpga_obs` は `obs_jtag`（PYNQ だけ）、`la_testgen` は `testgen`（PYNQ だけ）。宣言では「機能 → 要る資源群」の表にする（§4.3）。
気付き: PYNQ は `sync=deep`（`designs.py` 行 ~190。深い構成だけ同期を有効にする）。**理由は書かれていない（未確認）**。利用者に確認する点（§10）。

## 3. 固定する口（ボードサービス）の決定

### 3.1 一覧

| サービス | 状態 | 実装（現在） | 固定する約束（概要） |
|---|---|---|---|
| `clocking` | 固定 | Gowin rPLL、Xilinx MMCM | `sys_clk`・`sys_rst`（同期解除）。宣言: `sys_hz`。追加出力 `cap_clk`（任意、1 実装） |
| `mem_port` | 固定 | `axi_mem_port`、`primer20k_ddr3` | 128 ビット拍、発行順の `rd_valid`、止められない読み、`mem_ready` まで出さない。宣言: `bytes`・`same_clock`・副路の有無（`wr_pending`）・測った持続帯域（任意） |
| `host_link` | 固定 | USB・eth・serial・zynq | sys ドメインの流れの束（コアの ctrl/dap/swo/uart/stream）。宣言: `packet_size` の種類・副路（`swo_flush`・`uart_ctrl_lines`・`ps_window`）・測った持続速度（任意） |
| `pad_stage`（端子の出力段 + 入力） | 固定 | Primer 三状態、PYNQ ODDR+IOB | `pin_o`/`pin_o_lead`/`pin_oe` → 端子、`pins_in` ← 端子。宣言: `output_ticks` |
| `la_input: raw` | 固定（コアのポート） | 端子直結 | 端子の値（非同期でよい） |
| `la_input: lanes` | **固定しない** | ISERDESE2 の 1 実装 | コアの既存ポートが約束。宣言だけ（N・IN_AGE・要る資源） |
| 補助の端子 | 口にしない | — | ピン資源の有無 |

**ボード内の複合（composite）**: 1 つの実装が複数の口を同時に提供し、口の間に副路があるもの。PYNQ の `ps7`（`host_link: zynq` + `mem_port: axi` + `pad_stage` の ISERDES 入力 + `ps_window`/`mem_base`/`mem_errors` の副路）。複合は 1 つのボード部品として宣言し、**複合の内側の副路は口の約束に含めない**。これが「実装が 1 つしかない口を増やさない」を守る方法。

### 3.2 層（誰が何を持つか）

```
L0  fprobe_core                        既存。変えない（generic・ポート）
L1  fprobe_system (新・静的 VHDL)       fprobe_core + pin_select/debug_pin_func + generic map（design_caps_pkg から 1 箇所）
                                        ボード非依存。ポートは口ごとの接頭辞でそろえる
L2  ボードのアダプタ（手書き、各々に適合試験）
        clocking / mem_port / host_link / pad_stage / la_input
L3  <board>_<link>_top (薄い top)       端子のポート + L2 のインスタンス + L1 のインスタンス。接続は名前の規約だけ
```

L1 を作る利点は、4 トップに写した 28 行の generic map と、sys のホストリンクの port map の写しを 1 つにすること。**L1 を挟むことで合成の結果（LUT・WNS）が変わらないか**は未確認（階層が 1 段増える。Gowin/Vivado の階層の扱い）→ §9 の実験 E3。変わるなら L1 は作らず、**生成器が L3 の中に同じ接続を出す**（案 B、§10 の決定点）。

### 3.3 新しい口を足す基準（規則）

1. 現に 2 つ以上のボードが、約束を 1 文で言える形で実装していること。
2. 約束にできない差は、口に入れず「宣言された事実」（欄）にする。
3. 副路は、2 つ以上の実装が同じ形で持つものだけ口に入れる。そうでなければ複合の内側。
4. 口ごとに cocotb の適合試験（§7）を付ける。付けられない口（Gowin の暗号化 IP）は、その旨を宣言に書く。

## 4. 宣言のスキーマ案 (c)

形式は Python の frozen dataclass（`gateware/fprobe_gw/boards/` が既に Python であること、型・検証を書けることから）。データの形（JSON/TOML）にもできるように、**関数・ラムダを宣言に入れない**。

### 4.1 ボード（資源の供給側）

```python
@dataclass(frozen=True)
class MemService:           # mem_port の実装の宣言
    name: str                       # "ddr3_gowin" / "axi_hp0_zynq"
    adapter: str                    # VHDL entity 名
    sources: tuple[str, ...]        # 依存順
    bytes: int                      # 2 の累乗 × 16
    same_clock: bool                # mem_clk == sys_clk か
    sidebands: frozenset[str]       # {"wr_pending", "errors", "base"}
    sustained_write_bytes_per_s: int | None   # 測った値。測っていなければ None（見積もらない）
    measured_in: str | None         # 測定の記録（docs/reports/... の節）
    ips: tuple[GowinIp, ...] = ()
    clocks: tuple[ClockConstraint, ...] = ()
    pin_groups: tuple[str, ...] = ()
    conformance: str                # "cocotb:test_mem_port_axi" / "hardware:memtest"（暗号化 IP は模型が無い）

@dataclass(frozen=True)
class HostLinkService:
    name: str                       # "usb_hs_ulpi" / "eth_udp_rmii" / "serial_cobs" / "zynq_axil"
    adapter: str
    caps_link: int                  # caps.LINK_*
    sources, pin_groups, clocks, io_delays, placements, options, cdc_max_delay_ns
    packet_size: int | Literal["by_speed"]
    sidebands: frozenset[str]       # {"swo_flush", "uart_ctrl_lines", "ps_window"}
    measured_stream_bytes_per_s: int | None

@dataclass(frozen=True)
class PadStage:
    name: str                       # "tristate" / "iob_oddr"
    output_ticks: int               # コア出力 FF から端子までの FF 段数（timing.BOARD_OUTPUT_TICKS を置き換える）
    sources, adapter

@dataclass(frozen=True)
class LaLanes:                      # lanes 段の事実
    lanes: tuple[int, ...]          # 出せる N（例 (6, 8)）
    in_age: dict[int, int]          # N → IN_AGE（測定または xsim の記録。出どころを tests/data/ に）
    needs: tuple[str, ...]          # {"cap_clk", "serdes_pins:14"}

@dataclass(frozen=True)
class Composite:                    # 1 実装が複数の口を出すもの（PYNQ の ps7）
    name; provides: dict[str, str]  # {"host_link": "zynq_axil", "mem_port": "axi_hp0_zynq"} と副路の内部結線の説明

@dataclass(frozen=True)
class Board:                        # 既存の Board を拡張（既存の項目はそのまま）
    ...既存...
    clocking: ClockingService
    mem: tuple[MemService, ...]
    links: tuple[HostLinkService, ...]
    pad: PadStage
    la_lanes: LaLanes | None
    composites: tuple[Composite, ...]
    capacity: Capacity              # デバイスの事実（LUT/FF/BRAM の総数。ベンダー資料から）
```

### 4.2 プロファイル（構成）

```python
@dataclass(frozen=True)
class Profile:
    name: str                       # "fprobe_eth"
    board: Board
    link: str                       # board.links の name
    mem: str | None
    pad: ...                        # board.pad（既定は board の唯一のもの）
    la_buffer: Literal["internal", "deep"]   # LA_INTERNAL を宣言にする（今は暗黙に mem_bytes==0）
    features: Mapping[str, Any]     # Capabilities の機能の欄。**省略を許さない**（既定値に頼らない。全部書く or 継承元の名前を書く）
    exclusions: tuple[Exclusion, ...]   # 面積・タイミングで外した機能と理由
    build: BuildOptions             # options・cdc_max_delay_ns・placements
    extends: str | None             # 継承元のプロファイル（fprobe → fprobe_jtag 等）

@dataclass(frozen=True)
class Exclusion:
    feature: str                    # "sampler_match"
    reason: str                     # 事実の文章
    evidence: str                   # 測定の記録（docs/reports/...、ビルドの識別子）
```

`Exclusion` は §1.4 の重複 2（`sampler_match=False` が 4 箇所）を 1 つの表にする。**外した理由が測定の記録に結びつく**ので、「面積は人の経験」ではなく「測ったら入らなかった」という事実の一覧になる。外す決定そのものは人（利用者）が書く。生成器は外さない・外すよう勧めない。

### 4.3 機能レジストリ（機能が要る資源・両立しない組み合わせ）

`gateware/fprobe_gw/features.py`（新）に、機能ごと 1 エントリ。**理由の文を必須**にする（生成時のエラーにそのまま出す）。

```python
FEATURES = {
  "la_stream_ps": Feature(
      generics=(),                       # コアの generic は無い（la_ps_port は常にある）
      tlv_bit="FEATURE_LA_STREAM_PS",
      requires=[Need("mem", sideband="wr_pending",
                     why="PS が DDR のリングを直接読むには、応答の来ていない書きの数（axi_mem_port.wr_pending）を引いた位置までしか読めないと教える必要がある"),
                Need("link", sideband="ps_window",
                     why="PS の窓 zynq_frame_link の ps_bus_* が la_ps_port への道")]),
  "la_lanes>1": Feature(                 # 800 MS/s は N = 8
      requires=[Need("la_lanes", lanes="N",
                     why="端子の ISERDESE2 が 1 周期に N サンプルを出す。cap_clk = sys × N/2 が要る"),
                Need("clocking", output="cap_clk")],
      conflicts=[("la_filter", "グリッチフィルタ・パルス幅のトリガは 1 周期 1 サンプルの前提"), ("la_pulse", "同左")]),
  "la_token": Feature(                   # 依頼文の「トークン形式は 1 周期 1 サンプル」。中身は未確認（別 agent が実装中）
      conflicts=[("la_lanes>1", "1 周期 1 サンプル")] ),
  "sync": Feature(requires=[Need("pins", group="sync", why="sync_in / sync_out の端子")]),
  "fpga_obs": Feature(requires=[Need("pins", group="obs_jtag")]),
  "la_testgen": Feature(requires=[Need("pins", group="testgen")]),
  "aux": Feature(requires=[Need("pins", group="aux")]),
  "la_inputs>0": Feature(requires=[Need("pins", group="la_in", count="la_inputs")]),
  "outputs": Feature(requires=[Need("debug_connector", names="output_pins")]),
  "mem_test": Feature(requires=[Need("mem")], conflicts=[("la_stream_events", "...")]),
  "la_stream_events|la_pack|la_stream_gaps": Feature(requires=[Need("mem")]),
  "event_ring": Feature(conflicts=[("mem", "外部メモリ有りは深いバッファに記録する")]),
  ...
}
```

規則:
- 既存の `Capabilities.__post_init__`（`caps.py` 行 195〜229）の検査は、**ここに移して理由つきにする**（今は `ValueError` の文に書いてある。内容は同じ）。移す前後で同じ組み合わせを通す/断ることを試験で示す（§8 段階 5）。
- 要る資源を満たさない構成は、生成時に **「プロファイル X は機能 F を要求するが、ボード B の Y が提供しない。理由: …」** で止める。機能を黙って外さない。代わりの機能を選ばない。
- 資源の数値（持続スループット等）を要る機能は、**プロファイルの側が「最低これだけ要る」と明示**したとき（例 `require_stream_bytes_per_s=50_000_000`）だけ比べ、ボードの測定値が空なら「未測定のため確認できない」で止める（測定値を推定で埋めない）。

### 4.4 プロファイル → ケイパビリティの導出

今 `Capabilities` を手で組み立てている（§1.4 重複 1）。新しくは `Profile` + `Board` から**機械的に**作る:

| ケイパビリティの欄 | 出どころ |
|---|---|
| `board`・`sys_clock_hz`・`timebase_hz`・`la_sample_hz`（lanes 無し） | `Board.clocking.sys_hz` |
| `host_link` | `HostLinkService.caps_link` |
| `channels` | プロファイルの宣言（全件 CONTROL, DAP, UART0 だが、書く。既定値にしない） |
| `mem_bytes`・`mem_test` | `MemService.bytes`・プロファイル |
| `outputs`・`uart_flow`・`la_inputs` | `Board.outputs()`/`uart_flow()`/`la_inputs()`（既存）をプロファイルが「使う」と書いたとき |
| `la_lane_in_age` | `LaLanes.in_age[N]` |
| その他の機能の欄 | プロファイルの `features` |

### 4.5 検査（生成時）

1. スキーマの型・範囲（`Capabilities.__post_init__` を機能レジストリ + 数値の範囲に分けて移す）。
2. 資源の充足: §4.3 の `requires`／`conflicts`。
3. 名前の解決: `link`・`mem`・`pin_groups`・クロックがボードに存在する。
4. 一貫性: `LA_INTERNAL = (mem is None)`（宣言 `la_buffer` と矛盾したら止める。いまは暗黙）。`MEM_SAME_CLOCK`・`CLK_HZ`・`LA_PIN_COUNT`・`LA_FINE_PINS` を宣言から生成し、**トップに手書きしない**（S1・§1.1 の `LA_PIN_COUNT` の穴を閉じる）。
5. 出力の段数の一貫: `output_ticks` を宣言から `timing` へ（S6）。
6. ソース: 各アダプタの `sources` が実在し、重複除去後の順が依存順（既存の GHDL 展開試験で検出）。
7. 出力: 生成物（§5）は `tests/test_generated.py` が一致を検査（既存の仕組み）。

## 5. 生成物

既存（`fprobe_gw/generate.py` `artifacts()`）に足す。コミットする生成物の仕組み（`python -m fprobe_gw gen` と `test_generated.py`）をそのまま使う。

| 生成物 | 内容 | 置き換える手書き |
|---|---|---|
| `rtl/designs/generated/<design>_caps_pkg.vhd` | 既存の `CAPS_ROM` + 構成の定数に、**`CLK_HZ`・`LA_INTERNAL`・`LA_FINE_PINS`・`LA_PIN_COUNT`・`MEM_SAME_CLOCK`・`RESET_SPLIT`・`MEM_ADDR_BITS`・`OUTPUT_TICKS`** を追加 | トップの手書きの generic（§1.1 のボード固有 7 個）、`pynq_z2_pkg` の `SYS_CLK_HZ`・`LA_INPUT_PINS` のうち重複するもの |
| ケイパビリティ ROM | 既存。入力が `Profile`+`Board` に変わるだけ（出力は同一であることを段階 1 で証明） | `designs.py` の `Capabilities(...)` 手組み |
| ソース一覧（`Design.sources`） | `core_sources` + 各アダプタの `sources` を、層の順 + `dict.fromkeys` で連結（今と同じ規則） | `designs.py` の `rtl(...)` の並び |
| `Design`（pin_groups・clocks・ips・io_delays・placements・options・cdc_max_delay_ns・block_designs） | サービスの宣言の和 | `designs.py` の手書き |
| `<design>_top.vhd`（案 A）または L3 の接続部（案 B） | 端子のポート宣言（向きはサービス宣言に持たせる）、アダプタとシステムのインスタンスと名前規約による接続 | 4 つのトップの写し |
| `docs/board-matrix.md` | ボード × プロファイル × 機能 × 要る資源の表 + 測定値の出どころ | （無し。人と AI が同じ表を見る） |
| ホスト定数 `DESIGN_FOR_*` | 既存（`PROTOCOL_DESIGNS`・`FEATURE_DESIGNS`） | 変えない |

## 6. 面積・タイミング（ヒューリスティックなしで確かめる）

- 生成器は**見積もらない**。`Exclusion` の根拠は過去の測定の記録（`docs/reports/2026-10-22-primer-resources.md` 等）を指す。
- ビルドの合否（既存＋明確化）: (1) 合成の警告 EX1998 があれば失敗（既存。CLAUDE.md）、(2) 配置配線の使用量がデバイスの総数以内（ツールが失敗する）、(3) WNS ≥ 0（タイミングを満たす、は定義であって閾値ではない）。
- **回帰を事実として並べる**: ビルドごとに `LUT・FF・BRAM・sys の WNS・Fmax` を `build/<design>/facts.json` に残し、`python -m fprobe_gw facts <design> --against <記録>` が差を表示する。**差が閾値を超えたら失敗、は既定では入れない**。使う側が `--max-lut-increase N` のように明示した引数を渡したときだけ失敗にする（原則どおり）。
- 基準値（今の事実）: Primer の標準 `fprobe` = LUT 17894/20736（87%）、FF 12419（77%）、BSRAM 41/46、sys Fmax は配置の版で 101〜107 MHz（`docs/reports/2026-10-22-primer-resources.md`）。PYNQ は sys 100 MHz、WNS +0.2 ns 前後（利用者の依頼文による。この書の範囲では PYNQ の最新の実測値を再確認していない: **未確認**）。**移行の各段階でこれを悪化させない**ことが受け入れ条件（§8）。
- 境にレジスタ段: §2.2 のとおり、新しいアダプタの境は自分でレジスタにする。既存アダプタに一律に足さない（`output_ticks` が変わり時刻の表が変わる／DAP の往復が延びる）。

## 7. アダプタの適合試験（cocotb）

試験は「口の約束」を 1 つの共通のモニタ/参照モデルで検査し、実装ごとにそれを当てる。

| 口 | 共通の検査 | 実装ごとの当て方 | できない部分 |
|---|---|---|---|
| `mem_port` | 乱数の番地へ書き → 読み戻し一致、発行順、`cmd_ready` が落ちてもコマンドが失われない、読みの結果を止めない、`mem_ready`=0 の間はコマンドを受けない、リセット後の状態 | `axi_mem_port` + AXI3 スレーブの参照モデル（`test_axi_mem_port.py`・`test_deep_axi.py` の延長） | `primer20k_ddr3` は Gowin 同梱の暗号化ネットリストで **GHDL に模型が無い**。実機の `memtest` デザイン（`mem_test`）が同じ約束の実機試験（宣言の `conformance: "hardware:memtest"`）。実機を使うので、この書では実施しない |
| `host_link` | sys 側の束のプロトコル（`valid` を上げたら `ready` まで `data`/`last` を変えない、`last` の意味、リセット中に `valid` を出さない）を **VHDL のモニタ（process + assert）**として 1 つ作り、各リンクの試験台に付ける。フレームの往復（CONTROL の読み書き、DAP のパケット、UART の往復）の共通シナリオ | `test_serial_link.py`・`test_eth_link.py`・`test_frame_link_harness.py`（zynq）・USB（`usb_core_cdc` の試験）に同じモニタ | USB の ULPI の相手（PHY）の模型は既存の範囲 |
| `pad_stage` | `pin_o`/`pin_oe` を与え、端子の値が出るまでの**段数を実測**し、宣言の `output_ticks` と等しいこと。`pin_o_lead`≠`pin_o` の半周期の振る舞い。`pins_in` の遅れ | Primer = 三状態（0 段）、PYNQ = `xilinx_oddr` + IOB FF（1 段。`tests/rtl/sim_xilinx_prims.vhd`） | ODDR の物理的な位相は合成後でないと分からない（既存の `docs/reports` の実測に依る） |
| `la_input: lanes` | 既存 `xilinx_la_iserdes_tb`（IN_AGE の実測）を宣言の `in_age` と比較 | PYNQ | 実装が 1 つ |
| `clocking` | ロック → `sys_rst` が同期して解除、再ロックで再リセット | `reset_sync` の既存試験の延長 | PLL の模型（`tests/rtl/sim_gowin_prims.vhd`・`sim_xilinx_prims.vhd`）の範囲 |

## 8. 移行の段階（各段階で既存の試験とビルドが壊れないこと）

共通の受け入れ条件（毎段階）:
- **A**: `pytest gateware` と `pytest host` が全部通る（試験の数が減らない）。
- **B**: `python -m fprobe_gw gen` の後 `git diff` が空（生成物が変わらない）— 「同じになる」段階では。
- **C**: 触った構成の合成・配置配線が通り、LUT/FF/BRAM が移行前と**同一**（変わるなら理由を事実として書き、利用者が受け入れる）、sys の WNS が悪化しない。Primer のビルドと PYNQ のビルドは時間がかかるので、段階ごとに「触ったボードだけ」実施する。
- 実機は使わない段階を先に、実機の確認が要る段階（PYNQ・Primer の書き込み）は利用者の許可後（この書の範囲外）。

**段階 0：ベースラインの記録（コードを変えない）**
Primer `fprobe`・`fprobe_eth`・`fprobe_serial`・`fprobe_nano9k`・PYNQ `fprobe_pynq`・`fprobe_pynq_deep_fast` のビルドを 1 回ずつ走らせ、LUT/FF/BRAM/WNS/EX1998 と、可能なら合成後ネットリストのハッシュを `build/baseline/` に記録する。実験 E1（ビルドの決定性）を同時に行う。

**段階 1：宣言だけ足す（RTL・ビルド不変）**
`Board` に `clocking`/`mem`/`links`/`pad`/`la_lanes`/`composites`/`capacity` を足し、`Profile` と導出（§4.4）を別モジュール（例 `designs_v2.py`）に作る。既存の `designs.py` はそのまま。
**証明**: 試験 `test_profiles_reproduce_designs` が、全 fprobe 系デザインで次を `==` で比べる:
(1) `Design` の全欄（`sources` は**並びも**、`pin_groups`、`clocks`、`ips`、`io_delays`、`placements`、`options`、`cdc_max_delay_ns`、`block_designs`、`ps_axi`）、
(2) `Capabilities` の全欄、(3) `caps.render_package(...)` の出力文字列。
最初の対象は **Primer の eth 構成**（`fprobe_eth`、`designs.py` 行 325〜347）。ここが通れば `design_caps_pkg` のバイト一致まで証明される。

**段階 2：ボード固有 generic の生成（RTL を最小に変える）**
`design_caps_pkg` に §5 の追加定数を出し、4 トップの `CLK_HZ`・`LA_INTERNAL`・`LA_FINE_PINS`・`MEM_SAME_CLOCK`・`RESET_SPLIT` をそれに置き換える。`test_core_generics.py` を「BOARD は生成パッケージからも渡す」に更新。
**証明**: 合成の入力（定数の値）が同一 → 合成後の使用量が同一（条件 C）。値が変わるもの（`LA_PIN_COUNT` を初めて渡す）は既定と同じ 8 であることを試験で確認。

**段階 3：端子の出力段の共通化**
`debug_pin_func`+`pin_select` の写し（Primer USB/PYNQ/eth/serial）を `pad_stage` のアダプタにまとめ、`BOARD_OUTPUT_TICKS` を宣言から出す（S6 の解消）。PYNQ は ODDR の IOB 属性・信号名を保つ（配置・タイミングを変えない）。
**証明**: `pad_stage` の適合試験（段数の実測）+ 条件 C。**PYNQ の WNS（+0.2 ns 前後）を最も壊しやすい段階**なので、PYNQ は最後に、`pad_stage` を入れる前後の比較が同一のときだけ進める。

**段階 4：L1 `fprobe_system` と薄い top の生成**
Primer eth から。生成された top（または L3 の接続部）で、ソース・generic・ポートの接続が既存と同じ。
**証明の梯子**（上ほど強い。どこまで要るかは利用者の決定、§10）:
 1. `design_caps_pkg` がバイト一致（段階 1 で済み）。
 2. `Design` の全欄が `==`（段階 1 で済み）。
 3. 生成した top を GHDL で展開でき（`test_design_elaboration`）、**既存の top と entity のポート・generic が同じ**（機械的に比べる）。
 4. 合成後の階層別の使用量表（`python -m fprobe_gw resources`）が既存と**同一**。
 5. 合成後ネットリストが一致（E1 で決定的と分かった場合のみ）。
 6. 実機で Primer eth のスモーク（利用者の許可後。この書では実施しない）。
段階 4 の順: eth → serial（Primer）→ Nano 9K（serial と同じ L1/L3 を使うので、**宣言だけで足せることの最初の実証**）→ Primer USB → PYNQ。

**段階 5：機能レジストリと生成時の拒否**
`Capabilities.__post_init__` の検査を `features.py` へ移し、`requires`（S11 の穴）を足す。
**証明**: 既存の全デザインが通り、既存の ValueError の組み合わせが同じ理由で断られる（試験で列挙）。新しい `requires` に反する組み合わせ（例: Primer に `la_stream_ps`）が理由つきで断られる。

**段階 6：新ボードの空打ち（受け入れ試験）**
宣言（ボード + プロファイル）と、薄い top（生成）だけで 1 つ新しい構成を足せるか。具体の候補は利用者が決める（§10）。Nano 9K を最初からこの形で再現できることを段階 4 の終わりで示しているので、段階 6 は**別の種類のボード**（例: Shrike-Lite、Tang Nano 4K/20K）でもよい。

段階 3 以降は RTL を変えるため、別の agent による `la_*` 系の変更と衝突しうる。`rtl/la/` はこの設計で触らない（コアのポートも変えない）ので、段階 1・2・5 は並行して進められる。段階 3・4 は `la_tok_encoder` 等の変更が落ち着いてから。

## 9. 未確認の項目と、確かめる実験

| ID | 未確認の点 | 確かめ方 |
|---|---|---|
| E1 | 同じ入力でビルドを 2 回走らせたとき、合成後ネットリスト・配置配線の結果（WNS・LUT）が同一か | Gowin（`fprobe_eth`）と Vivado（`fprobe_pynq`）を各 2 回。配置の版（`-place_option`）固定で比べる。一致しなければ梯子の 5 は使えず、4 の使用量表 + WNS の幅で見る |
| E2 | VHDL-2008 の record をポートに使えるか（Gowin 1.9.11.03 / Vivado）。使えれば L1 のポートを束にできる | 小さい試験設計。**使えなくても生成器が平らな名前で接続するので、設計の前提にしない** |
| E3 | L1 の階層を 1 段挟んだとき LUT・WNS が変わるか（特に PYNQ の WNS と Primer の LUT 87%） | `fprobe_eth` と `fprobe_pynq` で、現行 top と L1 経由を並べて測る。変わるなら案 B（接続を生成器で top に展開、階層を増やさない） |
| E4 | Primer の DDR3 アダプタに GHDL での適合試験を作れるか | Gowin IP の模型の有無を確認（今は無い）。作れなければ実機の `memtest` を適合試験とする |
| E5 | `UART_FLOW`（CSR の RTS/CTS）が host の RTS（リンクの副路）を要するか | `core_periph`/`uart_bridge` の `uart_host_rts` の使い方を読む。PYNQ は `'0'` を渡して CSR の flow が機能する構成のため、**要らない可能性があるが断定しない** |
| E6 | Primer の DDR3 の持続書き込み/読み出し帯域の測定値の有無 | `docs/superpowers/specs/2026-10-16-m8-ddr3-design.md` と `memtest` の記録を読む |
| E7 | `FEATURE_WATCH_START` の回路側の条件（S10） | CSR `inject.start` の生成元（`fprobe_map.py` 行 214 付近）と `swd_injector` の generic を読む |
| E8 | PYNQ の `sync=deep` の理由 | 利用者に確認（§10） |
| E9 | PYNQ の最新の WNS・LUT（依頼文は +0.2 ns 前後） | 段階 0 で記録 |
| E10 | トークン形式（`la_tok_*`）が要る資源 | 別 agent の作業が入ってから、その仕様書で確認。それまで機能レジストリには書かない |
| E11 | `S13`: 制約にあるのにトップに無いポートがビルドで警告か失敗か | 小さい試験（Gowin・Vivado）。生成器が両方を同じ宣言から出すなら無関係になる |

## 10. 利用者に決めてほしい点

1. **薄い top を生成するか、手書きか**（案 A: 生成器が top 全体を出す。案 B: L1 `fprobe_system` を静的 VHDL にして手書きの薄い top が呼ぶ。案 C: 両方）。推奨は **A（接続を名前の規約で生成）+ 例外だけ手書きで上書き可**。理由は、写しの原因が接続の列挙であり、列挙は機械が間違えないから。ただし E3 の結果次第で L1 を作らない。
2. **宣言の形式**: Python の dataclass（推奨。型と検証が書ける）か、データファイル（TOML 等。AI/MCP から読み書きしやすい）か。推奨は Python で書き、`docs/board-matrix.md` と JSON の書き出しを同じ定義元から出す。
3. **証明の梯子（§8 段階 4）のどこまで**必要か。推奨は 1〜4（実機を使わない）を毎回、6 は節目だけ。5 は E1 次第。
4. **`RESET_SPLIT` と `MEM_SAME_CLOCK` の置き場**: ボードの事実（宣言）にするか、プロファイルのタイミング調整の選択にするか。推奨は `MEM_SAME_CLOCK` = ボードの事実、`RESET_SPLIT` = プロファイルの選択（Primer に入れたときの影響が未確認のため、勝手に既定を変えない）。
5. **`Exclusion`（面積で外した機能）を必須の記録にするか**。推奨は必須（理由と測定の記録のペアを書かない外し方を許さない）。
6. **PYNQ の `sync=deep`**（E8）の理由。理由が事実（面積・タイミング）なら `Exclusion` に移す。
7. **非 fprobe デザイン**（`bringup`・`ulpi_probe`・`usbtest`・`obs_target_primer`・`zynq_dut`・`pynq_blinky`・`memtest`）を宣言の対象に入れるか。推奨は **最初は外す**（`fprobe_core` を使わないものは「生のデザイン」として今の `Design` のまま）。`memtest` は `fprobe_core` を使うので対象に入れる。
8. **段階 6 の新ボード**に何を選ぶか（Nano 9K は段階 4 で再現するので、別の種類のボードを推奨）。
9. **持続スループットなどの測定値を宣言に必須の欄にするか**。推奨は任意（空なら「未測定」と表示）で、機能が最低値を明示して要求したときだけ使う。
10. **このあとの順番**: 他の agent の `la_*` の変更が収束するまで、段階 0・1・2・5（RTL をほとんど変えない）から始める案でよいか。

## 11. この書が確かめていないこと（まとめ）

- 実際にビルドしていない（Gowin・Vivado とも）。LUT・WNS は既存の記録（`docs/reports/2026-10-22-primer-resources.md`、依頼文）から引いた。
- 実機に触れていない。
- `la_tok_encoder.vhd` 等は読んでいない（別 agent が変更中でこの worktree に無い）。トークン形式の資源・両立条件は未確認。
- `rtl/` の各ファイルは、トップ 4 本・`fprobe_core`・`axi_mem_port`・`primer20k_ddr3`・`frame_mux`・`serial_link`・`zynq_frame_link` のポート/先頭のコメントを中心に読んだ。内部の挙動（`core_storage` の同期化、`la_ps_port` 等）の細部は読んでいない。
- `usb_core_cdc`・`eth_link` の内部と、`host/` 側のリンクの使い方は読んでいない。
