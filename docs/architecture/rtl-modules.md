# RTL モジュール地図（gateware/rtl/）

`gateware/rtl/` の構造。各ファイルの責務は先頭のコメントの要約。VHDL-2008。
どのボードのどの設計が何を持つかは [feature-matrix.md](feature-matrix.md)、ホスト側は [host-modules.md](host-modules.md)。

## 1. ディレクトリ

| ディレクトリ | 中身 | ボード依存 |
|---|---|---|
| `core/` | `fprobe_core`（CSR 空間・DAP・UART・ロジアナ・イベント記録・クロストリガ・観測を束ねるコア）と、その部品。`generated/` に CSR の各ブロック（`csr_*.vhd`）・`csr_map_pkg`・`build_info_pkg` | 無し |
| `debug/` | CMSIS-DAP プロセッサ（ハードウェア FSM）、SWD/JTAG の PHY、SWO 受信、DAP サンプラ、故障注入、FPGA 観測のエンジン | 無し |
| `la/` | ロジアナ（前段・捕捉・フィルタ・パルス幅トリガ・深いバッファ・イベントリング） | 無し |
| `mem/` | 外部メモリ（DDR3 等）の側: ロジアナ/イベントの書き込み・読み出し・メモリ試験、AXI の口 | 無し（`axi_mem_port` は AXI 向け） |
| `common/` | 共通部品: FIFO・同期化・リセット・スライス・割り算器・RAM・`util_pkg` | 無し |
| `serial/` | UART の送受信と、ホスト（CDC-ACM）⇄ ターゲットの UART ブリッジ | 無し |
| `usb/` | USB 2.0 HS デバイスのスタック（ULPI リンク・パケット・エンドポイント・制御転送・CDC-ACM）。`generated/usb_descriptors_pkg.vhd` | ULPI（USB3317）向け |
| `eth/` | RMII 受信/送信と最小の UDP スタック（ARP・ICMP echo・UDP） | RMII 向け |
| `hostlink/` | フレーム形式のホストリンク: `frame_mux`（論理チャンネルの多重化）、`serial_link`、`eth_link`、`zynq_frame_link`（PS の Linux 中継との AXI4-Lite の受け渡し） | 無し（リンクごと） |
| `boards/` | ボード固有: トップ（`*_fprobe_top.vhd` 等）、ピン・クロックのパッケージ（`*_pkg.vhd`）、PLL/MMCM、DDR3 の口、PS7 のブロック設計（`ps7_bd.tcl`・`ps7_hp_bd.tcl`） | あり（`primer20k/`・`nano9k/`・`pynq_z2/`） |
| `designs/` | 設計ごとの付属: 生成されたケイパビリティ ROM（`generated/<設計>_caps_pkg.vhd`）、立ち上げ・試験・DUT 用の回路（`bringup_core`・`ulpi_probe_core`・`usbtest_responder`・`dut_*`） | 一部 |

ベンダー固有プリミティブ（Gowin の rPLL、Xilinx の MMCM 等）は `boards/` のラッパー越しにだけ使う。
ビルドに入れるソースの並びは `gateware/fprobe_gw/designs.py`（`_CORE`・`core_sources()`）が正。

## 2. fprobe_core の中身（`core/fprobe_core.vhd`）

ホストリンク（トップが用意）から見ると、CONTROL・DAP・UART0・SWO・STREAM の論理チャンネルを受け渡しする。

```
CONTROL 要求 → control_plane → csr_decoder ─┬ csr_system / csr_timebase(timebase) / csr_gpio / caps_rom
              ← 応答                         ├ csr_la / la_mem_csr / csr_la_deep / csr_mem
                                             ├ csr_xtrig / csr_xtrig_out / csr_trigseq
                                             ├ csr_sampler / csr_sampler_match / csr_aux / csr_sync / csr_obs / csr_events
                                             └ csr_uart / csr_debug
              （外した機能の窓は csr_absent: どの読み書きにもバスエラー）

DAP コマンド → dap_processor → swd_injector → dap_sampler → debug_port ─┬ swd_phy → SWCLK/SWDIO
                ↑ dap_monitor（転送を覗く）                              └ jtag_phy → TCK/TMS/TDI
SWO ピン → swo_receiver（NRZ / Manchester）→ swo_out
UART ホスト側 ⇄ uart_bridge ⇄ ターゲットの UART
ピン + 内部信号 + 専用入力 → la_filter → la_front → la_capture（内蔵）/ la_encoder → la_deep_buffer（外部メモリ）
```

`dap_processor` は `dap_packet`（パケットの枠）、`dap_cmd_config` / `dap_cmd_swo` / `dap_cmd_sequence` / `dap_cmd_transfer`（コマンドのユニット）、
`dap_link`（PHY への命令）で構成される。

`core/` の部品: `control_plane`（CONTROL 要求の解釈）、`csr_decoder`、`caps_rom`、`timebase`（64 ビットのフリーラン）、`trig_seq`（多段トリガ）、
`xtrig_out`（クロストリガの出力の端子）、`pin_select`（端子ごとの駆動の選択）、`aux_link`（補助マイコンの計測値の受け口）、
`sync_link`（ボード間の時刻の同期）、`event_collector`（イベントの集約）、`csr_absent`。

## 3. FPGA 観測の経路（`OBS_PORT` のときだけ）

対象の FPGA に入れた観測の回路（`target_ip/fprobe_obs`）を、対象の JTAG の口でスキャンし続け、記録と印をイベント 8・9 にする。

```
 CSR obs（csr_obs）──設定・命令(cmd_push)──▶ obs_jtag_engine ──obs_tck/tms/tdi/oe──▶ 対象の JTAG
 （enable / half_period / ir_len / words /             │  ◀──────────────────obs_tdo
   sync_period / id_scans / match_*）                   │
                                       jtag_phy ◀──────┤  スキャンの組み立て（状態機械）と止める判定（stuck / id_timeout）
                                                       │
            ┌──────────────────────────────────────────┼───────────────────────────────┐
            ▼                                          ▼                               ▼
     obs_cmd_queue                              obs_record_rx                     obs_match
  命令の待ち行列（FIFO、                   受けた語の解釈:                   観測した値の一致
  満杯で捨てて dropped、                   見出し・標本型・応答・記録を        （値の変化と mask/value）
  flush で開き直し）                       見分け、ev_*（イベント）・          → match（1 周期のパルス）
  → 次のスキャンの命令                      read_value・id・id_ok・            → クロストリガの源 bit4
                                           obs_v（観測した値）を出す ──────────▶ obs_match.obs_v
                                                │
                                                ▼  ev_valid / ev_sync / ev_data / ev_addr / ev_time
                                          event_collector（種類 8 = 記録、9 = 印）
```

- `obs_jtag_engine` は、スキャンの組み立て（どの語がいつ来るか）と止める判定（`stuck`・`id_timeout`、`id_scans` が待つ上限）を持つ。
  語の解釈は `obs_record_rx`、命令の待ち行列は `obs_cmd_queue`、値の一致は `obs_match`（いずれも `obs_jtag_engine` から切り出した部品）。
- 命令の待ち行列と `obs_record_rx` への脈は、engine のレジスタ信号だけから作る組み合わせ回路で、状態機械のプロセスの同じ条件と一致させてある
  （`flush_now`・`pop_now`・`scan_start_c` 等。コード内のコメントの注意）。
- `OBS_PORT` が false の構成では、`obs_*` の出力は定数（`obs_oe = '0'` など）、CSR の窓は `csr_absent`（`fprobe_core.vhd` の `no_obs_g`）。
- Primer の設計は `obs_jtag` のピン群を持たない。`OBS_PORT` は PYNQ-Z2 の 2 設計だけ（[feature-matrix.md](feature-matrix.md)）。

## 4. イベントの流れ

イベントは 1 件 128 ビット、時刻（タイムベース）付き。1 周期に 1 件ずつ `event_valid` / `event_data` に出る。

```
 dap_monitor ────── DAP の転送（種類 1、DRW でメモリの番地を追えなかったときは 12）──┐
 swd_injector ───── 割り込み書き込み ────────────────────────────────────────────────┤
 dap_sampler ────── サンプラの語 ────────────────────────────────────────────────────┤
 uart_bridge ────── UART の RX / TX のバイト ────────────────────────────────────────┤
 swo_receiver ───── SWO のバイト ────────────────────────────────────────────────────┼─▶ event_collector
 aux_link ───────── 補助マイコンの計測値（種類 6）────────────────────────────────────┤     │ event_valid / event_data / byte_lost
 obs_record_rx ──── FPGA 観測の記録・印（種類 8・9）──────────────────────────────────┤     │
 sync_link ──────── ボード間の時刻の組（種類 10）──────────────────────────────────────┘     │
                                                                                            ▼
                                       ┌─ MEM_ADDR_BITS > 0 : la_deep_buffer ─▶ la_mem_writer ─▶ 外部メモリ（イベントのリング）
              events_enable ──────────▶│                      （LA_STREAM_EVENTS ならロジアナのストリームにも見出し付きで流す）
              （csr_events）           ├─ MEM_ADDR_BITS = 0, EVENT_RING_BITS > 0 : event_ring（内蔵 RAM のリング、2**EVENT_RING_BITS 件）
                                       └─ MEM_ADDR_BITS = 0, EVENT_RING_BITS = 0 : 記録しない（events_capacity = 0）
                                                         │ stream_out（バイト）
                                                         ▼
                              トップ → ホストリンクの STREAM チャンネル（USB は EP6、フレームのリンクは ch 4）→ ホスト
```

- `event_collector` は源の優先順に出し、同じ周期に複数あれば残りは保持して後の周期に出す。保持が出ないうちに次が来たら捨てて `byte_lost` に数える
  （先頭のコメント）。源の無い構成では、その源の valid / fire は 0 で来る。`fprobe_core` から切り出しただけで、振る舞いは切り出し前と同じ。
- イベントの種類番号・形式は `event_collector.vhd` の先頭と `csr_events`（CSR の仕様）、ホスト側の復号は `host/fprobe/events.py`。
- `MEM_TEST` の構成（`memtest`）は、深いバッファ・イベント記録の代わりにメモリ試験回路を外部メモリに繋ぐ。
- 読み出しは CSR（`csr_la_deep`）の指示で `stream_out` に出る。`event_ring` は `la_deep_buffer` のイベントの読み出しと同じ口・意味
  （PC は同じ手順で読む）。

## 5. generic で切り替わる機能（`fprobe_core` の generic）

ケイパビリティ（`gateware/fprobe_gw/caps.py` の `render_package`）が生成する `work.design_caps_pkg` の定数を、各トップが渡す。
ケイパビリティ TLV の FEATURES / DEBUG と一致させる。外した機能の CSR の窓は `csr_absent` でバスエラー。

| generic | 既定 | 意味 |
|---|---|---|
| `CAPS` / `CLK_HZ` | 必須 | ケイパビリティ ROM の内容 / `clk` の周波数 |
| `LA_PIN_COUNT` / `LA_INPUT_COUNT` | 8 / 0 | ロジアナの外部ピン数 / 専用の入力の数（内部信号 2 本の後の ch） |
| `MEM_ADDR_BITS` | 0 | 外部メモリの拍（16 バイト）の番地ビット数。0 = 外部メモリ無し |
| `LA_INTERNAL` | true | 内蔵 BSRAM のロジアナ（外部メモリのある構成では深いバッファが上位互換なので false） |
| `MEM_TEST` | false | メモリ試験回路（基板立ち上げ用）。深いバッファ・イベント記録の代わり |
| `XTRIG` | true | クロストリガ（割り込み書き込み・番地一致の CSR・源と動作の対応） |
| `TRIG_SEQ` | true | 多段トリガ |
| `AUX` | false | 補助マイコンの計測値の受け口（`aux_link`、CSR aux、イベント 6） |
| `SAMPLER` / `SAMPLER_MATCH` | false / false | DAP サンプラ（CSR sampler、イベント 7）/ サンプラの値の一致 → クロストリガの源 bit5 |
| `OBS_PORT` | false | FPGA 観測（`obs_jtag_engine`、CSR obs、イベント 8・9） |
| `SYNC` | false | ボード間の時刻の同期（`sync_link`、イベント 10） |
| `HAS_JTAG` | true | JTAG の PHY と経路（無ければ DAP_Connect の JTAG は失敗） |
| `HAS_SWO_MANCHESTER` | true | SWO の Manchester 方式の受信 |
| `UART_FLOW` | false | UART の RTS/CTS（端子はトップが候補から選ぶ） |
| `XOUT_COUNT` | 0 | クロストリガの出力の動作の数（0〜2。0 = 無し） |
| `LA_FILTER` / `LA_PULSE` | true / true | ロジアナのグリッチフィルタ / パルス幅のトリガ（無ければ CSR は読み書きできるが何もしない） |
| `LA_STREAM_EVENTS` | false | 深いバッファのストリームにイベントも見出し付きで流す（外部メモリのある構成で） |
| `EVENT_RING_BITS` | 0 | 外部メモリの無い構成のイベント記録の件数（2 のべき。0 = 無し） |
| `RESET_SPLIT` | false | リセットの扇出の分割。true なら `reset_fanout`（COUNT => 5）で回路の塊ごとに `rst` を 1 周期遅らせた別のレジスタを使う（解除もアサートも全塊で同じだけ遅れる）。false なら全部 `rst` をそのまま使う |
| `INJECTOR` | true | 故障注入（`swd_injector`）。`XTRIG` の一部。無ければ `xtrig` の `inject_status.result = 3`、`route.to_inject` は何もしない |

トップごとに渡し方が違う点（各トップの `generic map` から）:

- `RESET_SPLIT => true` を渡すのは `pynq_z2_fprobe_top` だけ（理由: 最悪の経路が `reset_sync` の出力の扇出約 3500 だった。トップのコメントが
  `docs/reports/fprobe-core-notes.md §2` を参照）。ほかのトップは既定（false）。
- `OBS_PORT` を渡すのは `pynq_z2_fprobe_top` だけ。`SYNC` を渡すのは `primer20k_fprobe_top` と `pynq_z2_fprobe_top`。
- `LA_INTERNAL`: `primer20k_fprobe_top`・`primer20k_fprobe_eth_top` は false（DDR3 の深いバッファ）、`primer20k_fprobe_serial_top`・`nano9k_fprobe_top` は true、
  `pynq_z2_fprobe_top` は `not DEEP`。
- `MEM_ADDR_BITS`: Primer の USB/Ethernet は 23、serial / Nano 9K は 0、PYNQ-Z2 は `MEM_BITS`。
- `XOUT_COUNT` と `LA_INPUT_COUNT` は `primer20k_fprobe_top` と `pynq_z2_fprobe_top` が渡す。eth・serial・Nano 9K のトップは渡さず既定（0）。

## 6. ホストリンクとトップ

`fprobe_core` は USB/UART/UDP を知らない。トップが「クロック・リセット・ホストリンク・クロック乗せ換えの FIFO」を用意してつなぐ。

| トップ | ホストリンク |
|---|---|
| `primer20k_fprobe_top`（`fprobe`・`fprobe_jtag`・`fprobe_core_m`・`fprobe_la`・`memtest`） | `usb/` の USB HS デバイス（ULPI クロックドメイン）。sys との間は `async_fifo` で乗せ換え |
| `primer20k_fprobe_eth_top` | `hostlink/eth_link` + `eth/`（RMII 50 MHz、UDP）+ `frame_mux` |
| `primer20k_fprobe_serial_top`・`nano9k_fprobe_top` | `hostlink/serial_link`（UART、COBS + CRC）+ `frame_mux` |
| `pynq_z2_fprobe_top` | `hostlink/zynq_frame_link`（PS の Linux の中継 `fprobe-relay` と AXI4-Lite で受け渡し）+ `frame_mux` |

`frame_mux` の論理チャンネル（フレーム = ch(1) | payload）: 0 = CONTROL、1 = DAP、2 = SWO、3 = UART0、4 = STREAM、5 = LINE_CODING（PC → FPGA のみ）。
送信の優先順は CONTROL > DAP > UART0 > SWO > STREAM。

## 7. 更新するとき

- CSR の定義は `gateware/fprobe_gw/csr/fprobe_map.py` が正。`core/generated/` はそこから `python -m fprobe_gw gen` で再生成される（直接編集しない）。
- 機能の有無の申告は `gateware/fprobe_gw/caps.py` と `designs.py`。generic を足したら、`render_package` の定数とトップの `generic map` も揃える。
- 規約: すべての entity に cocotb テスト（`gateware/tests/tb/*_tb.py` + pytest ラッパ）。信号はすべて明示的に駆動する（CLAUDE.md）。
