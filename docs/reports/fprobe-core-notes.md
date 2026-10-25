# fprobe_core の構造とタイミングの覚え書き（PYNQ-Z2 の sys クロック）

読んだもの: `gateware/build/fprobe_pynq/Default/{timing.rpt,util_hier.rpt}`、`fprobe_pynq_deep/Default/timing.rpt`（Vivado 2025.2、sys 100 MHz = 10 ns）。
合成・実機は走らせていない。数字はすべて上の報告から。効果は Vivado で測ること。

## 1. 構成ごとの有効な機能（fprobe_core の generic）

| generic | 外すと消えるもの |
|---|---|
| XTRIG / INJECTOR | クロストリガの CSR・割り込み書き込み（swd_injector）。窓は csr_absent |
| TRIG_SEQ | 多段トリガ（trig_seq・csr_trigseq） |
| SAMPLER / SAMPLER_MATCH | dap_sampler（DAP の命令の間に割り込む層）/ サンプラの一致 |
| HAS_JTAG / HAS_SWO_MANCHESTER | JTAG の PHY / Manchester の受信 |
| LA_INTERNAL / MEM_ADDR_BITS / EVENT_RING_BITS | 内蔵 BSRAM のロジアナ / 外部メモリ（深いバッファ）/ 内蔵のイベントリング |
| LA_FILTER / LA_PULSE / LA_INPUT_COUNT | ロジアナの前段 |
| AUX / SYNC / OBS_PORT | 補助マイコンの受け口 / 時刻の同期 / 対象の FPGA の観測 |
| XOUT_COUNT / UART_FLOW | クロストリガの出力の端子 / RTS・CTS |
| RESET_SPLIT（今回追加） | false = 従来と同じ回路。true = リセットの扇出を 5 塊に分ける |

外した機能の信号は generate の else 側ですべて明示的に駆動してある（EX1998 を出さない規約）。定数伝播で消えない書き方は見つからなかった。
ただし次の 2 つは「外した構成でも CSR が読み書きできる」ままになっている（記録として）。
- `csr_mem` と `csr_la_deep` は外部メモリが無い構成でも常に入る。値は読み書きできるが使い道が無い（PYNQ の util_hier で csr_mem は 0 LUT・130 FF、csr_la_deep は 3 LUT・135 FF）。ケイパビリティに申告が無いので、ホストが読まなければ害は無い。
- 面積は目標でなくなったので変更しない。

## 2. PYNQ の最悪の setup 経路 = リセットの扇出

| 構成 | WNS | 経路 | 配線 | 論理 | 扇出 |
|---|---|---|---|---|---|
| fprobe_pynq | +0.300 ns | `sys_reset_i/stage_reg[2]` → `core_i/trig_seq_g.trigseq_csr_i/stage3_count_r_reg[2]/R` | 8.603 ns（データ経路 9.059 ns のうち 95 %） | 0 段 | 3525 |
| fprobe_pynq_deep | +0.494 ns | `sys_reset_i/stage_reg[2]` → `core_i/sync_g.link_i/rx_data_reg[33]/R` | 8.294 ns（データ経路 8.750 ns） | 0 段 | 約 3500 |

- どちらも**論理は 0 段で、配線の遅れだけ**。reset_sync 1 本が数千の FF の同期リセットを駆動している。fprobe_core の外（トップの reset_sync）が出す線だが、負荷はほぼすべて fprobe_core の中にある。
- 論理段数や CSR の読み出しの多重化・ケイパビリティの合成が最悪になっているのではない（少なくとも報告に出ている最悪の 1 本はこれ。報告は各ドメインの最悪 1 本だけで、2 番目以降は載っていない）。
- ホールド（WHS）は +0.034 / +0.021 ns（イベントの値の FF → イベントリングの RAM。最小遅延の経路で、リセットとは別）。

### 提案と実装（RESET_SPLIT）

`reset_fanout`（新規、`rtl/common/reset_fanout.vhd`）: rst を 1 周期遅らせた同じ値の FF を 5 本作る。fprobe_core は塊ごとに別の本を使う。
- 塊: 0 = バス・CSR、1 = DAP・デバッグポート・サンプラ・モニタ、2 = SWO・UART・補助・同期・観測、3 = ロジアナ・トリガ・クロストリガ、4 = イベント記録・外部メモリ。
- 1 本あたりの扇出が約 3500 → 約 700 になる見込み（塊の大きさに依る）。配線 8.3〜8.6 ns が、扇出に概ね比例するとして数 ns に縮む見込み。**ただしこの経路が直ってもその次に悪い経路が新しい最悪になるので、WNS が何 ns 増えるかは報告からは言えない**（2 番目の経路が不明）。Vivado で測ること。
- 振る舞い: 全塊でアサートも解除も 1 周期遅れる。塊の間の関係は変わらない。入力のストリームはスライス（リセット中 ready = 0）が受けるので、解除が 1 周期遅れる間のデータは取りこぼさない。`mem_rst`（メモリ側のクロック）は別系統でそのまま。
- 同じ値の FF が合成で統合されないよう `syn_preserve`（Gowin）・`keep`/`dont_touch`（Vivado）を付けた。**統合されていないか Vivado の util で確かめること**（統合されると効果が無くなるだけで、動作は変わらない）。
- false（既定）は `rst_g <= (others => rst)` で、従来と同じ回路。Primer の構成は何も変わらない。
- 有効にしたのは PYNQ のトップ（`pynq_z2_fprobe_top.vhd` の generic map に `RESET_SPLIT => true` の 1 行）だけ。戻すときはこの 1 行を消す。
- 追加の FF は 5 個。

試験: `tests/test_fprobe_core.py` を RESET_SPLIT の false / true で走らせる（true は全部入り・sync・event_ring の 3 構成）。false の 9 構成は従来の試験そのもの。`tests/test_common_sync.py::test_reset_fanout` が reset_fanout 単体（全ビットが rst を 1 周期遅らせた値）を確かめる。

### 次に悪い所の見当（報告にある範囲）

過去の Gowin の報告（`docs/reports/2026-10-22-primer-resources.md`）の頭打ちは、故障注入の書き込み → event_data の調停（0.43 ns）、monitor → byte_lost（0.86 ns）、多段トリガ → 深いバッファの符号器、SWO の分周の設定 → 受信のタイマ。PYNQ（Vivado）でも同じ所が次に出る可能性がある。今回 event_data の調停を `event_collector` に切り出したので、この経路を直す（例えば源ごとに 1 段レジスタを置く）ときの手がかりになる。**今回は FF を増やすパイプライン化はしていない**（PYNQ deep は Fmax 106.5 MHz・TNS 0 を満たしており、リセット以外に明確な改善策が報告から言えないため）。

## 3. 構造の整理

- `event_collector`（新規 `rtl/core/event_collector.vhd`）: fprobe_core にあった「イベントの集約」（約 150 行の process 2 本と保持のレジスタ約 600 FF）を切り出した。DAP の転送・割り込み書き込み・サンプラ・UART/SWO のバイト・補助・観測・同期の 7 種の源を受け、1 周期 1 件で event_valid / event_data を出し、取りこぼしを byte_lost に数える。
  - 振る舞いは切り出し前と同じ（process の本体は文字どおり移しただけで、信号名の付け替えのみ）。fprobe_core の既存試験（9 構成）が切り出し後も通る。
  - 新しい源を足すときは、この entity に valid とデータのポートを足し、保持と優先順を 1 か所に書けばよい（fprobe_core の中の他の場所は触らない）。
- fprobe_core.vhd: 1395 行 → 約 1270 行（リセットの塊分けの追加を含む）。
- 切り出さなかったもの（リスク対効果）: クロストリガの出力の配線（xout_g）、外部メモリ・深いバッファ（memory）、CSR 窓の接続。どれも generic ごとの分岐が絡み、portを大量に持つ entity になる割に、新機能を足しやすくならない。次に切り出すなら、(a) `xtrig_out` まわり、(b) 外部メモリまわり、の順。

## 4. 不具合の疑いの調査

読んで気づいたこと。確定した不具合は無い。
- `lost_n`（`natural range 0 to 7`）は同じ周期に落ちうる源の数の上限と一致している（サンプラ・補助・観測・同期 + バイト 3 = 7）。源を足すと範囲を越えるので、`event_collector` に源を足すときは範囲を広げること（越えると実行時のエラーかシミュレーションの停止）。
- `events_lost` は `ev_lost + byte_lost` で 32 ビットの折り返し。実用上は起きない。
- イベントの時刻は源ごとに 1 周期ずれうる（バイトは 1 段レジスタで受けるため。コメントに記載済み）。
- 外部メモリの無い構成でも `csr_mem` / `csr_la_deep` は読み書きできる（1 章）。
- `obs_g` の `sample_delay => 0` は固定（ponytail のコメント付き）。遅れて届く対象で必要になる。
- `uart_rts_pin` / `uart_cts_pin` は UART_FLOW に依らず CSR の値を出すが、claim は UART_FLOW で止めているので端子は動かない（問題なし）。

## 5. 後で Vivado で確かめること

1. `fprobe_pynq` / `fprobe_pynq_deep` の最悪経路が変わったか（WNS・次の最悪の経路）。
2. `reset_fanout` の 5 本が統合されていないか（`util_hier.rpt` の `reset_fanout_i` に FF 5 個）。
3. 追加の FF 5 個、event_collector 切り出しでの資源の差（同じはず）。
4. 合成の警告に新しい EX1998 や属性の警告が出ていないか（`keep` / `dont_touch` / `syn_preserve`）。Gowin 側はこの generic が false なので reset_fanout は使われないが、ソース一覧には入っている。
