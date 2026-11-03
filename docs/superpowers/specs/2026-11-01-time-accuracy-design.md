# 共通タイムスタンプの時刻の正しさと分解能を、理論の限界まで上げる

2026-11-01。状態: §6 は主担当が判断した（§6 の「主担当の判断」）。段階 S0（ホストだけ）は実装済み（§8）。段階 S1（R1・R2・R3・TLV TIMING）は
実装済み（§10。2026-11-02。シミュレーションまで、実機は未確認）。全数点検の結果と段の割り当ては §9（2026-11-02）。
段階 S1b（§9 の S1b の項目と、辺と時刻の約束の 1 刻み）は実装済み
（§11。2026-11-02。シミュレーションまで、実機は未確認）。段階 S2（DAP。R5・R6・R7）は実装済み（§12。2026-11-02。cocotb まで、実機は未確認）。
段階 S3（R4・R9・B2・B3・B4・N6・clockmap）は実装済み（§13。2026-11-02。cocotb まで、実機は未確認）。対象は全機能の構成（PYNQ-Z2、Zynq-7020 -1。`fprobe_pynq_deep`・`fprobe_pynq_cap`）。
上位の方針: `2026-10-29-performance-limits-study.md`（利用者が承認済み）の 2.3（時刻の正しさと分解能）・2.5（停止の同時性）・2.9（非干渉性）。
関係: `2026-11-01-stop-precision-design.md` §3.3（「1 回の書き込みが端子に出てから時刻が付くまでの差の上限」はこの文書の表の 1 行）。
**範囲の分け方**: ロジアナの取り込みそのもの（ISERDES・速い領域でのサンプリング・入力の制約）は、同時に書かれている
`2026-11-01-la-resolution-design.md`（ロジアナの分解能）が扱う。この文書は「各情報源の時刻が何を意味し、どれだけ正しいか」と、
細かい位置（ISERDES の位置・TDC）を**共通の時間軸にどう載せるか**だけを扱う。

この文書の数値は、断りの無いかぎり RTL を読んで出した**導出**（シミュレーション・実機では未確認）。推測は「推測」と書く。

## 1. なぜ

fprobe の最優先は解析力（ロジアナとデバッガの一体化・共通タイムスタンプ・クロストリガ）。「なぜ壊れたか」は、別々の情報源の
出来事の**前後関係と間隔**で決まることが多い（ピンの変化と変数の書き込みのどちらが先か、割り込みの書き込みが何 ns 後に効いたか）。
いまの共通タイムスタンプは「1 本の 64 ビットのカウンタ（`timebase.vhd`、sys 10 ns）を全情報源が使う」ところまでで、
**各情報源の時刻が、本当の出来事のどの瞬間から何刻みずれているか**は、ロジアナとボード間の同期を除いて申告していない。
AI は道具の報告をそのまま信じるので、ずれを知らずに前後関係を読み違えるのが最も危険（CLAUDE.md の原則）。

## 2. 情報源ごとの「時刻が付く瞬間」と「本当の出来事の瞬間」（いまの RTL）

### 2.0 読み方

- **時刻が付く瞬間**: イベントの [47:0]（`event_collector.vhd:111-130`）や CSR の時刻のレジスタに入る `time_count` の値が、どの周期のものか。
  `time_count` は sys の毎周期 1 増える（`timebase.vhd:35-49`）。以下「刻み」= sys の 1 周期（PYNQ 10 ns。`pynq_z2_pkg.vhd:12-19`）。
- **本当の出来事の瞬間**: その情報源で「起きたこと」と呼ぶべき瞬間。プローブから見える範囲（端子の上）と、対象の中（プローブから
  見えない）を分けて書く。
- **差の種類**: 固定（回路で決まる定数・設定で決まる式）／ばらつく（その時々で変わる。上限があるかを書く）／不明（プローブの作りからは
  決まらない）。
- 共通の段: イベントの集約（`event_collector.vhd`）は、DAP・割り込み・サンプラ・バイト・補助・観測・同期の各源を**受けた周期の**
  `time_count` を取る（`event_collector.vhd:114-139, 160-167, 204-211, 216-222`）。保持して後の周期に出すので、出力の待ちは時刻に入らない。
  ただしバイト（UART・SWO）は 1 段のレジスタで受けるので +1（`event_collector.vhd:90, 94-98`）。
- **版の注（2026-11-02）**: §2.1 の表は S0 の時点（2026-11-01）の回路を基にしている。その後の点検・段で、3（N1）・6（N4）・16（N3・B7）を
  直し、22・23 を足した（§9.5・§11・§12）。S1・S2 で回路が変わった行の今の意味は、表ではなく各段の節にある: 2・4・11・12・13 は §10.1
  （R1・R2・R3）、5・8・9・10 は §12.1（R5・R6。DAP_LINE_TIME の回路では時刻が線上の点）。上の「共通の段」（集約が受けた周期の
  `time_count` を取る）も、S1 の後の UART 受信・SWO と S2 の後の DAP・割り込みでは源が運ぶ時刻を使う（`event_collector.vhd:9-10, 111-130`）。
- **行番号の版**: `fprobe_core.vhd`・`event_collector.vhd` のうち、イベントの形式・`la_probe_inputs`・クロストリガ・多段トリガ・UART の
  受け渡し（§9.1 の D3 の項目）は 87bb626e（S2・S1b・段 B の統合）の版で照合した。それ以外の行番号は 21efa776（S1b）の版のままで、
  S2 で変わったファイル（`event_collector`・`swd_phy`・`jtag_phy`・`debug_port`・`dap_sampler` 等）はずれている所がある（照合していない）。

### 2.1 表

| # | 情報源 | 時刻が付く瞬間（いま） | 本当の出来事（端子の上 / 対象の中） | 差（式・段数） | 種類 | 根拠 |
|---|---|---|---|---|---|---|
| 1 | ロジアナ 内蔵（sys、外のピン） | トリガ: `la_capture` が hit を受けた周期。各サンプルはトリガからの番号 × (rate + 1) | ピンの電圧がしきい値を横切った瞬間 | フィルタあり 3 + `la_front` → `la_capture` 3 = 6 刻み（申告 `LA_DELAY`）＋ 位相の幅 1 刻み（[0, 1) のどこか）＋ rate > 0 ならサンプル間隔の窓 | 固定 + 位相 1 刻み。端子 → 最初の FF の配線は**不明**（IOB・入力の制約が無い。ch ごとに違う） | `fprobe_gw/timing.py`（`LA_FILTER_DELAY`・`LA_INTERNAL_DELAY`・`la_delay_ticks`）、`la_front.vhd:126-128, 145-146, 184-186`、`la_capture.vhd:165-172`、`la.py:336-339` |
| 2 | ロジアナ 内部の ch（SWDIO を駆動中・DAP 処理中） | 同上 | sys の FF の出力が変わった周期 | **フィルタを通らない**（S1 の前の `fprobe_core`。いまは `la_probe_inputs` が揃える）ので 3 刻み。ホストは全 ch に 6 を引く（`la_delay_ticks` は ch を区別しない） | 固定。**ピンの ch と 3 刻み食い違う**（導出・未確認）。（S1 の前の）`fprobe_core` の注「全 ch が同じだけ遅れる」は外のピンの中だけで成り立つ | `fprobe_core.vhd:837-846`（`la_probe_inputs`）、`fprobe_gw/timing.py` の `la_channel_timing` |
| 3 | ロジアナ 取り込みの領域（cap 300 MHz、`fprobe_pynq_cap`） | トリガ: cap の周期の番号 n = time × RATIO + phase − latency（sys の辺 T は cap の辺 T × RATIO。S1b で cocotb `the_cap_cycle_number_is_ratio_times_the_timebase`） | 同 1 | latency = 5 − RATIO（cap の周期。試験で確認済み）＋ 位相の幅 1 cap 周期。ピンは辺 n の後に変わった（n 以上 n + 1 未満） | 固定。sys 由来の内部 ch（`sys_a_c`・`sys_b_c`）は `la_probe_inputs`（INTERNAL_AGE 1）でピンのフィルタの段に揃えてあり、ずれ 0（S1 の R2。S1b の N1 で cocotb `internal_channels_land_on_the_same_sample_as_a_pin_changed_on_the_same_edge` を RATIO 2・3・4 で確かめた。LA_FILTER = false の構成も同期化と揃えの段は同じ） | `la_cap_domain.vhd:12-23, 191-195`、`la.py:292-299` |
| 4 | ロジアナ 深いバッファ（`fprobe_pynq_deep`） | トリガ: 符号器が最初の triggered を出した周期（`LA_DEEP_DELAY` 4）。**連続記録のサンプル 0 の時刻は回路に記録が無い** | 同 1 | トリガは 3 + 4 = 7 刻み＋位相 1。連続記録のサンプル 0: ホストが「トリガ時刻が CSR の読みの前後に入っていればそれ、余裕 4 サンプル」か「CSR の往復の中点 ± 半分」で**推定**している | トリガは固定。連続記録の起点は**ばらつく（往復 約 290〜380 µs の窓）**。余裕の 4 サンプルは経験則（CLAUDE.md の原則に反する） | `la_deep_buffer.vhd:11, 309-311`、`recording_stream.py:34, 61-72` |
| 5 | DAP の転送（ホスト、SWD） | PHY の完了（最後のビットの High 相の終わり。アイドル相があればその終わり）の 2 刻み後（`debug_port` 1 段 + `dap_monitor` 1 段） | 端子: ヘッダの始め・ACK・データ相の終わり。対象の中: AP / DP のアクセスの開始（読みはヘッダの後、書きはデータ相の後。ADIv5 の規定。推測・要確認） | 完了 − 線上の点 = 設定（hp・turnaround・idle_cycles・sample_delay・data_phase）と ACK・RnW で決まる式（1 ビット = 2·hp 刻み。`swd_phy.vhd:207-218, 269-303`）。ホストはこれを計算していない | 端子までは固定（設定ごと）。対象の中の遅れは**不明** | `swd_phy.vhd:290-329`、`debug_port.vhd:97-112`、`dap_monitor.vhd:110-124`、`event_collector.vhd:114-117` |
| 6 | DAP の転送（ホスト、JTAG） | 同 5（完了の 2 刻み後） | 端子: その DR スキャンの Update-DR を出る TCK の立ち上がり（JTAG-DP はここでアクセスを始める） | （2026-11-02 に訂正。§9.5 の N4・§12.3）IR のスキャンは DR の前なので、IR を省くかで変わるのは「開始 → Update-DR」だけ。Update-DR を出る立ち上がり E_u から done の辺まで = hp + 1 + 2·hp·idle_cycles、DP の読み（RDBUFF・IDCODE 以外、ポストの読みでない）で ACK OK なら続く RDBUFF のスキャンの 2·hp·(40 + dr_before + dr_after) + 5 を足す（導出。cocotb では確かめていない: S2 は点を直接取るのでこの式を使わない） | 設定と要求・ACK で決まる固定（履歴に依らない）。1 ビット = 2·hp、区間に入るたびに +1 周期（stop-precision §3.1 の後） | `jtag_phy.vhd:338-382`、stop-precision §2 |
| 7 | ポストされた AP の読みの値（ホスト） | 値が**返った**転送（次の AP の読みか RDBUFF）の時刻（`resolve`） | 対象のバスで読んだ瞬間 = 読みを頼んだ転送の後、返した転送の前のどこか | 窓 = [頼んだ転送のアクセス開始, 返した転送の ACK]。いまは窓の後端だけを付けている | ばらつく（転送 1〜数回分）。窓の両端はイベントの列に**すでにある** | `events.py:94-137`（特に 122-128） |
| 8 | サンプラの値（種類 7） | 語 i: 次の DRW 読みの完了 + 2。**最後の語: RDBUFF の後の CTRL/STAT（確認）の完了 + 2** | 対象のバスで読んだ瞬間（DRW 読み i を頼んだ後） | 語 i は 1 転送、最後の語は 2 転送（RDBUFF + CTRL/STAT）遅れて付く。1 語のか所（見張りの普通の使い方）は**読みを頼んでから 3 転送分後**。WAIT の繰り返しで延びる | ばらつく（WAIT）。窓の始め（読みを頼んだ転送）は回路の中にあるが、イベントに載っていない | `dap_sampler.vhd:279-293, 459-476` |
| 9 | サンプラの値の一致（`fire_time`・`prev_time`） | 標本を受けた周期（= 8 と同じ） | 8 と同じ | 8 と同じ | 8 と同じ | `sampler_match.vhd:9-13, 85` |
| 10 | 割り込み書き込み（種類 11、`inject_first_time`・`inject_time`） | DRW 書き込みの完了 + 2 | 対象のバスに書いた瞬間（データ相の後） | 完了 − データ相の終わり = 5 と同じ式 | 端子までは固定。対象の中は不明 | `swd_injector.vhd:256-281`、`fprobe_core.vhd:1104-1109`、`event_collector.vhd:118-121` |
| 11 | SWO（UART 方式） | 停止ビットの中央の判定の周期 + 1（`out_ready = '1'` 固定なので待ちは無い） | 端子: 開始ビットの立ち下がり。対象の中: ITM に書いた瞬間 | 端子の辺 ∈ [t − D, t − D + 1)、D = 9N + ⌊N/2⌋ + 4（N = divisor。導出: 同期 2 + 待ちの判定 + 半ビット + 9 ビット + 受け渡し 1） | 端子までは固定。ITM → TPIU の FIFO の遅れは**不明** | `uart_rx.vhd:41-55, 76-108`、`swo_receiver.vhd:60-63, 80-88`、`event_collector.vhd:96, 216-222` |
| 12 | SWO（Manchester） | 8 ビット目の中央の変化の検出 + 同期 2 + 1 | 同 11（端子は開始ビットの立ち上がり） | 開始の辺から 8.5 ビット（**送り手の実際のビットの長さ**）+ 定数。受信器はビットごとに同期し直すので N ではなく実際の速さで決まり、その速さは記録されない。1 フレームの 2 バイト目以降はさらに 8 ビットずつ | **ばらつく（送り手の速さ）**。対象の中は不明 | `swo_manchester_rx.vhd:9-17, 52-56, 100-107` |
| 13 | UART 受信（種類 3） | `uart_out_valid and uart_out_ready` の周期 + 1 | 端子: 開始ビットの立ち下がり | D = 9N + ⌊N/2⌋ + 4 + w。**w = ホストへの経路が受け取るまでの待ち**（記録されない。上限は次のバイトが揃うまで ≈ 10 ビット、そこで overrun） | **ばらつく（w）** | `fprobe_core.vhd:1366`、`uart_rx.vhd:62-63, 100-108`、`uart_bridge.vhd:178-181` |
| 14 | UART 送信（種類 4） | 受け渡しの周期 + 1 = 送信器がフレームを積んで開始ビットを出し始める周期 | 端子: 開始ビットの立ち下がり | 0 刻み + 出力の遅れ（OBUF・配線） | 固定（出力の遅れは未測定） | `uart_tx.vhd:54-57, 71`、`fprobe_core.vhd:1368` |
| 15 | 補助マイコンの計測値（種類 6） | フレームを受け終えた周期 | Pico の ADC が変換した瞬間 | Pico の処理と UART の 1 フレーム | **不明** | `fprobe_core.vhd:1171-1194`、`event_collector.vhd:160-167` |
| 16 | クロストリガ | 源のパルスの周期 S（= その源の時刻: match_time・seq_time 等）→ `la_ext`・`inj_request` は 1 段（S + 1。`fprobe_core.vhd:1108-1119`）。ロジアナ（S1b の N3）: 外部トリガを `la_front`（EXT_DELAY = 2 + probes の前の段 − 外部トリガの前の段）・`la_lane_front`（レーン IN_AGE mod N）で遅らせ、`la_ext` が 1 の周期の頭の後に変わったピンのサンプルに付ける。割り込みは PHY の境界を待つ（実際の遅れは `inject_first_time` − 源の時刻で記録済み）。出力の端子は端子の条件から 4 周期（PYNQ は 5。D2） | 源の出来事 → 動作 | ロジアナ: トリガのサンプルのピンの時間軸の時刻 = S + 1（TIMING `la_ext`）、rate > 0 なら + 0〜rate 取り込み周期の待ち（ホスト `timing.ext_trigger_window`）。S1b の前は外部トリガを遅らせず、トリガ時刻がピンの段数を引くと `la_ext` の辺より前（sys・フィルタあり 5 刻み、なし 2、cap 4 cap 周期、レーン約 3 sys + IN_AGE）になっていた。逆向き（ロジアナのトリガを源にしたとき、S1b の B7）: sys の内蔵 S = trigger_time + 1、深いバッファ S = trigger_time（詰め pack ≠ 0 は trigger_time 自体がトリガのサンプルより 1 + (k − 1 − j)(rate + 1) 周期遅い）、取り込みの領域 S × RATIO − n ∈ [8, 7 + RATIO]、1 周期に複数のサンプル S × N − n ∈ [latency + 1, latency + N]（TIMING `la_xtrig`・`la_deep_xtrig`・`la_deep_packed`・`la_cap_xtrig`・`la_lanes_xtrig`）。割り込み: ばらつくが記録あり | 固定 / 記録ありのばらつき（rate の待ち・詰めの語の中の位置は範囲で返す） | `fprobe_core.vhd:1108-1133`、`la_front.vhd`、`la_lane_front.vhd`、`la_cap_domain.vhd`、`la_pack.vhd:37-39, 66-69`、`xtrig_out.vhd:13` |
| 17 | 対象の FPGA の観測（種類 8・9） | 標本型: Capture-DR を出る TCK の立ち上がりを見た周期。印: Update-DR を出る立ち上がり。記録: 受けた周期 | 端子: その TCK の辺。対象の中: 観測の回路が値を取った瞬間 | TCK の内部のレジスタが変わった次の周期で見る（+1、導出）。対象の中の遅れは観測の回路が `SYNC_DELAY` で申告 | 固定 + 対象の申告 | `obs_jtag_engine.vhd:14-21, 334-346`、`obs_record_rx.vhd:171` |
| 18 | ボード間の同期（種類 10） | 子: `sync_in` が 1 になった周期 + 2。親: 決めた周期 + 1 で線が 1 | 線の辺 | RX 2・TX 1・位相の幅 1 刻み（申告済み）。**配線の遅れは未測定（None）**。組の残差は返す | 固定 + 位相 1 + 不明（配線） | `sync_link.vhd:16-18`、`fprobe_gw/timing.py`（`SYNC_*`）、`timesync.py:1-51` |
| 19 | PC 側の出来事・CSR で読む時刻 | CSR の読みを FPGA が処理した周期（`device.time()`） | PC で起きた瞬間 | [前の読み, 後の読み] の窓（往復 約 290〜380 µs） | ばらつく（往復） | `recording.py:97`、performance-limits-study §1 |
| 20 | 対象の Linux の時刻（CLOCK_MONOTONIC_RAW） | 対象の ns → PC の時計（往復の中点の組の内挿）→ fprobe の刻み（19 の組） | 対象の中の出来事 | 往復の窓が 2 回 | ばらつく（往復 2 回分） | `linux/clock.py:1-28` |
| 21 | タイムベースそのもの | — | 秒 | 公称 100 MHz（125 MHz × 48 / 5 / 12。2026-11-01 の la-resolution 段 2 から。その前は × 36 / 5 / 9）。発振器（`H16` の 125 MHz）の誤差・温度変化は未測定。秒への換算に ppm がそのまま乗る | **不明（未測定）** | `pynq_z2_pkg.vhd:10-19`、`fprobe_gw/boards/pynq_z2.py:47, 71-74` |
| 22 | 多段トリガの発火の時刻（CSR `trigseq.fire_time` = `seq_time`。S1b の A2） | fire が 1 の周期の time（`fprobe_core.vhd:1063-1081`） | 最後の段を進めた事象: ピンの変化・内部の FF の辺・match / host のパルス | trig_seq の入り口から 4（同期化 2 + 判定 1 + 段の更新 1）、match・host は 2。入り口の前の段: sys の構成のピンはグリッチフィルタ 3（内部の ch は揃えの 3）、取り込みの領域・1 周期に複数のサンプルの構成は素の値（0）。1 周期に複数のサンプルの専用の入力は ISERDESE2 の最も新しいレーン（細かい辺 N × w − IN_AGE）を sys の値として受けるので 細かいサンプルで seq_time × N − n ∈ (4N + IN_AGE, 5N + IN_AGE] | 固定 + 位相 1 刻み（ピン）/ 0（内部・事象）。どの事象が進めたかは記録されない（最後の段の条件の事象を全部並べて返す） | `trig_seq.vhd:89-131`、cocotb `trig_seq_tb.the_fire_cycle_is_the_table_delay_after_the_event`、TIMING `seq_pin`・`seq_internal`・`seq_input_lanes`・`seq_event` |
| 23 | ロジアナ 1 周期に複数のサンプル（`fprobe_pynq_fast`・`fprobe_pynq_deep_fast`。S1b の A9） | 内蔵: n = trigger_time × N + trigger_lane − latency（latency = 4N + IN_AGE）。深いバッファ: n = start_time × N + s × 間隔 − latency（latency = N + IN_AGE） | 同 1（細かいサンプル 1.25 ns） | 細かい ch（ISERDESE2）は軸そのもの。内部の ch・sys で取るピンは `la_lane_inputs` がレーンごとに選んだ sys の周期の値を入れて軸に載せる: 内部の ch ずれ 0・幅 0、sys で取るピン ずれ −1 刻み・幅 1 刻み（S1b の N2。それまでは全レーンに今の値で、内部の ch が N + IN_AGE 細かいサンプル早く見えた） | 固定 + 位相 1 細かいサンプル | `la_lane_inputs.vhd`、`la_lane_capture.vhd`、`la_lane_encoder.vhd:7-9, 45`、cocotb `la_lane_inputs_tb`・`la_lane_e2e_tb`、TIMING `la_lanes`・`la_deep_lanes` |

### 2.2 表から分かること

1. **時刻がプローブの出す辺に付く情報源（DAP・割り込み・UART 送信・観測）は、sys の格子の上で正確に決まる。** 量子化の誤差は 0 で、
   残るのは出力の遅れ（ns 未満〜数 ns）と、対象の中の遅れ。出力の遅れの扱いはボードで違う（2026-11-02 に訂正。§9.1 の A3）:
   PYNQ-Z2 の JA の出力は IOB の FF（`pynq_z2_fprobe_top` の `pad_o`/`pad_t`）で出すので、時刻の周期から端子まで +1 刻みの固定の段と、
   FF → 端子の部品の遅れ（未測定）になる。stop-precision §3.1 で JTAG の出力（TCK・TMS・TDI）を IOB の FF にした（SP §6 のビルドで
   JA の 8 本が OLOGIC に入った: OLOGIC 8）。DAP の端子の段は S2 で TIMING の `dap_swd`・`dap_jtag` に申告している（§12.1）。
   Primer は端子の段 0（§9.1 の A3）。UART 送信・クロストリガ・同期の出力の端子の段は S3（R9）で扱う。
   S0 の時点で失っていたのは「完了の時刻しか付けていない」ことによる**設定に依存した数 µs のずれ**（5・6・8・10）。
   → S2 で線上の点にした（§12.1。DAP_LINE_TIME の回路）。
2. **非同期の入力（ロジアナのピン・UART 受信・SWO・同期の線）は、位相の幅 1 刻み（10 ns）が下限**で、分解能を上げる意味があるのはここ。
3. **経験則・推定が本体に残っている所**: 深いバッファの連続記録の起点（4。余裕 4 サンプル・往復の中点）。→ S0 で除いた（§8）。
4. **申告と回路が食い違う所**: ロジアナの内部の ch（2）。→ S0 でホストが ch ごとに直した（§8）。
5. **ホストだけで今日直せる所**: ポストされた読みの窓（7。両端はイベントの列にある）。→ S0 で付けた（§8。下限は頼んだ転送ではなく
   その前の転送）。
6. **記録されないばらつき**: UART 受信の待ち w（13）、Manchester の実際のビットの長さ（12）、サンプラの WAIT（8）。
   どれも回路の中では分かっている。（2026-11-02 に訂正: 初版はここに「JTAG の IR の省略（6）」も挙げていたが、IR の省略は Update-DR
   より前だけを変え、Update-DR から完了までは設定と要求・ACK で決まる固定（§9.5 の N4・§12.3）。S1 の R3 で w は除いた（§10.1）、
   S2 の R6 で WAIT の回数は記録に入れた（§12.1）。）

## 3. 理論の限界

### 3.1 タイムベースの分解能

| 方法 | 1 刻み | 何に効くか | 根拠・注記 |
|---|---|---|---|
| sys（今） | 10 ns | — | `timebase.vhd` |
| 取り込みの領域の位相（今ある） | 3.33 ns（cap 300 MHz） | ロジアナ（内蔵）だけ | `la_cap_domain.vhd:12-16` |
| 1 本のクロックの上限 | 2.16 ns（BUFG 464 MHz） | 同上 | DS187（performance-limits-study §0.1） |
| ISERDES の位置 | 約 1.05 ns（-1 の端子の 950 Mb/s） | ISERDES に載せたピン | 同上。取り込みは la-resolution の担当 |
| IDELAY で位相をずらす | 約 78 ps（1 段） | 補正（ch の間のずれを揃える）に使う。分解能そのものではない | DS187、-1 は基準 200 MHz |
| キャリーチェーンの TDC | 10〜30 ps 級（推測: 7 系列の公表例の一般的な値。ここでは測っていない） | 選んだ数本の入力 | 段の幅が不揃いで、較正（符号の密度）と温度・電圧の追従が要る |

**64 ビットのカウンタそのものを速い領域に移す必要は無い**（私の判断）。sys に同期した情報源（プローブの出す辺）は 10 ns の格子で
正確で、細かくしても情報は増えない。細かさが要るのは非同期の入力だけで、それは「刻み + 刻みの中の位置」（la_cap_domain の
`phase` と同じ形）で表せる。sys を 100 MHz から上げても（2.10）、この形のまま `timebase_hz` が変わるだけ。

### 3.2 各情報源の不確かさの下限

| 情報源 | プローブの作りで下げられる下限 | それ以上は下がらない理由 |
|---|---|---|
| ロジアナのピン | ISERDES 約 1 ns → TDC 数十 ps。ch の間のずれは IDELAY で揃え、揃えた後の残りを測って返す | 入力の緩衝器のゆらぎ、基板・プローブの線の長さ（約 5 ns/m。測って入れる値） |
| DAP・割り込み（端子の上） | 0 刻み（sys の格子で正確）＋ 出力の遅れ（IOB にすれば部品の値で固定。数 ns 以下） | 線の長さ |
| DAP・割り込み（対象の中のアクセス） | 窓 [線上のアクセス開始点, 次に WAIT しうるアクセスの ACK OK]（ADIv5: 前のアクセスが終わっていなければ次は WAIT になる。推測・要確認）。窓の両端はプローブが知る | 対象の DAP の中のクロックの乗せ換え（対象のバスのクロック数周期）。プローブからは見えない |
| サンプラの値 | 窓 = [DRW 読み i のアクセス開始, 値を返した転送の ACK]。1 転送の長さ（JTAG 20 MHz で約 2 µs）が窓の幅の下限 | ADIv5 の読みはポスト（値が後の転送で返る）。窓を狭めるには TCK を上げる（stop-precision §3.1・§3.8） |
| UART・SWO（端子の上） | 1 刻み（位相）。開始の辺に時刻を付ければ divisor・送り手の速さに依らない | 位相（ISERDES に載せれば約 1 ns） |
| UART・SWO（対象の中） | 下げられない（対象の FIFO）。対象の時計の時刻（ITM の時刻の記録）との対応付けで扱う（§4.6） | 対象の実装 |
| ボード間の同期 | 位相 1 刻み（細かい位置を載せれば約 1 ns）＋ 配線の遅れ（往復で測れば片道の差だけが残る） | 往復で測るとき「行きと帰りの遅れが等しい」は仮定（結果に書く） |
| 秒への換算 | 外の基準（GPS の 1 秒の印など）で発振器の誤差を測れば、その測定の精度まで | 基準が無ければ ppm は不明のまま |

## 4. 根本の設計

### 4.1 原則（4 つ）

1. **時刻は、出来事に最も近い回路の点で取り、結果と一緒に運ぶ。** 遅れて出る結果（完了・ポストされた読み・停止ビットの後のバイト）に、
   早い点で取った時刻を載せる。観測の口（`obs_jtag_engine` の `cap_time`・`upd_time` を `ev_time` で運ぶ）がすでにこの形で、
   それを全情報源に広げる。集約（`event_collector`）は `time_count` を自分で取らず、源の時刻を使う。
2. **残る差は、作りから決まる定数と式として一か所（時刻の表）に持つ。** 推定でなく段数・設定・規格で決まる値。
3. **分からない部分は「分からない」と、項目の名前で返す。** 0 や見込みの数値で埋めない（`timesync.to_parent` の `cable_delay: None` と同じ）。
4. **判断しない。** どの窓が「正しそうか」は選ばず、窓の両端と、それを決めた根拠をそのまま返す。仮定を使う計算（対象の時計の直線など）は、
   仮定を結果に書き、仮定が破れたら（どの直線も窓をすべて通らない等）それも事実として返す。

### 4.2 時刻の表（一か所に持つ）

- 正: `gateware/fprobe_gw/timing.py`（新設。CSR の `fprobe_map.py`・ディスクリプタの `fprobe_device.py` と同じ扱い）。
  情報源ごとに「時刻を取る点の名前」「その点から時刻までの固定の段数（刻み、または取り込みのクロックの周期）」「位相の幅」
  「含まない項目（出力の遅れ・対象の中・配線）」を持つ。いまの `LA_FILTER_DELAY`・`LA_INTERNAL_DELAY`・`LA_DEEP_DELAY`・
  `SYNC_*_TICKS`（S0 の前は `caps.py`）と `la_cap_domain` の latency はここへ移す（S0・S1 で移した）。
- `python -m fprobe_gw gen` が生成する: VHDL のパッケージ（RTL の assert と試験が使う）、ホストの `generated.py`、
  ケイパビリティの TLV（新しい種類 TIMING: 情報源ごとの組。古い TLV LA_DELAY は残して同じ値を出す）、レジスタ表と同じ形の文書の表。
- **デバイスが申告し、ホストはそれを使う**（PC 側はデバイスが申告するケイパビリティを見て動く、の原則）。TIMING の無い古いデバイスには、
  ホストが「この文書の §2 の、いまの意味」を版ごとに持つ（推定ではなく、その版の回路の事実）。
- 設定で変わる式（SWD・JTAG の転送の中の位置、UART の divisor）はホストの関数にする（`fprobe/timing.py`）。cocotb の試験はこの
  ホストの関数を import して RTL の振る舞いと比べる（ゲートウェアの試験がホストの符号化・解析を使う、の既存の約束）。表と回路と
  ホストの式が食い違えば試験が落ちる。

### 4.3 RTL の変更（情報源ごと）

| 変更 | 内容 | 効く行（§2.1） |
|---|---|---|
| R1 連続記録の起点 | `la_deep_buffer` が run の後の最初のサンプル（`la_front` の sample_valid）の `time` を `start_time` として持ち、CSR で読める・ストリームの最初の見出しに載せる。ホストの推定（`TRIGGER_SLACK_SAMPLES`・往復の中点）を除く | 4 |
| R2 内部の ch の段数 | フィルタを通らない内部の ch に、フィルタと同じ段数の遅れを入れて全 ch を揃える（FF 2 本 × 3 段）。取り込みの領域（`la_cap_domain`）も同じ考えで揃える。揃えられない構成は TIMING に ch の種類ごとの段数を出す | 2・3 |
| R3 UART 受信の時刻 | 受け渡し（valid and ready）ではなく、開始ビットの辺を見つけた周期（`uart_rx` の S_IDLE → S_START）の時刻を取ってバイトと一緒に運ぶ。SWO の UART 方式も同じ部品なので同時に直る | 11・13 |
| R4 Manchester の時刻 | バイトごとに、その最初のビットの中央の変化の周期の時刻を運ぶ（開始の辺から数えない: 送り手の速さに依らない） | 12 |
| R5 DAP の時刻を線上の点で | `swd_phy`・`jtag_phy` が、転送ごとに線上の決まった点の時刻を `rsp_time` として応答と一緒に出す（SWD: ACK の 3 ビット目を出す Low 相の終わり = 次の SWCLK の立ち上がりの周期。JTAG: その転送の DR スキャンの Update-DR を出る TCK の立ち上がり）。`debug_port` は応答と同じ 1 段で運ぶ。`dap_monitor`・`swd_injector`・`dap_sampler` はそれを使い、集約は源の時刻を使う。完了との差は設定で決まる式（4.2）になり、JTAG の IR の省略の履歴に依らない | 5・6・10 |
| R6 サンプラの窓 | 語 i のイベントの時刻を「DRW 読み i の `rsp_time`」（読みを頼んだ点 = 窓の始め）にする。値を返した転送の WAIT の回数を、いま定数の [119:115]（`event_collector.vhd:127` の `'0' & "1111"`）に入れる（31 で飽和。飽和したら窓の後端は不明として返す）。窓の後端 = 始め + 設定で決まる転送の長さ × (1 + WAIT の回数) になる。`sampler_match` の `fire_time`・`prev_time` も同じ時刻にする（停止の見張りの報告の根拠が変わるので、stop-precision の 3.3 と一緒に直す） | 8・9 |
| R7 PHY の設定のイベント | SWJ_Clock・TransferConfigure・JTAG の鎖の設定・sample_delay が変わったら、その値をイベント（新しい種類）に出す。pyOCD 等ほかの道具が設定を変えても、記録だけで 5・6 の式が計算できる | 5・6・8・10 |
| R8 細かい位置 | 非同期の入力の源（ロジアナのトリガ、同期の線、ISERDES に載せたピン）に「刻みの中の位置」を付ける。イベントの空き（同期の [119:112]、バイトの [119:112]）に入れる。位置の単位（1 刻みの分割数）は TIMING で申告。取り込みの回路は la-resolution の担当で、この文書は表し方だけを決める | 1・3・18 |
| R9 出力の IOB | SWCLK/TCK・SWDIO/TMS・TDI・UART TX・同期の出力を IOB の FF で出す（stop-precision 3.1 と同じ）。出力の遅れが配置に依らず、部品の値で固定になる | 5・6・10・14・18 |

### 4.4 イベントの形式

いまの 128 ビットの形式（`event_collector.vhd:97-110`）は全部のビットが使われているが、上の変更は新しい欄を足さずに入る:
R5 は時刻の意味を変えるだけ、R6 はサンプラの定数のビット、R8 は種類ごとの空きのビット、R7 は新しい種類。
**時刻の意味が変わる種類（1・7・11・12・3・5）は、TIMING の申告で区別する**（申告が無ければ今の意味）。
バイトを詰めた種類 13・14・15（bandwidth-observation 3.5）の各バイトの時刻は、3・4・5 と同じ意味（同じ申告が効く）。
窓の幅を回路から直接出したい（決定的でないばらつきが増えたとき）場合は 256 ビットの形式（第 2 版）が要る。これは利用者の判断（§6）。

### 4.5 ホスト: 時刻の事実（`fprobe/timing.py`）

- 1 件のイベント（と、その前後のイベント・その時の PHY の設定）から、次を返す関数を一か所に置く:
  `{"t": 補正した時刻, "t_lo": 窓の始め, "t_hi": 窓の終わり（不明なら None）, "parts": [{"name": …, "ticks": …, "kind":
  "fixed" | "phase" | "window" | "measured", "from": "TIMING" | "config" | "events"}], "unknown": ["output_delay", "target_internal", …]}`。
  `t` は「線上の点（または端子の辺の最も前）」で、どこを `t` にしたかも `parts` に書く。
- 使う所: `events.resolve`（ポストされた読みは窓 = 頼んだ転送から返した転送まで。**R5 の前でも、いまのイベントの列だけで出せる**）、
  記録（`recording.py` の `uncertainty` の 1 つの数を `t_lo`・`t_hi`・根拠に置き換える。古い記録は読める形を残す）、
  共通タイムライン・MCP・API。画面は `t_lo`〜`t_hi` を与えられたとおりに描くだけ（計算しない）。
- PC 側の出来事（19）も同じ形にする（窓 = 前後の CSR の読み）。

### 4.6 対象側の時計との対応付け

- いま: ボード間の同期（最小二乗の直線 + 残差。`timesync.py`）、観測の口の印（`fpga_obs.fit_line`）、対象の Linux（PC の往復の中点の
  内挿。`linux/clock.py`）が別々にある。
- まとめる: `fprobe/clockmap.py`（新設）。入力は「プローブの時刻の窓 [lo, hi] と、対象の時計の値（またはその窓）」の組の列。
  出力は、区間ごとに**すべての窓を通る直線の範囲**（傾きと切片の取りうる範囲）と、任意の対象の時刻に対するプローブの時刻の範囲。
  - 仮定は「区間の中で 2 つの時計の比が一定」だけで、結果に書く。区間の切れ目は使う側が明示して渡す（`timesync.segments` の
    `gap_ticks` と同じ。暗黙の既定にしない）。どの直線も全部の窓を通らなければ、それを返す（仮定が破れた事実）。
  - 最小二乗（対称な雑音の仮定が入る）は置き換えの候補。既存の出力が変わるので利用者の判断（§6）。
- 組の作り方（どれを使うかは使う側が明示して選ぶ。既定は置かない）:
  1. **DAP で対象の時計を読む**: サンプラで対象のカウンタを読む。窓は R6 の窓そのもの（読みを頼んだ点〜返した点）で、対象の
     ソフトは要らない。Cortex-M: DWT_CYCCNT（0xE0001004）。Zynq の対象（Cortex-A9）: グローバルタイマ（0xF8F00200、AHB-AP から
     読める。Linux のクロックソースがこれなら CLOCK_MONOTONIC_RAW はその一次式。推測・要確認: `/sys/devices/system/clocksource/
     clocksource0/current_clocksource`）。Pi 2B は AP が APB だけなので、この方法が使えるかは未確認。
  2. **対象が印を出す**: 対象が自分の時計を読む → GPIO を変える → もう一度読む。ロジアナがその辺を見る（位相 1 刻み）。
     組 = （辺の窓, [前の読み, 後の読み]）。ftrace なら `trace_marker` に書く前後の時刻（推測・要確認: `trace_clock` の選び）。
     対象のソフトの改造が要る（汎用の機能に合わせてターゲットを改造する、の原則に沿う）。
  3. **SWO / ITM の時刻**: ITM の時刻の記録は対象の時計の値なので、1 または 2 の組で絶対の位置を決めれば、各パケットの対象の時刻が
     プローブの時間軸に載る。パケットが端子に出るまでの対象の遅れ（11・12 の「対象の中」）は、到着の時刻 ≥ 対象の時刻の換算、という
     不等式として残す（遅れの値は推定しない）。
  4. 既存: ボード間の同期の組・観測の口の印（窓の幅 = 位相の幅）。
- PYNQ の PS（中継の Linux）の時刻は、PS が AXI で PL のタイムベースを読めば窓が約 0.3 µs になる（いまは PC の往復 約 300 µs）。
  PYNQ の上で起きた出来事（中継・Linux の停止の流れ）の時刻に使える（推測: 1 語の読みが 0.15 µs という §1 の値からの見込み。未測定）。

### 4.7 非干渉性（2.9）との関係

対応付けの 1（DAP で読む）は対象のバスを使う。読んだ回数・時間はイベントの列にすべて残る（サンプラの語 1 件ずつ）ので、
それを使う側に返す（乱した量を事実として記録する、の 2.9）。読む周期は使う側が明示する。

## 5. 確かめ方と段階

### 5.1 cocotb（シミュレーション）

すべて「端子の辺を既知の時刻に起こし、イベントの時刻・窓が、ホストの `fprobe/timing.py` の式と一致する」形にする（式は 1 か所）。

- `la_front`・`la_capture`・`la_deep_buffer`・`la_cap_domain`: ピンの ch と内部の ch に同じ周期で辺を入れ、記録の上で同じ番号に
  来ること（R2。直す前に、いまは 3 刻み食い違うことを試験で確かめてから直す）。連続記録の起点（R1）。
- `swd_phy`・`jtag_phy`・`debug_port`: hp・turnaround・idle_cycles・sample_delay・data_phase・ACK（OK・WAIT・FAULT）・RnW の組み合わせで、
  `rsp_time` と線上の点（SWCLK / TCK の辺）の関係。JTAG は IR を省く場合と省かない場合、DP の読み（RDBUFF の追加）。
- `dap_monitor`・`swd_injector`・`dap_sampler`・`event_collector`: 端から端（`dap_harness`）で、ホストの転送・割り込み・サンプラの語の
  イベントの時刻が `rsp_time` になること。サンプラの WAIT の回数と窓（R6）。
- `uart_rx`・`swo_receiver`・`swo_manchester_rx`: divisor の偶数・奇数、送り手の速さのずれ（±）、ホストへの経路の待ち（R3 の前は w が
  時刻に入ることを確かめる）、1 フレーム複数バイト（R4）。
- 既存の確かめ（`la_cap_domain_tb.py` の latency、`LA_*_DELAY` の試験、`sync_link` の RX/TX）は、時刻の表から値を取る形に書き換える。

### 5.2 実機（PYNQ-Z2）

- **ロジアナの ch の間のずれ**: 試験用の方形波（`la_testgen`）の出力を、全部の入力に同じ長さの線でつなぎ、ch ごとの辺の位置の分布を
  返す（事実として。揃える補正値は測った値として持ち、測った日・温度（XADC）と一緒に返す）。
- **DAP の時刻**: SWCLK/TCK・SWDIO/TMS の端子をロジアナの入力にもつなぎ、イベントの `rsp_time` とロジアナで見た辺の差の分布を取る
  （出力の遅れ + 入力の遅れの和が定数になるはず。R9 の前後で比べる）。対象は Nucleo F401RE（SWD）・Pi 2B（JTAG。DAP が CTRL/STAT に
  応答しない間は TAP のスキャンだけ）・Zynq の対象（PJTAG）。
- **UART・SWO**: TX → RX の折り返しで、送信のイベントと受信の開始の辺の差（R3 の式の確認）。Nucleo の SWO をロジアナにもつなぐ。
- **ボード間の同期**: Primer と PYNQ を同期の線でつなぎ、両方のロジアナに同じ辺を入れて、換算した時刻の差の分布を取る（performance-limits
  §5 の「ボード間の同期の実機の精度」）。
- **対象の時計**: Nucleo の DWT_CYCCNT をサンプラで読んだ組と、GPIO の印の組で、同じ対応付けが窓の範囲で一致するか。Zynq の
  グローバルタイマも同じ。
- 配線の指示は、電源の順・電圧の範囲・逆流を書く（プロジェクトの約束）。PYNQ の PMOD は 3.3 V。

### 5.3 段階（依存の順。各段は独立に統合できる）

| 段 | 中身 | 触る所 |
|---|---|---|
| S0 | ホストだけ: `fprobe/timing.py` の骨組み（いまの回路の意味の表）、ポストされた読みの窓（7）、記録の `t_lo`・`t_hi`、連続記録の起点の経験則（余裕 4 サンプル）を除いて往復の窓をそのまま返す。**加えて**（2026-11-01 の指示）ロジアナの内部の ch のずれ（2）をホストが ch ごとに直す（`fprobe_gw/timing.py` を新設し、TLV LA_CH_TIMING を生成）。実装済み: §8 | `host/fprobe`・`fprobe_gw/timing.py`・`caps.py` |
| S1 | 小さい RTL: R1（連続記録の起点）・R2（内部の ch の段数）・R3（UART 受信）・時刻の表と TLV TIMING の生成 | `la_deep_buffer`・`fprobe_core`・`uart_rx`・`fprobe_gw` |
| S2 | DAP の時刻: R5・R6・R7（stop-precision の 3.2・3.3 と同じファイルを触るので、その統合の後） | `swd_phy`・`jtag_phy`・`debug_port`・`dap_monitor`・`swd_injector`・`dap_sampler`・`sampler_match`・`event_collector` |
| S3 | R4（Manchester）・R9（出力の IOB。stop-precision 3.1 と一緒）・対象の時計の対応付け（`clockmap`） | |
| S4 | R8（細かい位置。la-resolution の取り込みの後）・ボード間の配線の遅れの測定・TDC（任意） | |

## 6. 利用者に決めてもらうこと

1. **DAP の転送の「時刻」の定義**（R5）。案: SWD は ACK の 3 ビット目の後の立ち上がり、JTAG は Update-DR。ほかに「ヘッダの始め」「完了」を
   選ぶこともできる（どれを選んでも、他の点は設定で決まる式で出せる）。
2. **イベントの形式**: 128 ビットのまま（時刻の意味の変更 + 空きのビット + 設定のイベント。§4.4。私の推奨）か、窓の幅を回路から直接出せる
   256 ビットの第 2 版か（イベントの帯域が 2 倍。PS への経路の作り直しが要る）。
3. **互換**: 時刻の意味が変わる種類を、同じ種類の番号のまま TIMING の申告で区別する（私の推奨）か、新しい種類の番号にするか。
4. **対象の時計の直線の合わせ方**: 窓をすべて通る直線の範囲（§4.6。仮定は比が一定だけ）に置き換えるか、いまの最小二乗を残して並べるか。
   既存の `timesync`・`fpga_obs` の出力が変わる。
5. **TDC を作るか**（面積と較正の手間。作るなら対象の入力の本数）。
6. **秒への換算の基準**: 外の基準（GPS の 1 秒の印などをロジアナの入力に）を用意して発振器の誤差を測るか、公称の周波数のまま「ppm は不明」と
   返すか（基準が無ければ後者しかない）。
7. **ボード間の配線の遅れ**: 2 本目の線（子 → 親の折り返し）を足して往復で測るか（「行きと帰りが等しい」は仮定として結果に書く）、
   線の長さを測って使う側が明示して渡すか（いまの `cable_delay_ticks`）。

### 主担当の判断（2026-11-01）

- **1（DAP の時刻の定義）**: SWD は ACK の後の立ち上がり（ACK の 3 ビット目を出す Low 相の終わり = 次の SWCLK の立ち上がり）、
  JTAG はその転送の DR スキャンの Update-DR を出る TCK の立ち上がり。S2 で RTL（R5）を変えるときの目標。S0 では RTL を触らない。
- **2（イベントの形式）**: 128 ビットのまま（§4.4）。256 ビットの第 2 版は作らない。
- **3（互換）**: 時刻の意味が変わる種類は、同じ種類の番号のまま、ケイパビリティの申告（TLV TIMING）で区別する。
- **4（対象の時計の直線）**: 窓をすべて通る直線の範囲を返す形（§4.6 の `clockmap`）に置き換える。最小二乗は残さない。
  既存の `timesync`（`fit_parent`・`to_parent`・`interpolator`）・`fpga_obs`（`fit_line`・`fit_sync`）の出力が変わる。
  段階の表（§5.3）どおり S3 で行い、そのとき変わる出力（`ParentTime` の `ratio`・`residual_max` → 傾き・切片の範囲、
  `to_parent` の `t` → `t_lo`・`t_hi`、`SyncSegment` の直線 → 範囲）をこの文書に書き、呼び出し側（`merge`・`obs_decoder`・
  `linux/clock`）と試験を合わせる。S0 では変えていない。
- **5（TDC）・6（外の基準）・7（ボード間の 2 本目の線）**: 当面作らない（ハードウェアの作業が要る）。秒への換算は公称の周波数のまま
  「ppm は不明」、ボード間の配線の遅れは使う側が明示して渡す（いまの `cable_delay_ticks`。暗黙の既定にしない）。

## 7. 設計原則との関係

- ヒューリスティックを入れない: 補正は段数・設定・規格で決まる値だけ。いまある経験則（連続記録の起点の余裕 4 サンプル）は S0 で除く。
  ADIv5 の窓は規格の規定（要確認と書いた）。対象の時計の直線は仮定を結果に書き、破れたら返す。
- 画面は考えない層: 窓・根拠の計算はホストの `fprobe/timing.py` と `clockmap.py`。画面は返された窓を描くだけ。
- 移植性: 時刻の表・`rsp_time`・源が時刻を運ぶ形はボード非依存のコア。ISERDES・IDELAY・TDC・IOB はボードの層。Primer・小さい FPGA は
  TIMING の申告が違うだけで、同じホストのソフトが動く（細かい位置の無い構成は位置の欄が 0 で、分割数 1 を申告する）。
- 特定のターゲットのための機能を足さない: 対象の時計は「DAP でカウンタを読む」「GPIO の印」という汎用の組み合わせで扱い、どの番地の
  カウンタを読むかは使う側（または取り外せるプラグイン）が渡す。

## 8. 段階 S0 の実装（2026-11-01）

### 8.1 直したこと

| §2.1 | 前 | 後 | 所 |
|---|---|---|---|
| 4 連続記録のサンプル 0 | トリガ時刻が前後の読みの間にあればそれを起点にし、不確かさ「4 サンプル」（経験則 `TRIGGER_SLACK_SAMPLES`）。無ければ往復の中点。比べたトリガ時刻は段数を引かない生の値で、ピンの時間軸（他の捕捉）と 8 刻み食い違っていた | 回路と往復から確かに言える範囲だけ: `t_lo` = 前の読み − (LA_DELAY の深いバッファの段数 + 位相の幅)、`t_hi` = 後の読み（トリガがこの流れのものならトリガ時刻）− 同じ。範囲の中のどこかは推定しない。波形は範囲の中点に置き、記録の不確かさ（± 範囲の半分）がちょうど範囲を覆う（中点は置き場所の決まりで、推定ではない） | `fprobe/timing.py` `stream_start_window`、`recording_stream.py` |
| 2 内部の ch | 全 ch にピンの段数（LA_DELAY）を引く。内部の ch はフィルタを通らないので 3 刻み早く見えた | 回路が ch ごとのずれを申告（TLV LA_CH_TIMING。正は `fprobe_gw/timing.py` の `la_channel_timing`: ピン・専用の入力 0、内部の ch はフィルタのある構成で +LA_FILTER_DELAY、位相の幅 0。取り込みの領域の内部の ch は未確認なので「分からない」）。フィルタをかけた ch は設定の cycles − 1 周期さらに遅れる（`la_filter.vhd`。ホストが書いた設定から）。ホストは `Capture`・`Transitions` の `channel_skew`（刻み、ch ごと）で ch を置き直す。申告の無い古いデバイスの内部の ch は「分からない」（0 で埋めない。ずらさずに置き、分からないと返す） | `fprobe_gw/timing.py`・`caps.py`（TLV 21）、`fprobe/timing.py` `la_channel_timing`、`la.py`、`la_wave.py`（`Waveform` が ch ごとに置き直す）、`recording.py`（波形のかたまりの見出しに `channel_skew`、VCD の書き出し）、`la_vcd.py`、`la_measure.py`、`export/perfetto.py`、`probe_la.py`、`probe_analysis.py` |
| 3 取り込みの領域の時刻 | la_cap_info の latency（前段の段数を含む）で求めたピンの周期から、さらに sys の経路の LA_DELAY（6 + 1 刻み）を引いていた（二重） | la_cap_info の latency だけで求める（n はピンが変わった周期の下限そのもの） | `la.py` `LogicAnalyzer.read` |
| 7 ポストされた読み | 値が返った転送の時刻だけ | 対象が読んだ瞬間の範囲 [`time_lo`, `time`]: `time` = 値を返した転送（確かな上限）、`time_lo` = 読みを頼んだ転送の前に線上で終わった転送（ホスト・サンプラ・割り込みのどれでも。確かな下限。見えていなければ None）、`requested` = 読みを頼んだ転送。頼んだ転送の時刻は完了 + 2 なので、対象のアクセス（頼んだ転送の ACK の後に始まる）の下限にはならない（設計時の案「窓 = [頼んだ転送, 返した転送]」から直した）。ACK の線上の時刻は S2（R5・R7）の式で出す | `events.py` `resolve`、`collector.py`（かたまりをまたぐ持ち越しにも下限を付ける）、`fprobe/timing.py` `posted_read_window`、`probe_events.py` |

### 8.2 API の出力の変化

- `la_collect`・`la_deep_collect`: 新しい欄 `channel_timing`（ch ごとに `channel`・`kind`・`skew_ticks`（None = 分からない）・
  `span_ticks`・`from`（"LA_CH_TIMING" / "LA_DELAY" / "config"））。`first_edges_ns` は ch のずれを足した値になった（内部の ch は
  フィルタのある構成で +30 ns、フィルタをかけた ch は −(cycles − 1) 周期）。`la_arm`・`la_deep_arm` の `filter` に `delay_cycles`。
- 取り込みの領域の構成（`fprobe_pynq_cap`）の `trigger_time` は 7 刻み（70 ns）後ろになった（二重に引いていたのをやめた）。
- `la_view`・共通タイムラインの波形・`recording_waveform_measure`・`la_measure`・VCD・Perfetto: 内部の ch・フィルタをかけた ch が
  ずれの分だけ置き直される（ずらした ch の変化点・位置は小数のサンプル番号になりうる）。
- `recording_la_stream_start`・`recording_la_stream_stop` の `anchor`: `t_lo`・`t_hi`・`basis` が増えた。`ticks` は範囲の中点、
  `uncertainty_ticks` は範囲の半分（前は「トリガ時刻、4 サンプル」か「中点、往復の半分」）。`method` は範囲の終わりを決めたもの
  （"trigger" / "host"）。記録の `la`・`stream_start` の本体に `t_lo`・`t_hi`・`basis`・`channel_skew`。
- `events_read`（raw = false）: ポストされた読みに `requested_us`・`time_lo_us`。記録の `dap` の `ap_read`・`drw_read`（値が後で返ったもの）の
  本体に `requested`・`t_lo`。
- ケイパビリティ: TLV 21 LA_CH_TIMING（ch 1 本に int8・int8・u8。-128 / 255 = 分からない）。古い TLV LA_DELAY は同じ値で残る。
  ホストの `Capabilities.la_channel_timing`。

### 8.3 S1 以降に回したこと

- R1（連続記録のサンプル 0 の時刻を回路で記録）: 記録されれば S0 の範囲は 1 点になる。
- R2（内部の ch の段数を RTL で揃える）: 揃えたら LA_CH_TIMING の内部の ch は 0 になり、ホストは変えずに済む。取り込みの領域
  （`la_cap_domain`）の内部の ch の段数を cocotb で確かめるのもここ（それまでは「分からない」）。
- TLV TIMING と `fprobe_gw/timing.py` の残り（DAP・UART・SWO の点）、VHDL のパッケージの生成、cocotb の試験が時刻の表から値を取る形。
- 記録の不確かさの列（`unc`。± の 1 つの数）を `t_lo`・`t_hi`・根拠に置き換えること（§4.5）。S0 では範囲を本体に持たせ、列は
  範囲を覆う ± のまま。PC 側の出来事（19）の窓も同じ。
- `la_stream`（VCD への直接の流し込み、`VcdWriter`）は ch のずれを入れていない（R2 で揃えば不要になる）。
- `merge` のボードの波形は、子のずれ（子の刻み）をそのまま親の刻みとして使う（2 枚の時計の比の ppm の分だけ換算が違うが、ずれは数刻みなので差は 1 刻みの 1e-4 未満）。
- 対象の時計の対応付けの置き換え（§6 の判断 4、`clockmap`）は S3。

## 9. 全数点検の結果と割り当て（2026-11-02）

利用者の指示（2026-11-02）: 「時刻の正しさは非常に重要。完璧に、同じ修正点を漏らさない」。HEAD cc172ead を、この仕様書を読まずに
コードから独立に全数を作り（RTL はタイムベース `timebase.vhd` の `time_count` から配線をたどる、ホストは時刻を作る・直す・推定する所）、
最後に §2.1・§4.3・§8 と突き合わせた（読むだけの点検。点検役の報告の行番号のうち、主担当が開いて確かめたものに「確」）。
下の表の項目はすべて、どこかの段に割り当てる。割り当ての無い項目を残さない。

### 9.1 漏れ・不足・食い違い（段の割り当て）

| # | 種類 | 中身（根拠） | 段 |
|---|---|---|---|
| A1 | 漏れ | `fprobe_core` の CSR の DAP の時刻 `match_time`・`inject_time`・`inject_first_time` がその場で `time_count` を取る（`fprobe_core.vhd:1130-1139`、確）。R5 でイベントだけ線上の点にすると CSR と食い違う。使う側 `halt_watch.py:104-116`・`linux_halt/backend_arma.py:416`・`request_times.py` | S2（2026-11-02 に実装中の担当へ追加） |
| A2 | 漏れ | 多段トリガの発火の時刻 `seq_time`（`fprobe_core.vhd:1083-1090`、確）が表に無い。ピン → 同期 2 → ev → fire で 3〜4 刻み（`trig_seq.vhd:90-121`）、フィルタ構成は +3、lanes 構成は ISERDES の最も新しいレーン、match・host の入力は +2 | S1b（済 eab33581・21efa776: TIMING の seq_pin・seq_internal・seq_input_lanes・seq_event、cocotb trig_seq_tb で 4・2 段、ホスト timing.seq_fire_window・xtrig.sequence_status の fire_windows。§11） |
| A3 | 漏れ・食い違い | PYNQ の JA の出力は IOB の FF（`pad_o`/`pad_t`、`pynq_z2_fprobe_top.vhd:183-191, 486-492`）で +1 刻み（Primer は 0）。§2.2 の 1「IOB で登録していない今は」は PYNQ では誤り。表の 5・6・10・14・16 | DAP の端子は S2（追加済み）、UART TX・クロストリガ・同期の出力は S3（R9 と一緒） |
| A4 | 漏れ（原則） | SWO の ITM パケットに PC の時計 `time.monotonic()` を 1 回の読みごとにまとめて付ける（`probe_observe.py:641-660`、確）。デバイスが付けた SWO のバイトの時刻（種類 5・15）も ITM のローカルのタイムスタンプも使わない。Perfetto の例外の区間もこれから作る | H（済 890651e1: 記録の本体に time_source = host_received。API の応答の local_time・global_time は元から ITM の時刻。デバイスの SWO のバイトの時刻で解読し直すのは残した: 生の流れと時刻付きのイベントの 2 つの口をまとめる設計が要る） |
| A5 | 漏れ（原則・最重要） | 対象のトレース（SystemView・CTF・Tracealyzer・TraceX・EventRecorder の snapshot）を「最後の出来事 = 読んだ瞬間」と置き、不確かさ 0（`tracefmt/model.py:125-135` の `TargetClock(uncertainty=0.0)` 確、`probe_formats.py:533-534, 691-693, 774-776`） | H（済 10ab360c: 対応点は上限 anchor_bound = upper、幅は受けた時刻の写しだけか None、unknown に anchor_lag・rate_error。記録は time_bound = upper で不確かさの列なし） |
| A6 | 漏れ | テストポイントの流れの起点が `(before+after)//2 ± 幅/2`（`probe_testpoint.py:214-226`、点検役は確）。`LA_DELAY`・位相の幅を引かず、`timing.stream_start_window` と食い違う | S1b（済 21efa776: testpoint の stream は recording_stream.anchor_stream を使う。settings.anchor） |
| A7 | 漏れ | `halt_watch.py:104-116` の `t_fire`・`t_prev` は 48 ビットのまま（折り返しを外さない）。並ぶ `t_first_write` は 64 ビット | H2（済 3ba35cca: 後に読んだタイムベースで 64 ビットに。§14） |
| A8 | 疑い | 取り込みの領域の構成（`fprobe_pynq_cap`）で、`probe_la.py:156-175` がフィルタ・パルス幅を `sys_clock_hz` で ns に換算（cap の周期で数えるなら 3 倍ずれる。`la_cap_domain` の中は未確認）。同じ所の `rate_hz` はレーンの分周を考えず、`trigger_time` はピンに直していない生の値 | S1b（済 21efa776: フィルタ・パルス幅の ns を la_sample_hz で、trigger_time をピンの時刻に直し、生の値は trigger_time_raw） |
| A9 | 漏れ | 1 周期に複数のサンプルの構成（`fprobe_pynq_fast`・`fprobe_pynq_deep_fast`）の行が表に無い（`la_lane_capture.vhd:52`、`la_lane_encoder.vhd:45`、IN_AGE `pynq_z2_fprobe_top.vhd:397`、細かくないピンは sys で 2 段同期化 `fprobe_core.vhd:879-892`、ずれは None） | S1b（済 eab33581・21efa776: 表の 23、la_lane_inputs で内部の ch を揃え（N2）、TIMING の la_lanes・la_deep_lanes・la_lanes_xtrig・seq_input_lanes、連続記録の起点は timing.stream_start_lanes） |
| A10 | 漏れ | サンプラの語の時刻（表の 8・R6）に、SWD のか所の境界（TAR を挟んで 2 転送後、`dap_sampler.vhd:609-620`）と JTAG（DRW → POSTED_CTRL → 次のスキャン、`S_SETTLE`・`S_READ_BOUNDARY` の待ち、`dap_sampler.vhd:563-572, 587-597`）が無い。R6 の窓の後端の式がこの場合に合わない | S2（追加済み） |
| B1 | 不足 | UART 受信の待ち w に上限が無い（バーストの最後のバイト。`uart_rx.vhd:62-63, 100-107`）。PYNQ は `frame_mux` の待ち、Primer は 512 段の FIFO（ほぼ 0）。表の「上限は次のバイトが揃うまで」は誤り | S1（R3 で直る。表を直す） |
| B2 | 不足 | 補助マイコン（表の 15）: 時刻は 3 バイト目の停止ビット + 同期 2 + 2（`aux_link.vhd:46-60`）。TIMING で「対象の中は不明」と返す項目が決まっていない | S3 |
| B3 | 不足 | 観測の口（表の 17）: 標本型（`obs_snap_core`）は遅れの申告が無い。記録型の SYNC_DELAY は平均（±0.5 周期、`obs_core.vhd:34-35, 91-96`）をホストが 1 点として引く（`obs_decoder.py:258`）。組が 1 つの鎖は傾きを借りる（`obs_decoder.py:58-82`、仮定）。表の「+1」は導出では 0（`obs_jtag_engine.vhd:330-346`、`obs_tck` は組み合わせで端子） | S3（clockmap と一緒）。傾きの借りは H（済 1baea835: fpga_obs_open の borrow_ratio で使う側が明示したときだけ。既定は申告の CLOCK_HZ の比） |
| B4 | 不足 | ボードの間の合成 `merge.py:27-91` の不確かさが「手で書いた 1 刻み + 子」だけで、`phase_span`・`residual` が入らない | S3 |
| B5 | 不足 | DAP の時刻を使う側（`request_times.py:47-67`、`correlate`）が「完了 + 2」を前提にしている。`request_times` は「どのコアも書き込みから停止までが同じ」と仮定して数値の欄を置き換える | S2（追加済み） |
| B6 | 不足 | None（分からない）のずれを、`timing.skew_of`・`la_wave` が 0 として置く（`timing.py:107-109`、`la_wave.py:90-95`）。VCD・Perfetto・correlate に「分からない」が伝わらない | H（済 80c37f73: ずらさずに置いたうえで skew_unknown・VCD の $comment・Perfetto の args・相関の gaps の pin_skew_unknown で返す） |
| B7 | 不足 | ロジアナのトリガ → クロストリガの源（LA_DELAY + 立ち上がりの検出 1 + 1、`fprobe_core.vhd:1119-1126`）が表に無い。深いバッファで pack ≠ 0 なら `la_pack` が hit を最大 (k−1)·(rate+1)+1 遅らせる（`la_pack.vhd:37-39, 66-69`）。RAW でもストリームでなくても効く（`la_deep_buffer.vhd:307`）。表の 4 の「`LA_DEEP_DELAY` 4 固定」に入っていない | S1b（済 eab33581・21efa776: TIMING の la_xtrig・la_deep_xtrig・la_deep_packed・la_cap_xtrig・la_lanes_xtrig を cocotb で確かめ、ホスト timing.la_trigger_facts・la_xtrig_source、API の xtrig_source_time） |
| D1 | 食い違い | UART 送信（表の 14）: 表は 0 刻み、PYNQ は端子が +1（受理 c → frame c+1 → pad_o c+2、時刻は c+1） | S3（R9） |
| D2 | 食い違い | クロストリガの出力の端子（表の 16）: 表は 4 周期、PYNQ は 5（`xtrig_out.vhd:13` + pad_o）。脈の長さが最初の刻みの位置の分だけ短くなりうること（`xtrig_out.vhd:10`）も無い | S3（R9） |
| D3 | 食い違い | この仕様書の行番号が古い（`event_collector` の切り出し・`event_byte_pack` の追加の前）: format 82-90 → 93-104、内部の ch 773-781 → 837-846、クロストリガ 984-1003 → 1116-1141、UART 受信 1222 → 1364、`caps.py:43-46, 64-73` → `fprobe_gw/timing.py` | 次にこの文書を直す段（S1 の統合のとき）。S1b（済（§11 のコミット）: §2 の行番号を直した。行番号は 21efa776 の時点のもの） |
| D4 | 疑い | `sampler_match.vhd:13` の「次の周期」は記述の誤りの疑い（発火は 2 周期後、`fprobe_core.vhd:584`） | S2（追加の 2 と一緒に確かめる） |

### 9.2 ホストの、仮定・経験則で時刻を作る所（段 H、本体の原則「ヒューリスティックを入れない」）

A4・A5・A7・B3 の傾きの借り・B6 に加えて（点検役の報告。主担当は A4・A5 だけ開いて確かめた）:
- SWO の不確かさを `10 ビット / baud`（8N1 を仮定、Manchester でも同じ）、RTT の上限に公称の `poll_s`（`probe_formats.py:300-318`、`probe_testpoint.py:43, 290`）→ 済 2121325d（SWO・UART は None = 分からない、RTT は PC の時計の写しだけ。どちらも上限の印 time_source・bound）
- 対象の時計を `core_clock_hz` で線形に写し、速さのずれを不確かさに入れない（`probe_testpoint.py:323`、`testpoint/live.py:44`）→ 残した（core_clock_hz は使う側が明示した値。速さのずれを返すには対象の時計の対応付け（§4.6 の clockmap、S3）が要る）
- host call の区間の始まりを `e.t − PC の秒数 × 公称の hz` で推定（`export/perfetto.py:174-176`）→ 済 890651e1（記録が始まりを同じ時計の対応付けで写して t_start に入れる。無い古い記録は点）
- 組の範囲の外へ延ばしても不確かさが増えない（`linux/clock.py`、`probe_linux.py:865-927`）→ 済 2121325d（組の外 outside の記録の不確かさは None）
- 分解能を中央値で決める（`rtos/timeline.py:129-131`）→ 済 ae3fade6（実際の間隔の最大。抜けにした間隔は除く）
- 既定の暗黙の値: `probe_rtos_analysis.py:87,95` の `window_us`、`rules.py:348-358` の窓の起点、`probe_profile.py:78,112` の `exception_rate_hz=1000`（2 回目の点検で訂正: 延ばすときは `profile/dwt.py:157` が警告する。暗黙の既定値は事実）→ exception_rate_hz は済 ae3fade6（既定 None = 見積もりに入れず警告、計画に使った値）。規則の窓の起点は ae3fade6 で結果に window_t0 を返す（起点を引数にするかは残した: 規則の書き方が変わる）。window_us の既定は残した（結果に使った値 window_us を返している。必須にすると画面・利用者の呼び方が変わるので利用者の判断）
- 黙って欠ける・ずれる: `probe_swo.py:291`（前と同じ刻みのバイトを捨てる）・`:301`（時刻が無いと 0）・`:337`（溢れの時刻が最後のバイトの時刻）（:301・:337 は済 e9c4820f: None と untimed の数、溢れは印のパケットの時刻。:291 は残した: バイトの時刻は 1 バイトの長さ以上離れるので同じ刻みは起きない。窓を読み直す重なりを時刻で除く形のままで、通し番号で除く形にするのはイベントの窓の口の変更）、`cycles.py:86`（CYCCNT の 1 周を超えても気づかない。`profile/cycles.py` の clock_from_two_reads。済 71135588: 2 回の読みからは周回が分からないので「1 周未満と仮定した」を warnings に返す）、ITM の遅延フラグ・overflow の後の印を捨てる（`itm.py:422`、`profile/*`。2 回目の点検で訂正: `itm.py:422` は印 after_overflow の定義で、捨てるのは使う側。残した: 使う側（`probe_observe.py:653` の応答・profile）に印を通すのは次）、VCD の timescale 1 ns で 800 MS/s（1.25 ns）が丸まる（`la_vcd.py`・`recording.py`）、`stream_to_vcd` が ch のずれを当てない（§8.3 で既知）→ 済 80c37f73（丸めない最も粗い単位、丸めたら $comment。stream_to_vcd もずれで置き直す）
- **対象の出来事（ブレークポイントの停止・ヒープ・gdb のトレースポイント・watch の式の評価）に PC の時計の時刻を、PC の時刻である印なしで付ける**（`probe_watch.py:496-516`、`probe_heap.py`、`probe_gdb_trace.py:175-180`、`probe_breakpoints.py`、`debug.py` の `elapsed_s`）→ 済 890651e1（記録の本体と API に time_source: ヒープ・トレースポイント・ログのブレークポイント・ターゲットの状態は host_received、elapsed_s は host）。`probe_watch.py:501` は 2 回目の点検で訂正（`device.time()` で PC の時計ではない。全部の式の評価の前の 1 点を幅なしに付けるのが問題）: 残した（式の値の並びの形を範囲に変えるのは watch の API の変更）。表の 19 は PC 側の出来事だけを扱い、ここは範囲の外だった（漏れ）
- RTL の段数をホストに手で書いた所: フィルタの「cycles − 1」（`la.py:121-122`、`fprobe/timing.py:26`）、`merge.py` の `QUANTIZATION_TICKS = 1`（`fprobe_gw/timing.py` から生成する形へ）→ 残した（どちらも gateware の時刻の表からの生成が要り、gateware と `merge.py`（B4、S3）は段 H の範囲の外）
- プラグイン（本体の外。報告だけ）: `zmk_health.py:32-34`（32768 Hz を仮定）、`spiflash.py`（時間切れが固定値）、`keymatrix.py`（PC の sleep で tap を作る）→ 残した（本体の外。プラグインの持ち主へ報告）

### 9.3 段の追加

- **S1b**（S1 の統合の後）: A2・A6・A8・A9・B7。ロジアナ・トリガの側の残り。
- **H**（ホストだけ。RTL の段と独立）: 9.2 の全部と A4・A5・A7・B6、B3 の傾きの借り。直し方の原則: 時刻は測った事実（デバイスの時刻・範囲）で返し、
  仮定で作る所は「分からない」か、使う側が明示する引数にする。PC の時計の時刻には PC の時刻である印を付ける。
- S2・S3 は 9.1 の割り当てを足す。

### 9.4 点検で読み切れなかった所（2 回目の点検の対象）

- RTL: `la_cap_domain` の中（ext_trigger の sys → cap の段数）、`la_lane_front`・`la_lane_capture` の PIPE_CYCLES、`xilinx_la_iserdes` の IN_AGE、
  `la_filter`・`la_pulse` の段数（`la_pulse` が同期化の前の値を見るか）、`jtag_phy` の本体（RDBUFF を足す・IR を省く・遅延線。表の 6 を事実として確かめていない）
- target_ip: `obs_core.vhd` は見出しと申告だけ、Verilog の例（`shrike_lite_check` 等）、`obs_soft_tap`・`obs_tck_sampler` の段数
- ホスト: rtos の各 OS のファイルの一部、testpoint の instrument・cparse、linux_halt の rsp・cli・kernel・mmu、`tracefmt` の各解読器の細部、timeline 系、`api/binary.py`
- タイムベースのリセット（`rst_g(0)` で 0 に戻ったとき、ホストの unwrap48 と記録の連続性）

### 9.5 2 回目の点検（2026-11-02。§9.4 の読み切れなかった所と §9.1・§9.2 の行の照合）

値はどれもコードを数えた導出（cocotb では未確認）。直す段で、まず cocotb で確かめてから使う。

**§9.1・§9.2 の訂正**
- A8 は疑いでなく事実（確。`la_cap_domain.vhd:185-189, 205-209` のフィルタ・パルスは cap_clk、`probe_la.py:161` は `sys_clock_hz` で換算 → `fprobe_pynq_cap` で 3 倍長く出る。`la.py`・`fprobe/timing.py` の換算は正しい）。
- A4: API の応答には ITM の local_time・global_time が入る。PC の時計なのは記録（Perfetto の元）の時刻だけ（`probe_observe.py:653`）。
- A5: `probe_formats.py:533-534` は `unc or 0.0`（不確かさは `_arrival` の値）。0 固定は 691-693・774-776 と `tracefmt/model.py:129`。
- A6 の行は `probe_testpoint.py:228`（読みは 218・220）。A10 の S_SETTLE は `dap_sampler.vhd:556-561`（563-572 は S_READ_BOUNDARY）。B2 の時刻は `aux_link.vhd:71`・`event_collector.vhd:207`。
- B5: `request_times.py` は「完了 + 2」を前提にしない。前提は `fprobe/timing.py:97`（`posted_read_window`）。仮定（コアごとに同じ遅れ）は docstring 7-10、数値の欄の置き換えは `backend_arma.py:303-305`。
- B7 の最初の +1 は `la_record` の state の段（`la_record.vhd:183`）。§9.2 の `probe_watch.py:501` は `device.time()`（PC の時計ではない。問題は幅なしの 1 点）。`probe_profile.py` は警告を出す（`profile/dwt.py:157`。暗黙の 1000 Hz は事実）。`itm.py:422` は印の定義（捨てるのは使う側）。

**新しい差**
| # | 中身 | 段 |
|---|---|---|
| N1 | `fprobe_pynq_cap` の内部の ch のずれ: フィルタありで同じ瞬間のピンより cap で 2 周期早く入る（+2 cap 周期、位相の幅 0）。フィルタなしは −1。今は None（`fprobe_gw/timing.py:73`） | S1b（済: S1 の R2 で揃っていた。cocotb la_cap_domain_tb の internal_channels_land_on_the_same_sample_as_a_pin_changed_on_the_same_edge が RATIO 2・3・4 で通ることを確かめた。回路の変更なし） |
| N2 | 1 周期に複数のサンプルの構成の内部の ch: +(L + IN_AGE) 細かいサンプル（fast は L 8・IN_AGE 5 で 13 = 16.25 ns）。細かくないピンは [IN_AGE − L, IN_AGE − 1]（今の製品構成は使わない）。今は None（`fprobe_gw/timing.py:103-105`） | S1b（済 eab33581: 申告でなく回路で揃えた。la_lane_inputs。LA_CH_TIMING の内部の ch は 0・幅 0、sys で取るピンは −1・幅 1） |
| N3 | 外部トリガ（クロストリガ → ロジアナ）の `trigger_time`: ホストはピンの段数を引くので、`la_ext` が 1 になった辺より前になる（sys・フィルタあり 5 刻み前、なし 2、cap・フィルタあり cap 4 周期前、なし 1、レーン約 3 sys + IN_AGE）。rate > 0 で最大 rate 周期の待ち。`la_cap_domain.vhd:17` の注「ext は遅らせない」は実際と違う。表の 16 | S1b（済 eab33581・21efa776: 回路で揃えた。la_front の EXT_DELAY・la_lane_front の EXT_DELAY / EXT_LANE。TIMING の la_ext、ホスト timing.ext_trigger_window。la_cap_domain.vhd の注を直した） |
| N4 | JTAG（表の 6）の記述は誤り: IR の省略は「開始 → Update-DR」だけを変え、「Update-DR → 完了」は変えない。1 ビットは 2·hp（区間ごとに +1 周期）。D − E_u = hp + 1 + 2·hp·idle（DP の読みで ACK OK なら RDBUFF の 2·hp·(40 + dr_before + dr_after) + 5 を足す）。設定と要求・ACK で決まる固定値 | S2（実装中の担当へ伝えた） |
| N5 | `LA_FILTER=false` かつ `LA_PULSE=true` だと `la_pulse` が同期化していない値を 1 段で取る（メタ安定）。製品構成は使わないが `caps.py:197` は拒まない | S1b（済 eab33581: caps.Capabilities が断り、fprobe_core が elaboration で止める） |
| N6 | 観測の口: `obs_core` の印は Update-DR の立ち上がりから [3, 4) clk（1 クロック版は [6, 7)）、申告の 7・13 半周期は一様の仮定の平均。標本型は [2, 3) 対象の clk で申告なし・TCK ≤ clk/8 を回路で守らない。`obs_decoder.py:203-231` は鎖の始め・組の切り替えの写しにも変化と同じ時刻を付ける。`shrike_lite_check` の Verilog は v1 の回路で今の申告は当てはまらない | S3 |
| N7 | ホストの解読器・rtos の時刻（SystemView の基準の混ざり・START の後のずれ、Event Recorder・Tracealyzer・CTF の unwrap と ts_hz の決め打ち、`stream.py` の偽の時刻、`uncertainty or None`・`(v or 0.0)`、rtos/timeline の端の区間・stats、rtos/analysis の同時とみなす読み、各 OS の 0 と折り返し、`flow.py` の暗黙の t1、`model.py` の isr_exit） | H2（済: §9.6 の表と §14） |
| N8 | タイムベースのリセット（ビットストリームの書き込み・電源・クロックのロック外れ）をホストが検出・記録しない: Recorder は単調性を見ない、ClockMap は前後の時代を 1 本の直線に当てはめる、`Collector._read_new` は前の件を数えずに失う（`EventCursor` と食い違い）、`obs_decoder` は 2^47 未満の戻りを後ろへの時刻にする。`_switch_design`・`recover_link` は記録を止めない | H2（済 61daecbe: 設計 §14.1。obs_decoder は残した: S3 と同じファイル） |
| N9 | A8 の続き（推測）: レーンの構成で `probe_la.py:160` の `rate_hz` が rate の値をそのまま使う（`la_lane_front` は rate を使わない） | S1b（済 21efa776: la_trigger_status の rate_hz は、1 周期に複数のサンプルの内蔵バッファは la_sample_hz（rate_used = false）、深いバッファの N 並列は 2^step × (rate + 1)） |

**§2.1 の表で直すもの**: 6（N4）、3（N1）、16（N3）。

**まだ読み切れなかった所**: N1〜N4 の cocotb での確認、`obs_jtag_engine`・`obs_async_fifo` の全体、`la_lane_encoder`・`la_deep_buffer` の中、BSCANE2 の UPDATE・CAPTURE の辺、EVENTS_CONTROL の start が written を 0 に戻すか、systemview.py の 41-139・325-430、tracealyzer.py の 205 行以降、scvd・cparse の本文、recording.py の 240 行以降、Zephyr の CTF の時刻の作り、レーンの構成でホストが rate に書く値、`obs_check_core.v` の中。これらは各段で触るときに読む。

### 9.6 2 回目の点検で段 H に足した項目の扱い（2026-11-02）

段 H の実装（80c37f73〜71135588）の途中で主担当から追加された。段 H2（§14）で、行を開いて確かめてから 1 項目ずつ失敗する試験から直した。

| 項目 | 中身（主担当の報告。行番号は H2 で確かめた） | 扱い |
|---|---|---|
| SystemView | SYSTIME_CYCLES の絶対の値を最初の 1 回だけ使う（`tracefmt/systemview.py:240-245`）、2 回目の START の後に止めていた時間の分ずれる（:228-231）、解読の誤りの読み飛ばしで差を失う（:197-203）。印なし | 済（b0d93cd9: ts は差の和で基準を変えない。TRACE_START・INIT・読み飛ばしで時刻の区切り、SYSTIME と差の和の食い違いは量と範囲を残して区切る） |
| Event Recorder | 対象の `ts_overflow` を使わず「1 周未満」の仮定で unwrap（`eventrecorder.py:60-61, 103, 133`） | 済（b0d93cd9: ts_overflow を上位に入れた 64 ビット。読みをまたいでも続く。init_count が変われば区切り） |
| Tracealyzer | 減るタイマにも 32 ビットの unwrap（:190、docstring :14-16 と食い違い）、欠けの印の時刻を直前の出来事に（:187） | 済（b0d93cd9: 減るタイマ・OS のタイマは時刻にしない（timed false・raw_ts）。欠けの印に after_ts・before_ts、前の時刻 0 を見落とさない） |
| CTF | ts_hz = 1e9 の決め打ち（`ctf.py:33, 214`。TSDL の clock の freq を読まない）、時刻の無い CTF に使う側の ts_hz を付ける（`stream.py:80-81`） | 済（b0d93cd9: TSDL の clock の freq か使う側の ts_hz（trace_start にも）。時刻の無い CTF には付けない） |
| unwrap | `tracefmt/model.py:91-99, 199-203` が後ろへの小さな戻りを +2^32 の前進に変える。`unwrap32` は数えない（`probe_formats.py:770`） | 済（b0d93cd9: 戻りと 1 周は解読から見分けられないので、前提 assumptions と戻った量の最小 ts_wrap_min_back を返す。unwrap32 は無くした） |
| 0 と分からない | `rtos/timeline.py:268` の `r.uncertainty or None`（確かめた: 0 を None にする）、`tracefmt/model.py:194`（A5 の 10ab360c で不確かさの列を入れない形にした）、`recording.py:621` の `(v or 0.0)`（確かめた: 分からない ch はずらさずに置く決まりなので、かたまりの境の並べ替えの限度としては正しい。B6 の扱いと同じ） | timeline.py:268 は済（11a7bac9: 0 は 0、分からないは None のまま書く。方法 B の分解能は baud・方式が分からなければ None）。model.py:194 は済 10ab360c。recording.py:621 は直さない（理由は左） |
| rtos の時間軸 | 観測の端・抜けの端の区間を不確かさ 0 で閉じ印が無い（:133, 141-152, 206, 235-238）、stats（:99-104）に切り詰めた区間が入る、`itm_packets` の範囲外のオフセットは黙って最後のバイトの時刻（:178）、方法 B の不確かさが baud 無しで 0（`probe_rtos.py:469`） | 済（11a7bac9: Run.start_cut・end_cut、長さの統計は両端が切り替えの区間だけ（cut_runs・max_cut_run）、範囲外は誤り、分解能 None） |
| rtos の解析 | `memory_series` の点は窓の始めの時刻（`rtos/analysis.py:389`）、優先度の逆転で別々の時刻に読んだ語を同時とみなす（:483-504） | 済（b2623e98: 点はその値を読んだサンプルの時刻。逆転に pair_read_span・opened_reads） |
| rtos の各 OS | `nuttx.py:262-265`（期限切れを 0、32 ビットの折り返しなし）、`ucos3_objects.py:189-191`（読めなければ経過 0）、`zephyr_objects.py:140-156`（経過を含まない印なし）、`rtx5.py:199`・`threadx.py:241`・`ucos3.py:242`（0xFFFF_FFFF の決め打ち） | 済（22c15330: NuttX はティックの幅で回して overdue_ticks、ほかはティックの変数の幅（tick_mask）、µC/OS-III は経過が読めなければ None、Zephyr は remaining_excludes） |
| flow・model | `flow.py:263, 269-272`（t1 の暗黙の既定が平均を変える）、`model.py:174-177`（isr_exit で番号を見ずにスタックを取り出す。`flow.isr_pairing` と食い違い） | 済（09166ab7: t1 が無ければオブジェクトの最後の点まで、使った終わり mean_t1。出口が番号を持てば番号で対に） |
| タイムベースのリセット（N8） | 0 に戻るのはビットストリームの書き込み・電源・クロックのロック外れ。Recorder.add は単調性を見ない（`recording.py:162-175`）、ClockMap は前後の時代を 1 本の直線に当てはめる（:54-85）、`Collector._read_new`（`collector.py:292-295`）は written < _seen の間黙って待ち、超えたら前の件を数えずに失う（`EventCursor` は restarts に数え食い違う）、`obs_decoder.py:127, 257` は 2^47 未満の戻りを後ろへの時刻にする、`probe_design._switch_design`・`recover_link` は記録を止めない。直し方: 時代の切れ目を検出して事実として記録し、前後の時代を混ぜない | 済（61daecbe: 設計 §14.1。obs_decoder の 2^47 は残した: 記録の中は切れ目で記録が止まる。記録の外は S3 と一緒に） |

## 10. 段階 S1 の実装（2026-11-02）

シミュレーション（GHDL + cocotb）とホストの試験まで。ビルド・実機では確かめていない（§10.3）。

### 10.1 直したこと

| §2.1 | 前 | 後 | 所 |
|---|---|---|---|
| 4 連続記録のサンプル 0（R1） | 回路に記録が無く、ホストが往復の読み（とトリガ時刻）で範囲を出していた（S0） | 1 周期 1 サンプルの深いバッファ（`la_deep_buffer` の LANES = 1）が、run の後の最初のサンプル（`la_front` の sample_valid）を**トリガ時刻と同じ段**（符号器が入力を受けた周期の次の周期の time）で `start_time` に持つ。サンプル i のピンの時刻 = start_time + i × (rate + 1) − LA_DELAY の深いバッファの段 − 位相の幅（トリガと同じ式）。CSR `la_deep.start_time`・`info.start_valid`（既存の窓。lanes > 1 の構成だけが使っていた）で読める。`start_valid` は run の 0→1 で 0、最初のサンプルで 1。申告は TLV TIMING の `la_deep_start`（記録しない古い回路は申告しない = S0 の範囲のまま。ケイパビリティで分ける）。ホストは申告があれば CSR の 1 点を使う（`stream_start_recorded`。method "circuit"、不確かさ 0）。cocotb: 毎周期のカウンタで、rate 0・3 の先頭 64 サンプルの時刻が式どおり | `la_deep_buffer.vhd`、`fprobe_map.py`（文書）、`fprobe/timing.py`、`recording_stream.py`、`la_deep_buffer_tb.py` |
| 2 内部の ch（R2） | フィルタ（3 段）を通らない内部の ch がピンより 3 刻み早く見えた（S0 はホストが申告 +3 で直していた） | 新しい部品 `la_probe_inputs`（外の信号のフィルタ・内部の ch の段数合わせ・ch の並びの組み立て）。内部の 2 本を `LA_FILTER_DELAY`（`timing_pkg`）段のレジスタで遅らせる。cocotb で「時刻 T の辺で変わった FF」と「その辺の直後に変わったピン」が同じ周期に出ることを確かめた（直す前の形で 3 刻み食い違うことも確かめた）。LA_CH_TIMING の内部の ch は 0 になった（`fprobe_gw/timing.py` の生成と一致）。ホストは変えていない（申告を読むだけ。古い回路は +3 の申告のまま直る） | `la_probe_inputs.vhd`（新設）、`fprobe_core.vhd`、`fprobe_gw/timing.py`、`la_probe_inputs_tb.py` |
| 3 取り込みの領域の内部の ch（R2） | sys → cap の 1 段だけで、ピン（cap の領域のフィルタ 3 段）と違う段数（S0 は「分からない」と申告） | cocotb で確かめた: 直す前は内部の ch が 2 cap 周期早い。`la_cap_domain` も `la_probe_inputs`（INTERNAL_AGE = 1: sys → cap の段を含めて 3 段）を使い、RATIO 2・3・4 で同じサンプルに来る。LA_CH_TIMING の内部の ch は 0。あわせて、LA_FILTER = false の構成でも同期化と揃えの段を同じにした（フィルタの ch の選びを 0 にして la_filter を通す）。それまでは latency（5 − RATIO）がフィルタの段を含む値のまま、フィルタの無い構成では 3 cap 周期ずれていた（その構成の製品は無い）。latency の定数は `timing_pkg` の `LA_CAP_LATENCY_BASE` から | `la_cap_domain.vhd`、`la_cap_domain_tb.py`・`test_la_cap_domain.py` |
| 11・13 UART 受信・SWO の UART 方式（R3） | 受け渡し（valid and ready）の周期 + 1。ホストへの経路の待ち w が入り、記録されない | `uart_rx` が開始ビットの辺を見つけた周期（S_IDLE で同期化した線が 0 になった周期）の time を取り、バイトと一緒に運ぶ（`out_time`）。端子の立ち下がり = out_time − `UART_RX_START_DELAY`（2）− 1 以上、out_time − 2 未満。divisor の偶数・奇数、受け手の待ちに依らない（cocotb）。`uart_bridge`・`swo_receiver` がそのまま出し、`event_collector` は受信・SWO のバイトに源の時刻を使う（送信は今までどおり受け渡し + 1）。`event_byte_pack` は受けた周期でなく源の時刻の差を詰める（差が 12 ビットに収まらなければ件を分ける。溜める時間の上限は今までどおり最後のバイトを受けてから 4096 周期） | `uart_rx.vhd`、`uart_bridge.vhd`、`swo_receiver.vhd`、`event_collector.vhd`、`event_byte_pack.vhd`、`fprobe_core.vhd`、各 tb |
| 12 SWO の Manchester 方式 | バイトを出した周期 + 1 | バイトを出した周期（`swo_receiver` が出す時刻。集約の 1 段は時刻を変えない）= 最後のビットの中央の変化 + `SWO_MANCHESTER_DELAY`（3。cocotb）。開始の辺からの位置は送り手の速さで決まり記録されない（R4 は S3）。TIMING の `swo_manchester` が `sender_rate` を含まない項目として申告する | `swo_receiver.vhd`、`swo_receiver_modes_tb.py` |
| — 時刻の表・TLV TIMING | `fprobe_gw/timing.py` はロジアナと同期の段数だけ | 情報源ごとの点の申告（TLV 25 TIMING、1 件 8 バイト: 情報源・点・単位・含まない項目・段数 i16・位相の幅 u16）。S1 の情報源: `la`・`la_cap`（単位 cap の周期）・`la_deep`・`la_deep_start`・`uart_rx`・`uart_tx`・`swo_uart`・`swo_manchester`・`sync_rx`・`sync_tx`。DAP の点は S2（R5〜R7）で足した（§12。情報源 `dap_swd`・`dap_jtag`）。申告が無ければ §2.1 のいまの意味。VHDL のパッケージ `rtl/core/generated/timing_pkg.vhd`（`python -m fprobe_gw gen`）を RTL が段数に使う（`la_probe_inputs` の揃え、`la_cap_domain` の latency）。ホストは `Capabilities.timing`（名前 → `TimingPoint`）、`fprobe.timing.source_timing`（申告か、申告の無い古い回路の意味 `_LEGACY`）・`byte_edge_window` | `fprobe_gw/timing.py`・`caps.py`・`generate.py`・`designs.py`、`host/fprobe/caps.py`・`timing.py` |

### 10.2 API の出力の変化

- `recording_la_stream_start`・`recording_la_stream_stop` の `anchor`（深いバッファの 1 周期 1 サンプルの新しい回路）: `method` "circuit"、
  `t_lo` = `t_hi` = `ticks`（ピンの時間軸のサンプル 0）、`uncertainty_ticks` 0、`basis` に `start_time`（回路の記録）と前後の読み（根拠として
  残すだけ）。記録の `la` の `stream_start` も同じ。古い回路・lanes > 1 の構成は S0 のまま（記録する回路で `start_valid` が 0 だったときは
  S0 の範囲に `basis.start_valid = false` を付ける）。
- `events_read`（raw = false）: UART・SWO のバイト（kind "uart_rx" / "uart_tx" / "swo"）に `line`（`point`: "start_bit_edge" / "last_bit_center"、
  `t_lo_us` 以上 `t_hi_us` 未満、`from`: "TIMING" / "legacy"、`unknown`: 含まない項目の名前）。申告の無い古い回路の UART 受信・SWO は範囲が null
  （受け渡しの時刻で、待ちが記録されない）。SWO は、この道具の `swo_start` で選んだ方式を使い、`swo_start` を呼んでいなければ範囲を出さない
  （`unknown: ["swo_mode"]`。ほかの道具が設定した方式は知らない）。
- イベントの時刻そのものの意味（種類 3・13 の UART 受信、5・15 の SWO）: 新しい回路では開始ビットの辺を見つけた周期（受け渡しより約 9.5
  ビット + 待ちだけ前）。Manchester は 1 刻み前（バイトを出した周期）。種類の番号は同じで、TLV TIMING で区別する（§6 の判断 3）。
  これを時刻に使う画面・解析（ターミナルの表示、`recording_correlate` 等）は、受信の時刻が前に移る。
- `la_collect`・`la_deep_collect` の `channel_timing`: 新しい回路の内部の ch は `skew_ticks` 0（前は 3）、取り込みの領域の構成も 0（前は null）。
  `first_edges_ns` の内部の ch の +30 ns が無くなる（回路で揃った）。
- 内部の ch を条件にした多段トリガ・パルス幅のトリガ（sys の経路）は、内部の ch が 3 刻み遅れて届くので、成立が 3 刻み遅れる
  （ロジアナの記録の上の位置とは揃った）。
- ケイパビリティ: TLV 25 TIMING。ホストの `Capabilities.timing`。CSR `la_deep.info.start_valid` は 1 周期 1 サンプルの深いバッファでも 1 になる。

### 10.3 実機で確かめること（未確認）

- ビルド: `fprobe_pynq_deep`（R1・R2・R3）、`fprobe_pynq_cap`（取り込みの領域の R2）、`fprobe_pynq`（R2・R3）。Primer の `fprobe`・`fprobe_la`
  （R2・R3。面積の増分は内部の 2 本 × 3 段の FF と、UART・SWO の時刻の 48 ビットの保持が数か所）。タイミング（100 MHz）を満たすこと、
  EX1998 が無いこと。`event_byte_pack` に 48 ビットの引き算と比較が源ごとに増えた（前は 13 ビットのカウンタだけ。sys を上げると遅い経路に
  なりうる）。
- 見る値: (1) `recording_la_stream_start` の `anchor.method` が "circuit" で、`basis.start_time` が `basis.before`〜`basis.after` の間にある。
  (2) `la_testgen` の方形波をピンに入れ、内部の ch（`la_cap.control.selftest` のある構成は sys の周期ごとの反転）と同じ辺の位置が揃うこと
  （取り込みの領域の構成）。(3) UART の TX → RX の折り返しで、送信のイベントの時刻（`line`）と受信の `line.t_lo_us`〜`t_hi_us` の差が
  入出力の遅れ（数 ns）+ 1 刻み以内であること。ホストが受信を遅く読むときも変わらないこと。(4) Nucleo の SWO（UART 方式）で、バイトの
  イベントの `line` が SWO の線をロジアナで見た開始ビットの辺と合うこと。

**実機の結果（2026-11-03、8f538b91 = S1〜S3・H2 を含む。`C:\fw79` = fprobe_pynq_deep、`C:\fw80` = fprobe_pynq_deep_fast）**

- (1) fw79: `recording_la_stream_start` の `anchor.method` = "circuit"、`basis.start_time` 378158137 が `before` 372461843〜`after` 378370024 の間。
- (3) fw80、UART の TX（JA10）→ RX（JA8）の折り返し、115200・1 Mbaud × 受信を待たずに / 0.5 秒遅く読む、各 32 バイト: 受信の `line` の範囲は
  送信の `line` の [+20, +30) ns に毎回同じ（遅く読んでも変わらない）。同じ線をロジアナ（800 MS/s、ch 7 = JA10・ch 5 = JA8）で見ると、
  JA10 の立ち下がりは送信の `line` の +2.5 ns（出力の遅れ。申告と合う）、JA8 の立ち下がりは +20.0 ns（受信の範囲の中）。受信の範囲と送信の
  点の差 20 ns は回路の誤りではなく、JA10 → JA8 の線の上の遅れ（PMOD の直列抵抗とジャンパ線・端子の容量の RC と見る。推測）。両方の申告は
  第三者の観測（ロジアナ）と合う。前提: ロジアナの ch 5・ch 7 の段数が同じ。
- (2) `C:\fw81` = 8f538b91 の `fprobe_pynq_cap`（sys 107.5・cap 300.4 MHz）: 内部の ch 9（selftest、sys の周期ごとの反転）と方形波のピン ch 10
  （RPi 32 → JB 1）を同時に取ると、ピンの辺 − 直前の内部の辺は edge 0 で {0, +2}、edge 1 で {0, +1} サンプル（各 273 回、流れ全体で一定）。
  ただし `la_testgen` は cap（300 MHz）で刻むので sys の辺との位相が決まっておらず、「揃っている」かはこの組では確かめられない
  （sys の辺で変わるピンの信号が要る。未確認のまま）。(4) は SWO の対象が要る（未確認）。

### 10.4 残したこと

- **ストリームの最初の見出しに start_time を載せる**（R1 の後半）: 実装していない。PL が見出しを作る経路（`STREAM_EVENTS`）は見出しの
  128 ビットに空きが 8 ビットしか無く、新しいかたまりの種類（見出し + 1 拍）が要る。PYNQ の深いバッファは PS が見出しを作る
  （`host/pynq/fprobe-relay/relay.c`）ので中継の C も変える必要があり、PYNQ が別の作業で使われているため実機で確かめられない。
  CSR は run の 0→1 で `start_valid` が 0 に戻るので、読んだ値は今の流れのもの（取り違えは無い）。見出しに載せる形（種類の番号・PL と
  PS の両方）は主担当の判断を待つ。
- **1 周期に複数のサンプルの構成**（→ S1b で済。§11）（`fprobe_pynq_fast`・`fprobe_pynq_deep_fast`、la_lanes > 1）: 内部の ch・sys で取るピンの ch のずれは
  「分からない」のまま（la-resolution の担当の回路。R2 の揃えは入れていない）。TIMING にもロジアナの点を出さない（軸は CSR の latency の式）。
  `fprobe_pynq_deep_fast` の連続記録の記録への入れ方（`anchor_stream`）は S0 の範囲のまま（細かいサンプルの起点は刻みの整数にならない）。
- **時刻の約束の 1 刻みのずれの疑い**（→ S1b で事実と確かめて直した。§11.1 の最初の行）（S0 からのもの。直していない）: 試験は「時刻が T になる辺の直後に入力を変えると、回路の時刻が
  T + delay」で段数を測り、ホストは入力の変化を [時刻 − delay − span, 時刻 − delay) = [T − 1, T) と返す（`la.pin_time`・
  `timing.stream_start_window`・`byte_edge_window`）。辺の直後に変わった入力は、物理的には時刻 T の周期の中（[T, T + 1)）で変わって
  いるので、ホストの範囲は 1 刻み早い可能性がある（導出。既知の時刻の辺を入れた実機では確かめていない）。S1 の UART 受信は既存の
  ロジアナの約束に合わせた（同じ約束なら源の間の前後関係は崩れない）。直すなら `pin_time` 等を同時に変えることになり S0 の出力が
  変わるので、主担当の判断を待つ。
- `la_stream`（VCD への直接の流し込み、`VcdWriter`）: 内部の ch は回路で揃ったので新しい回路では不要になったが、フィルタをかけた ch の
  ずれ（設定）と古い回路の内部の ch（+3）は、まだ入れていない。
- 記録の不確かさの列（`unc`）を `t_lo`・`t_hi`・根拠に置き換えること、PC 側の出来事（19）の窓（§4.5・§8.3）: S1 の段の表（§5.3）に
  無いので触っていない。
- バイトの「線上の範囲」は `events_read` だけに付けた。記録（`recording.py` の `uart`・`swo` の流れ）の本体には入れていない。
- §5.1 の「既存の確かめを時刻の表から値を取る形に書き換える」: la_filter・la_capture・deep_axi・la_deep_buffer・la_probe_inputs・
  uart・swo の試験は時刻の表（`fprobe_gw.timing`）の値と比べる。`sync_link_tb` は回路の定数と比べたままで、`sync_link.vhd` も
  `timing_pkg` を使っていない。

## 11. 段階 S1b の実装（2026-11-02）

§9.1・§9.5 の段 S1b の項目と、§10.4 の 2（辺と時刻の約束の 1 刻み）。シミュレーション（GHDL + cocotb）とホストの試験まで。
ビルド・実機では確かめていない（§11.3）。コミット: eab33581（回路・時刻の表・cocotb）、21efa776（ホスト・API の文書）、この文書は その次のコミット。
試験（2026-11-02）: `pytest gateware` 484 通過（322 は実機・飛ばし、2 は既知の xfail）・1 失敗（既知の `test_obs_e2e.py::test_overflow_lost_counts[False-3-2]`。HEAD でも落ちる）、`pytest host` 4245 通過・33 飛ばし・54 xfail・失敗 0。host/ui の `npm test` は走らせていない（作業ツリーに node_modules が無い。画面のコードは変えていない）。

### 11.1 直したこと

| 項目 | 前 | 後 | 所 |
|---|---|---|---|
| §10.4 の 2 辺と時刻の約束 | 試験は「時刻が T になる辺の直後（周期 [T, T + 1) の中）に変えた入力 → 回路の時刻 T + delay」で段数を数え、ホストは変化を [時刻 − delay − span, 時刻 − delay) = [T − 1, T) と返していた（sys の経路のロジアナ・深いバッファの起点・UART・SWO のバイト・ボードの間の同期）。取り込みの領域・1 周期に複数のサンプルの経路（細かい周期 n。辺 n の後 = [n, n + 1)）とは 1 刻み食い違っていた | 時刻 T の瞬間 = タイムベースが T になる立ち上がり（出力の点の約束と同じ）。非同期の入力の点は [時刻 − delay, 時刻 − delay + span)。cocotb でシミュレーションの ps を明示して確かめた（`tests/tb/common.py` の `TimebaseClock`: 立ち上がりの ps とその辺で置いた値を記録し、入力を周期の中の 1 ps〜9.999 ns の位置で変える）: `la_capture_tb.the_host_pin_window_contains_the_true_edge`（直す前のホストの式で失敗: 本当の位置 1021.0001 に対して範囲 [1020, 1021)）、`uart_rx_tb.the_host_window_contains_the_true_start_bit_edge`、`la_deep_buffer_tb.stream_start_time_places_every_sample_on_the_trigger_time_axis`（ホストの `stream_start_recorded` で比べる形に）、`la_cap_domain_tb.the_cap_cycle_number_is_ratio_times_the_timebase`（cap の辺 T × RATIO = sys の辺 T。取り込みの領域の式は元から正しかった）。ボードの間の同期（`sync_link_tb` は線が 1 になった周期 T → 子の時刻 T + 2）は `timesync` の最も前の辺を 子の時刻 − RX（前は − RX − 幅）に直した | `fprobe/la.py` `pin_time`、`fprobe/timing.py`（`byte_edge_window`・`stream_start_recorded`・`stream_start_window`）、`fprobe/timesync.py`、`fprobe_gw/timing.py` の冒頭・`TimingPoint`、`fprobe/caps.py` の注 |
| A2 多段トリガの発火の時刻 | 表に無い。seq_time はピン・事象から何段かを申告していない | cocotb `trig_seq_tb.the_fire_cycle_is_the_table_delay_after_the_event`: trig_seq の入り口の辺の直後に変わった probes → fire は 4 周期後、match・host のパルス → 2 周期後（fprobe_core は fire の周期の time を取る）。入り口の前の段は構成ごと: sys の構成はグリッチフィルタ 3（内部の ch は揃えの 3）、取り込みの領域・1 周期に複数のサンプルの構成は素の値。1 周期に複数のサンプルの専用の入力は最も新しいレーン（細かい辺 N × w − IN_AGE）: seq_time × N − n ∈ (4N + IN_AGE, 5N + IN_AGE]。TIMING の `seq_pin`・`seq_internal`（幅 0）・`seq_input_lanes`（単位 fine_sample、幅 N）・`seq_event`（点 source_pulse）。ホスト `timing.seq_fire_window`、`xtrig.sequence_status` の `fire_windows`（最後の段の条件の事象ごと。パターンは使っている ch の種類ごと。どれが進めたかは回路が記録しないので選ばない） | `fprobe_gw/timing.py`、`trig_seq_tb.py`、`fprobe/timing.py`、`probe_xtrig.py` |
| A6 テストポイントの流れの起点 | 前後の読みの中点 ± 幅の半分（ピンの段数・位相の幅を引かない） | `recording_stream.anchor_stream` と同じ（回路が記録した起点の 1 点。記録しない古い回路は `timing.stream_start_window` の範囲）。結果の `settings.anchor` に根拠 | `probe_testpoint.py` |
| A8・N9 取り込みの領域・レーンの構成の `la_trigger_status` | フィルタ・パルス幅の ns を `sys_clock_hz` で換算（`fprobe_pynq_cap` で 3 倍長い）、`rate_hz` は rate をそのまま、`trigger_time` は CSR の生の値 | ns は `la_sample_hz`（`la.Front` と同じ取り込みのクロック）。`rate_hz`: 1 周期に複数のサンプルの内蔵バッファは `la_sample_hz`（rate を使わない: `rate_used` false）、深いバッファの N 並列は la_sample_hz / (2^step × (rate + 1))、`rate_raw` はレジスタの値。`trigger_time` はピンの時間軸の時刻（`la_collect` と同じ意味）、生の値は `trigger_time_raw`。ほかに `trigger_time_hi`・`trigger_fine_sample`・`xtrig_source_time`・`trigger_time_from`・`trigger_time_unknown`（`timing.la_trigger_facts`） | `probe_la.py`、`fprobe/timing.py` |
| A9・N2 1 周期に複数のサンプルの構成 | 内部の ch は全レーンに今の値を並べ、細かい ch の軸より N + IN_AGE 細かいサンプル早く見えた（LA_CH_TIMING は「分からない」）。sys で取るピンも「分からない」。TIMING にロジアナの点が無い。連続記録の起点は S0 の範囲 | 新しい部品 `la_lane_inputs`（fprobe_core のレーンの組み立てを切り出した）: レーン k に、そのサンプルの細かい辺の前の sys の周期の値を入れる（内部の ch: 1 − floor((k − IN_AGE)/N) 周期前、sys で取るピン: それより 1 周期新しい）。内部の ch はずれ 0・幅 0、sys で取るピンはずれ −1 刻み・幅 1 刻み（揃える前の形より早くは並べられない）。cocotb `la_lane_inputs_tb`（N 8・6・4、IN_AGE 5・3・0・6）。IN_AGE の正は `fprobe_gw.designs.pynq_z2_iserdes_newest_age`（ケイパビリティの `la_lane_in_age`・生成パッケージの `LA_LANE_IN_AGE`。`xilinx_la_iserdes_tb` が ISERDESE2 の模型で、`pynq_z2_fprobe_top` が VHDL の関数との一致を elaboration で確かめる）。TIMING の `la_lanes`（latency = 4N + IN_AGE、`la_lane_e2e_tb`）・`la_deep_lanes`（N + IN_AGE、`la_deep_lanes_tb`）。連続記録の起点（`fprobe_pynq_deep_fast`）は回路の start_time から細かい辺 start_time × N − latency（`timing.stream_start_lanes`。刻みに切り捨てて置き、不確かさは端数） | `la_lane_inputs.vhd`（新設）、`fprobe_core.vhd`、`fprobe_gw/timing.py`・`caps.py`・`designs.py`、`pynq_z2_fprobe_top.vhd`、`recording_stream.py` |
| N3 外部トリガ（クロストリガ → ロジアナ） | 外部トリガを遅らせず、トリガのサンプルが `la_ext` の辺より前のピンのもの（sys・フィルタあり 5 刻み、なし 2、cap 4 cap 周期、レーン約 3 sys + IN_AGE）。`la_cap_domain.vhd:17` の注「ext は遅らせない（ピンとの時間関係を保つ）」は実際と違った | 回路で揃えた（源の種類は記録しない: 外部トリガとパターンのトリガが同じ意味の時刻になる）。`la_front` の EXT_DELAY（= LA_FRONT_EXT_ALIGN 2 + probes が前に余計に通った段数。sys: + フィルタ 3、cap: + 3 − 1）、`la_lane_front` の EXT_DELAY（2 + IN_AGE / N）・EXT_LANE（IN_AGE mod N。外部トリガも成立の印に入れる）。`la_ext` が 1 の周期 X → トリガのサンプルのピンの時間軸の時刻が X（rate > 0 なら X 以後の最初のサンプル）。cocotb: `la_capture_tb`（通常・FAST）・`la_deep_buffer_tb`・`la_cap_domain_tb`（RATIO 2〜4、sys の周期の中の全部の位相）・`la_lane_e2e_tb`（N 6・8、N + 1 通りの周期）。TIMING の `la_ext`（源の周期 S → ピンの時刻 S + 1）、ホスト `timing.ext_trigger_window` | `la_front.vhd`、`la_capture.vhd`、`la_cap_domain.vhd`、`la_deep_buffer.vhd`（generic 1 つ）、`la_lane_front.vhd`、`la_lane_capture.vhd`、`fprobe_core.vhd` |
| B7 ロジアナのトリガ → クロストリガの源、深いバッファの詰め | 表に無い | cocotb で確かめて申告: sys の内蔵 源 = trigger_time + 1（`la_record` の state の段。`la_capture_tb`）、深いバッファ 源 = trigger_time（`la_deep_buffer_tb`）、詰め（pack ≠ 0、1 語 k サンプル）は trigger_time がトリガのサンプルより 1 + (k − 1 − j) × (rate + 1) 周期遅い（条件なしのトリガで trigger_time − start_time = 1 + (k − 1)(rate + 1)。`la_deep_buffer_tb.packing_delays_the_trigger_by_the_rest_of_the_word`）、取り込みの領域 源 × RATIO − n ∈ [8, 7 + RATIO]（全部の位相で両端に来る。`la_cap_domain_tb`）、1 周期に複数のサンプル 源 × N − n ∈ [latency + 1, latency + N]（`la_lane_e2e_tb`）。TIMING の `la_xtrig`・`la_deep_xtrig`・`la_deep_packed`・`la_cap_xtrig`・`la_lanes_xtrig`。ホスト `timing.la_trigger_facts`（詰めはピンの時刻を範囲で）・`la_xtrig_source` | `fprobe_gw/timing.py`、各 tb、`fprobe/timing.py`、`probe_la.py` |
| N1 取り込みの領域の内部の ch | S1 の R2 で揃ったと報告 | cocotb の `internal_channels_land_on_the_same_sample_as_a_pin_changed_on_the_same_edge` が RATIO 2・3・4 で通ることを確かめた（回路は変えていない） | — |
| N5 `LA_FILTER = false` かつ `LA_PULSE = true` | 拒まない（la_pulse が同期化していない値を 1 段で取る） | `fprobe_gw.caps.Capabilities` が断る（sys の構成。取り込みの領域は la_cap_domain が常に同期化するので許す）、`fprobe_core` が elaboration で止める | `caps.py`、`fprobe_core.vhd` |
| 表 3・16・22・23、D3 | — | §2.1 の 3（N1）・16（N3・B7）を事実に直し、22（多段トリガ）・23（1 周期に複数のサンプル）を足した。§2 の古い行番号を直した | この文書 |

### 11.2 API の出力の変化

- **ピンの時刻が 1 刻み（PYNQ 10 ns）後ろになる**（sys の経路の全部。正しさの直し）: `la_collect`・`la_deep_collect` の `trigger_time`、
  記録の波形の置き場所・`la_view`・共通タイムライン・VCD・Perfetto の絶対時刻（トリガからの相対の `first_edges_ns` は変わらない）、
  `recording_la_stream_start`・`stop` の `anchor`（`ticks`・`t_lo`・`t_hi`。範囲の幅は同じ）、`events_read` の UART・SWO のバイトの
  `line.t_lo_us`・`t_hi_us`。取り込みの領域・1 周期に複数のサンプルの構成の `trigger_time`・`trigger_fine_sample` は変わらない（元から正しかった）。
- ボードの間の同期の換算（`timesync.to_parent`・`interpolator`・記録の `merge`）: 子の時刻を親に直した値が 1 刻み前になる（前は 1 刻み遅かった）。
- 外部トリガ（クロストリガ → ロジアナ）で捕捉したとき: 新しい回路では、トリガのサンプルが外部トリガの来た周期のもの（`trigger_time` が
  外部トリガの源の周期 + 1。前は sys・フィルタありで 5 刻み前のサンプル）。トリガの前のサンプル数（`trigger_index` の前）は同じで、記録される
  区間が外部トリガの遅れの分だけ後ろへずれる。
- `la_trigger_status`: `trigger_time` が生の値からピンの時間軸の時刻に変わった（生の値は `trigger_time_raw`）。`trigger_time_hi`・
  `trigger_fine_sample`・`xtrig_source_time`・`trigger_time_from`・`trigger_time_unknown`・`rate_raw`・`rate_used` が増えた。取り込みの領域の
  構成の `filter.min_width_ns`・`pulse.min_ns`・`max_ns` が 1/3 になった（cap の周期で数える）。1 周期に複数のサンプルの構成の `rate_hz` が
  直った。画面（クロストリガの画面の捕捉の状態）は `trigger_time` をそのまま描くので、ピンの時刻が出る。
- `la_collect`・`la_deep_collect`: `xtrig_source_time` が増えた。
- `xtrig_sequence_status`: `fire_windows` が増えた。
- `testpoint_start`（stream）・`testpoint_stop`: `settings.anchor` が増え、写しの起点が `anchor_stream` の決め方になった（古い回路で約
  ピンの段数 + 1 刻み前へ、新しい回路は回路の 1 点で不確かさ 0）。
- ケイパビリティ: TLV TIMING に情報源 11〜22（`la_lanes`・`la_deep_lanes`・`la_ext`・`la_xtrig`・`la_deep_xtrig`・`la_deep_packed`・
  `la_cap_xtrig`・`la_lanes_xtrig`・`seq_pin`・`seq_internal`・`seq_input_lanes`・`seq_event`）、点 5・6（`source_pulse`・`internal_edge`）、
  単位 2（`fine_sample`）。古いホストは知らない情報源を読み飛ばす。LA_CH_TIMING の 1 周期に複数のサンプルの構成の内部の ch は 0（前は分からない）。
- 1 周期に複数のサンプルの構成の内部の ch（ch 8・9）の波形: 新しい回路では細かい ch の時間軸に揃い、前より N + IN_AGE（13）細かいサンプル
  後ろに出る。内部の ch を条件にした 1 周期に複数のサンプルのトリガは、同じだけ遅れて成立する。
- 記録の連続記録（`fprobe_pynq_deep_fast`）の `anchor`: `method` "circuit"、`basis.fine_sample`（細かい辺）、`uncertainty_ticks` は刻みの端数。

### 11.3 実機で確かめること（未確認）

- ビルド: 全部の PYNQ の構成（`fprobe_pynq`・`_deep`・`_cap`・`_fast`・`_deep_fast`）と Primer の `fprobe`・`fprobe_la`。タイミング
  （100 MHz・cap 300 / 400 MHz）、EX1998 が無いこと。増えた FF: 外部トリガの遅れ（sys 5 段・cap 4 段・レーン 2 段）、`la_lane_inputs` の
  内部の 2 本 × 2 段とピン 8 本 × 2 段。`pynq_z2_fprobe_top` の IN_AGE の一致の assert が Vivado の elaboration を通ること。
- 見る値: (1) `la_testgen` の方形波をピンに入れ、`la_collect` の `trigger_time` と、同じ辺を見た別の情報源（UART の TX → RX の折り返し、
  クロストリガの出力の端子をロジアナの別のピンに戻した辺）の時刻が、出力・入力の遅れ（数 ns）+ 1 刻み以内で揃うこと（S0〜S1 では 1 刻み
  食い違っていたはず）。(2) クロストリガ（`xtrig_fire` の host → ロジアナ、external_only）で捕捉し、`trigger_time` が host を書いた周期 + 1
  付近（CSR の書きの時刻は分からないので、往復の窓の中）にあること。(3) 番地一致を源にした外部トリガで、`match_time` + 1 = トリガのサンプルの
  ピンの時刻（rate 0）。(4) `fprobe_pynq_fast` で `la_cap.control.selftest`（sys の周期ごとに反転）を ch 9 に入れ、その変化が細かいサンプルの
  N の倍数の位置（軸の上で sys の辺）に来ること。(5) 多段トリガのパターン A（ピン）で発火させ、`fire_windows` の範囲にロジアナで見たピンの辺が
  入ること。

### 11.4 残したこと

- 外部トリガかパターンかの記録: 回路で揃えたので時刻の意味は同じになったが、どちらで成立したかは記録しない（ホストは仕掛けた条件で読む）。
  両方が成り立つ条件では区別できない。
- 深いバッファの詰め（pack ≠ 0）のトリガのサンプルが語の何番目か: 回路が記録しないので範囲で返す（`la_pack` の hit_acc を位置にすれば
  1 点になるが、深いバッファの段 B が並行して触っているので触らない）。ホストの `DeepAnalyzer.start` は詰めずに捕捉するので、トリガの捕捉は
  影響を受けない（詰めるのは垂れ流しだけ）。
- 古い回路（TLV TIMING に `la_deep_packed` の無い、詰めのある回路）の連続記録の起点の範囲（`stream_start_window`）は、トリガ時刻が詰めの分
  遅いので上の端が緩い（正しい上限のまま。段数をホストに持たないので詰めの分を引かない）。
- `la_lane_front` の `arm` の口は使わなくなった（外部トリガも成立の印に入れ、トリガ待ちかは `la_record` が見る）。口は `la_front` と同じ形に残した。
- §10.4 の残り（ストリームの見出しの start_time、`la_stream` の VCD のずれ、記録の不確かさの列、バイトの線上の範囲の記録への持ち込み、
  `sync_link.vhd` の `timing_pkg`）は S1b の範囲の外で、そのまま。

## 12. 段階 S2 の実装（2026-11-02）

R5（DAP の時刻を線上の点で）・R6（サンプラの窓）・R7（PHY の設定のイベント）。S1（R1・R2・R3）とは別の作業ツリーで並行に作った。
申告はケイパビリティ FEATURES の bit16 `DAP_LINE_TIME`（ホスト `Capabilities.dap_line_time`）と、TLV 25 TIMING（S1）の情報源
`dap_swd`・`dap_jtag`（端子の段。ホスト `Capabilities.dap_output_ticks`）。並行して作ったときは別の TLV 25 `DAP_TIMING` だったが、
S1 の TIMING と番号がぶつかったので、統合で TIMING に入れた（§12.4）。

### 12.1 直したこと

| §2.1 | 前 | 後 | 所 |
|---|---|---|---|
| 5・6 DAP の転送（ホスト。種類 1・12） | PHY の完了の 2 刻み後（JTAG は IR の省略・RDBUFF の追加で点がばらつく） | 線上の点（§6 の判断 1）: SWD = ACK の 3 ビット目の Low 相が終わって SWCLK が 1 になった最初の周期、JTAG = その転送の（DP の読みは最初の）DR スキャンの Update-DR を出る TCK が 1 になった最初の周期。PHY が `rsp_time` として応答と一緒に出し、`debug_port` が応答と同じ 1 段で運び、`dap_monitor` の `ev_time` → `event_collector` が使う。転送でない命令の `rsp_time` は受理した周期 | `swd_phy.vhd`・`jtag_phy.vhd`・`debug_port.vhd`・`dap_monitor.vhd`・`event_collector.vhd` |
| 10 割り込み書き込み（種類 11）と CSR | 受けた周期の `time_count`（完了 + 2） | 語の DRW の線上の点（`swd_injector` の `written_time`）。CSR の `xtrig.inject_time`・`inject.first_time` も同じ点。番地一致の `xtrig.match_time` も一致した転送の点（イベント 1 と同じ） | `swd_injector.vhd`・`fprobe_core.vhd`（CSR の時刻の所だけ） |
| 8・9 サンプラの語（種類 7）・値の一致 | 値が返った周期 | 語を読みに行った DRW の転送（WAIT を繰り返したら OK の回）の点 = 窓の始め。[119:115] = 値を返した転送の WAIT の回数（31 で止まる）。窓の後端は「件の並びで次のサンプラの件の時刻」（次の語の 7・値を返した転送の点の 17・誤りの区間の 18）。`sampler_match` の `fire_time`・`prev_time` も標本の時刻 | `dap_sampler.vhd`・`sampler_match.vhd` |
| （新） PHY の設定 | 記録に無い（pyOCD 等が SWJ_Clock 等を変えると式が計算できない） | 種類 16: half_period・idle・turnaround・data_phase・sample_delay（SWD・JTAG）・jtag・enable・鎖の位置。変わった周期と記録を始めた周期に出す（`event_collector` の `CONFIG_EVENTS`。いちばん後の優先順で、出せなければ lost に数える） | `event_collector.vhd`・`fprobe_core.vhd`（`dap_cfg_word`） |
| （新） 端子の段 | PYNQ の JA の IOB の FF（`pynq_z2_fprobe_top.vhd` の `pad_o`/`pad_t`）の 1 段を誰も数えていない | ボードごとの段（`fprobe_gw/timing.py` の `DAP_OUTPUT_TICKS`: PYNQ-Z2 = 1、ほか 0）を TLV TIMING の `dap_swd`・`dap_jtag`（点 `swd_ack3_rise`・`jtag_update_dr_exit`、delay = −段数、含まない項目 `output_delay`）で申告（§12.4）。ホストの範囲（t_lo・t_hi）は端子の時刻（点 + 段）。申告が無ければ範囲を出さない | `fprobe_gw/timing.py`・`caps.py`、`fprobe/timing.py` |

窓の後端を式（「設定で決まる転送の長さ × (1 + WAIT の回数)」、§4.3 の R6 の案）で出すのはやめた。か所の境界（SWD は値が TAR の
書きを挟んで次のか所の最初の DRW で返る）・JTAG の境界の待ち（S_SETTLE・S_READ_BOUNDARY はデータで変わる）・巡回の間の待ち
（up_busy）で、値を返した転送までの長さは設定だけでは決まらないため。代わりに、値を返した転送の点そのものを件の並びに載せる
（次の語の件がそれなら新しい件は要らない。そうでなければ種類 17 を語の直後に出す）。こうすると窓の後端はどの場合も式なしの
事実で、128 ビットの形式のまま入る（§6 の判断 2）。SWD のか所の境界では、本当は直前の TAR の書きの ACK で読みは終わっているが、
窓の後端はその後の DRW の点になる（確かな上限のまま、縮めない）。JTAG の後端は値を返したスキャンの Update-DR の点で、取り込み
（Capture-DR）はその前（確かな上限。縮める式は残した）。

サンプラは確かめた語を buf に積んでから 1 周期 1 件で出すので、語の件・標本は確認の応答の 2 周期後に出る（前は次の周期）。
停止の見張りの「一致した標本のか所の次を読まずに線を渡す」（stop-precision §3.2）を保つため、か所の境界（S_WAIT_BOUNDARY・
S_READ_BOUNDARY）では、最後に出した標本から 2 周期たつ（その値の一致が up_busy に届く）まで次のか所を始めない。転送の途中・
転送を出す前に線を失ったら（切断・方式の切り替え）戻さずに放す（前は応答の無い転送を待ち続けることがありえた。cocotb の切断の試験で
見つかった）。記録を始めると、最初に種類 16 の件が 1 件入る（イベントの数え・番号に入る）。

値を得られなかった読み（バスの誤り・WAIT の使い切り・線の不調・切断）は、黙って捨てずに種類 18（誤りの区間: 最初の読みの点・
見つけた転送の点・最初の番号・読みの数・ACK・STICKYERR・線を失った）にする。これで「窓の後端 = 次のサンプラの件」の規則は、
記録が欠けた（lost）ときを除いて崩れない。

ホスト（`fprobe/timing.py`）: `DapConfig`・`decode_dap_config`、`swd_header_to_point_ticks(hp, trn)` = (10 + trn) × 2 × hp + hp
（ヘッダの最初のビットの Low 相の始めから点まで）、`access_start_bound`（JTAG は点、SWD は転送の始め = SW-DP が読みを始める点は
未確認なので安全側。どちらも + 端子の段）、`sampler_window`、`posted_read_window_line`。`fprobe.events` は種類 16・17・18 を読み、
`resolve(events, line_time, config, output_ticks)` がサンプラの語の `time_lo`・`time_hi` とポストされた読みの範囲を出す。
`collector.add_events` は設定（`Carry.config`）をかたまりをまたいで持ち越す。

確かめたこと（cocotb）: `swd_phy_tb` の `rsp_time_is_the_rise_after_the_third_ack_bit`（hp 2・3・5 × turnaround 1・2・4 ×
sample_delay・data_phase・idle の組 × ACK OK・WAIT・RnW。点が線上の立ち上がりと一致し、ヘッダの始めからの差がホストの式と一致）、
`rsp_time_of_other_commands_is_when_they_were_accepted`、`jtag_phy_tb` の `rsp_time_is_the_rise_leaving_update_dr`（IR を省く・
省かない、DP の読みの RDBUFF、IDCODE・ABORT、hp・idle の組 24 件。モデルの TAP の Update-DR を出る立ち上がりと一致）、
`sampler_match_tb` の `times_are_the_samples_own_times`、`event_collector_tb`（源の時刻を受けた周期と別の値にして、源の時刻が出る）、
`event_collector_config_tb`（設定の件: 基準モデルと毎周期一致・記録を始めた周期の件）、`dap_sampler_tb` の
`swd_sample_times_are_the_read_points_and_the_next_record_ends_the_window`・`jtag_sample_times_and_windows`（語の時刻 = 読みに
行った OK の DRW の点、次のサンプラの件の時刻 = 値を返した転送の点、WAIT の回数、17 の要求と番地）。ホスト
`tests/test_dap_line_time.py`。

### 12.2 API の出力の変化（DAP_LINE_TIME の回路）

- `events_read`（raw = false）: 時刻（`time_us`）は線上の点。ポストされた読みの `time_lo_us` は頼んだ転送の始め（SWD）・
  Update-DR（JTAG）+ 端子の段、`time_hi_us` が増えた（返した転送の点 + 端子の段）。サンプラの語（kind "sample"）は `time_us` =
  読みに行った転送の点（前は値が返った周期 = 1 転送以上後）、`time_lo_us`・`time_hi_us`・`waits` が増えた。新しい kind
  "sample_error"（address = 最初の番号、value = 読みの数、`detail`）と "dap_config"（`detail` = 設定）。raw = true は `waits`・
  `config`・`gap` を返す。
- 記録: stream sample・word の本体 `{t_lo, t_hi, waits}`、新しい stream sample・error と dap・config。ポストされた読みの本体に `t_hi`。
- CSR: `xtrig.inject_time`・`inject.first_time`・`xtrig.match_time`・`sampler_match.time`・`prev` の意味（上の表）。
  `HaltWatchStatus.t_fire` などは同じ値の意味が変わる（halt_watch.py の注）。
- linux_halt の `StopReport` に `assumptions`（上限が頼った仮定）と `bound_without_assumptions_ns`（仮定なしの上限 = PC の時計）。
  FPGA の時刻での上限（`request_times`）は「どのコアも書き込みから停止までが同じ」という仮定に頼るので、それを欄でも出す
  （`skew_upper_bound_ns` は今どおり FPGA の時刻の値）。`linux.halt` の API の stop の dict にも同じ 2 つの欄。
- ケイパビリティ: FEATURES bit16 `DAP_LINE_TIME`、TLV 25 TIMING の情報源 `dap_swd`（23）・`dap_jtag`（24。JTAG の PHY のある構成だけ）と点 `swd_ack3_rise`（7）・`jtag_update_dr_exit`（8）、CSR `sampler.check`（帯域の設計書 2.7）。

### 12.3 残したこと

- 実機（PYNQ）の確かめ（§5.2 の「DAP の時刻」）: 下の手順。端子の段（IOB の FF）は RTL から入れた値で、トップの cocotb は無い。
- JTAG の窓の後端を、値を返したスキャンの Capture-DR まで縮める式（設定で決まる。鎖の位置・区間の境目の 1 周期を含む）。
- SWD のか所の境界の窓の後端を、直前の TAR の書きの ACK まで縮めること（その転送の点を種類 17 で出せばできる。件が増える）。
- 記録のかたまりの最後の語の `t_hi` は、次のかたまりの最初のサンプラの件で決まるが、記録の行は書いた後なので null のまま
  （持ち越しは設定だけ）。`correlate` は語の時刻（いまは読みを頼んだ点）を変化の時刻として使い、`t_lo`・`t_hi` を使っていない。
  → 段 H2 で済（cd7aaa23: 最後の語を持ち越して次のかたまりで t_hi を決める。correlate は time_point と変化の範囲の事実。§14）
- `docs/linux-halt.md` の式の説明（完了 + 2 の前提の段数の議論）は、線上の点の回路では不要になった（`request_times.py` の注に
  書いた）。文書の本文は直していない。→ 段 H2 で済（385473d5）
- 記録が欠けた（lost > 0）区間では、窓の後端の並びの規則は使えない（どの件が欠けたかは分からない）。ホストはまだ区間を区別しない。
  → 段 H2（cd7aaa23）: 後の件は必ず後なので t_hi は確かな上限のまま。かたまりの間で読めなかった件があった後に閉じた語に t_hi_after_loss（§14）
- §2.1 の表 6 は 2026-11-02 の点検で訂正した（IR の省略は Update-DR より前だけを変える、1 ビット = 2·hp）。完了から点までの式は
  導出のまま cocotb で確かめていない（S2 は点を PHY で直接取り、完了との差を使わない）。いまの意味（完了 + 2）の古い回路の
  ポストされた読みの範囲は、引き続き `posted_read_window`（下限 = 前の転送、§8）で出す（点の回路は `posted_read_window_line`）。

### 12.4 申告を TLV TIMING に統合した理由（2026-11-02、S1 と S2 の統合）

S1 の TLV TIMING（情報源ごとの点）と S2 の TLV `DAP_TIMING`（端子の段 u32）は、別の作業ツリーで並行に作ったため、どちらも
TLV の番号 25 だった。片方の番号を空いている番号へ移すのではなく、S2 の申告を TIMING の情報源として入れた（`DAP_TIMING` は無くした）。

- **中身が同じ種類の事実**: `DAP_TIMING` の段数は「時刻（PHY の出力の FF が変わる周期）と端子の辺の差」で、TIMING の 1 件
  （点・delay・span・含まない項目）がそのまま表せる。S1 の時刻の約束の出力の形（`sync_tx`・`uart_tx` と同じ）で、点 = 端子の
  SWCLK / TCK の立ち上がり、delay = −`DAP_OUTPUT_TICKS`、span 0、含まない項目 `output_delay`（FF → 端子の緩衝器・配線）。
  別の TLV にすると、時刻の意味の申告が 2 か所に分かれ、読む側が「どの源の意味がどこにあるか」を覚える必要がある（§4.2 の
  「一か所に持つ」に反する）。
- **SWD と JTAG の点が違う**ことも、情報源を分ければ申告に入る（u32 1 つでは点の名前を運べず、ホストが知っている前提だった）。
  `dap_jtag` は JTAG の PHY のある構成（DEBUG_JTAG）だけが申告する。
- 時刻の意味が変わったこと（完了 + 2 → 線上の点）は、これまでどおり FEATURES bit16 `DAP_LINE_TIME` で分ける（TIMING の件の有無で
  分けることもできるが、種類 16・17・18・CSR `sampler.check` も同じ bit で申告しているので bit を残した）。
- ホストの `Capabilities.dap_output_ticks` は TIMING の `dap_swd`・`dap_jtag` の delay の符号を反した値。申告が無い・2 つの段数が
  揃わない・分からない（delay が −32768）なら None（どちらかを選ばない。範囲の端を出さずに分からないと返すいまの扱いのまま）。
- 番号の確認: TLV は 0〜18・20〜25 が使用中（19 は空き、25 = TIMING）。イベントの種類 16・17・18 は S1・段 H・それ以前のどれも使っていない
  （既存は 1〜15。TLV の LA_LAYOUT 18・UART_FLOW 17 は TLV の番号で、イベントの種類とは別の空間）。TIMING の情報源 23・24、点 7・8 は
  S1・S1b の表（情報源 1〜22、点 1〜6。S1b が並行して 11〜22・5・6 を使った）の次の空き。
- 並行して作られた深いバッファの段 B（`2026-11-01-la-deep-fast-design.md` §12）もイベントの種類 16・FEATURES bit16 を使っていた。
  統合では段 B を種類 19・bit17 へ移し、S2 の 16〜18・bit16 はそのまま（理由は la-deep-fast §12.7）。
- 時刻の約束との関係（S1b の統合で確かめた）: S1b は非同期の入力の点を「時刻 − delay 以上 時刻 − delay + span 未満」に揃えた
  （§11。それまでは 1 刻み前の [時刻 − delay − span, 時刻 − delay)）。DAP の点は出力（プローブが出す辺）なので span = 0 で、点は
  時刻 − delay ちょうど（= rsp_time + 端子の段）。どちらの約束でも同じ値になり、開き・閉じの向きの違いは生じない。
  ホストの S2 の範囲（`sampler_window`・`posted_read_window_line` の `t_lo`・`t_hi`）は、端子の辺の位置の幅ではなく
  「対象のバスのアクセスがあった時刻」の確かな下限・上限で、両端を含む閉じた区間 [t_lo, t_hi]（S1b の `ext_trigger_window`
  と同じ書き方）。端子の辺そのものは幅を持たないので、S1b の半開きの規則と食い違わない。

## 13. 段階 S3 の実装（2026-11-02）

R4（Manchester）・R9（出力の IOB の段）・§9 の段 S3 の項目（A3 の残り・B2・B3・B4・D1・D2・N6）・対象の時計の対応付け（§4.6 の
`clockmap`、§6 の判断 4）・既知の失敗 `test_overflow_lost_counts[False-3-2]`。シミュレーション（GHDL + cocotb）とホストの試験まで。
ビルド（Vivado）・実機では確かめていない（§13.4）。**§2.1 の表の n を直す**（本文の表は文書の担当が直す。直す中身は §13.5）。

### 13.1 直したこと

| 項目 | 前 | 後 | 所 |
|---|---|---|---|
| R4 Manchester の時刻（表 12） | バイトを出した周期 = 最後（8 番目）のビットの中央の変化 + 3（点 `last_bit_center`）。開始の辺からの位置は送り手の速さで決まり、記録されない（含まない項目 `sender_rate`） | `swo_manchester_rx` がバイトごとに**最初（LSB）のデータビットの中央の変化を見つけた周期**の time を運ぶ（`out_time`。`swo_receiver` はそのまま出す）。端子の変化 ∈ [時刻 − 2, 時刻 − 1)（`SWO_MANCHESTER_BIT_DELAY` 2 = 同期化 2 段。点 `first_bit_center`）。ビットごとに同期し直すので送り手の速さ・divisor に依らず、1 フレームの 2 バイト目以降もそのバイトの最初のビット（`sender_rate` を含まない項目から外した）。cocotb `swo_receiver_modes_tb.the_byte_time_is_declared_by_the_timing_table`（送り手の半ビット 4・3（25% 速い）、1〜2 バイトのフレーム）・`swo_events_tb` | `swo_manchester_rx.vhd`・`swo_receiver.vhd`、`fprobe_gw/timing.py` |
| R9・A3 の残り・D1（表 14 UART 送信） | 申告 delay 0（PYNQ の JA の IOB の FF 1 段を数えていない） | ボードの出力の段を一か所に（`BOARD_OUTPUT_TICKS`。PYNQ-Z2 1、ほか 0。S2 の `DAP_OUTPUT_TICKS` を全部の出力に広げた）。`uart_tx` の delay = −段数（PYNQ は端子 = 時刻 + 1） | `fprobe_gw/timing.py` |
| R9（表 18 同期の出力） | PYNQ の `sync_out` は組み合わせで端子（IOB の FF なし。配置で遅れが変わる） | `pynq_z2_fprobe_top` に IOB の FF（`sync_pad`、`IOB = TRUE`）を足し、JA と同じ 1 段に揃えた。`sync_tx` の delay = −(1 + 段数)（PYNQ は −2） | `pynq_z2_fprobe_top.vhd`、`fprobe_gw/timing.py` |
| R9・D2（表 16 クロストリガの出力の端子） | 表は「端子の条件から 4 周期」だけ。源のパルスから端子まで・脈の長さの申告が無い | 新しい情報源 `xtrig_out`（点 `output_edge`: 源のパルスの周期 S → 出力の FF S + 2（`XTRIG_OUT_SOURCE_DELAY`）、端子はさらにボードの段。delay = −(2 + 段数)）と `xtrig_out_pins`（見る端子の辺 → 出力の FF + 4（`XTRIG_OUT_PIN_DELAY`）+ 段数、位相の幅 1）。脈（mode 0）の長さは (L − 1)·16^unit + 1 以上 L·16^unit 以下で、最初の刻みは出力の FF が変わる周期の time から決まる（範囲ではなく 1 つの値）: ホスト `timing.xtrig_out_window`（`t_on`・`t_off`・`pulse_cycles`）。cocotb `xtrig_out_tb.the_output_times_match_the_timing_table`（unit 0〜2・L 1〜3・刻みの中の位置 5 通りで、ホストの式と一致） | `xtrig_out.vhd`（注）、`fprobe_gw/timing.py`、`fprobe/timing.py` |
| B2 補助マイコン（表 15） | 時刻 = 3 バイト目の停止ビットの判定 + 同期 2 + 2（集約が受けた周期の time。BAUD で変わる）。申告なし | `aux_link` が組の**先頭のバイトの開始ビットの辺を見つけた周期**（`uart_rx` の `out_time`）を `sample_time` で運び、`event_collector` は源の時刻（`aux_src_time`）を使う。TIMING の新しい情報源 `aux`（点 `start_bit_edge`、delay 2、位相の幅 1、含まない項目 `input_routing`・`target_internal` = 補助マイコンが ADC で変換してから送り始めるまでは分からない）。申告の無い古い回路は `_LEGACY` の `aux`（点 `hand_off`、範囲なし）。cocotb `aux_link_tb.the_sample_time_is_the_start_bit_of_the_first_byte`・`event_collector_tb` | `aux_link.vhd`・`event_collector.vhd`・`fprobe_core.vhd`、`fprobe_gw/timing.py`、`fprobe/timing.py`・`probe_events.py` |
| B3・N6 観測の口（表 17） | 記録型の印の瞬間の申告 SYNC_DELAY は一様の仮定の平均（7・13 半周期）で、ホストが 1 点として引く。標本型は申告なし。表の「+1」。鎖の始め・組の切り替えの写しの値に変化と同じ時刻 | 確かめた事実（cocotb）: 印の瞬間は Update-DR を出る TCK の立ち上がりから [3, 4] 周期（1 クロック版 [6, 7]。端を含む: 立ち上がりが clk の辺と重なると端の値）。観測の回路のレジスタ 10・11 に範囲（SYNC_DELAY_LO・HI、半周期）を足した（7 は中点のまま。0 = 申告なし）。`FpgaObs.open` に `sync_delay_lo/hi`・`sample_delay_lo/hi`（古い回路はレジスタ 7 の ±0.5 周期、標本型は None）。標本型の取り込みは Capture-DR を出る立ち上がりから (2, 3] 対象の clk（回路に申告の場所が無いので None = 分からない。TCK ≤ clk/8 は回路で守らない事実を注に書いた）。**表 17 の「+1」は誤りで差は 0**（イベントの時刻 = 端子の TCK の辺。`test_obs_e2e.test_event_times_are_the_tck_edges_on_the_pin`、3 構成 × 40 件）。写しの値に `ObsSample.copy`（鎖の始め・溢れ・組の記録・欠けの後の最初の値・標本型の標本: その時刻にその値だっただけで、変わった時刻ではない）。試験 `obs_core_tb`・`obs_soft_core_tb`・`obs_snap_core_tb`・`obs_e2e_tb` | `target_ip/fprobe_obs/rtl/obs_core.vhd`・`obs_snap_core.vhd`・README、`obs_jtag_engine.vhd`（注）、`fpga_obs.py`・`obs_decoder.py` |
| 既知の失敗 `test_overflow_lost_counts[False-3-2]` | 回路の誤り: `obs_core` の S_VALUE が溢れの経路の値の変化を見ず、溢れの記録を書いた次の周期の変化が lost に入らなかった（失った数が 1 少なく見えた。対象の周期 19928 の溢れ lost 12 の次の周期に 393 → 394） | S_VALUE で溢れの経路の変化を数え、溢れの記録をもう 1 つ書いてから値を書く（入らなければ S_RESYNC）。ホストの `obs_decoder` は続けて来た溢れの lost を足す。cocotb `obs_core_tb.a_change_right_after_the_overflow_record_is_counted`（直す前の回路で落ちる）、e2e の全部の組 | `obs_core.vhd`、`obs_decoder.py` |
| §4.6 clockmap（判断 4） | 最小二乗の直線（`timesync.fit_parent`・`fpga_obs.fit_line`・`fit_sync`）と、組の間の直線の内挿（`interpolator`） | 新設 `fprobe/clockmap.py`: 組 = 2 つの時計の値の窓（両端を含む）、仮定は「区間の中で比が一定、比 > 0」だけ。合う直線 (a, b) の集まりを有理数で切り取り（Sutherland–Hodgman）、任意の値の範囲は頂点の最小・最大。どの直線も通らなければ `feasible = False`・`first_conflict`（与えた順で初めて合わなくなった組）。傾きが押さえられない（x の窓が全部重なる・組 1 つ）なら `feasible = None` で範囲を返さない。比を使う側が知っていれば `slope = (lo, hi)` で明示する。最小二乗は残さない | `clockmap.py`（新設）、試験 `test_clockmap.py` |
| B4 ボードの間の合成（`timesync`・`merge`） | 不確かさ = 手で書いた量子化 1 刻み（`QUANTIZATION_TICKS`）+ 子の不確かさ。位相の幅・残差は入らない。段数はホストの生成した定数（PYNQ の端子の段を知らない） | 組の窓 = 子 [子の時刻 − RX, 子の時刻 − RX + 位相の幅]、親 = 親の時刻 + TX（申告の sync_tx。端子の段を含む）+ 使う側が明示した配線の遅れ。`fit_parent` は全部の組を通る直線の範囲（`ParentTime.slope`・`feasible`）、`to_parent` は `t_lo`・`t_hi`（`t` は中点）、`interpolator` は `neighbour_converter`（両側の 2 組の窓を通る直線の範囲。仮定は隣り合う組の間で比が一定）。`merge` の行の時刻は範囲の中点を丸めた値、`uncertainty` はそこから範囲の端までの遠い方 + 子の不確かさ、body に `sync_window_ticks`。`QUANTIZATION_TICKS` は廃止（量子化は申告の位相の幅として窓に入る）。段数は記録の meta の `timing`（記録を始めたときの caps.timing を新しく残す）から取り、無い古い記録は生成した表の値（結果の `link.source` = "generated"） | `timesync.py`・`merge.py`・`probe_recording.py` |
| 観測の口の時刻（B3 の続き） | `obs_decoder` は鎖の印の組（直近 256）の最小二乗、印 1 つは申告の比か前の鎖の傾き（1 点） | 印の組の窓 = 対象の鎖の時刻 [印の瞬間 − sync_delay_hi, 印の瞬間 − sync_delay_lo]、fprobe の時刻 = イベント 9 の時刻（端子の辺そのもの）。値の時刻は鎖の時刻 [t − sample_delay_hi, t − sample_delay_lo] に**隣り合う 2 つの印**の窓を通る直線の範囲（`SyncSegment.window`。印の外は端の 2 つを延ばす）。`ObsSample.time_lo`・`time_hi`、`time_fprobe` は中点。印 1 つの鎖は使う側の比（申告の CLOCK_HZ の比を 1 点、または `borrow_ratio` で前の鎖の比の範囲）。窓を通る直線が無ければ `time_basis = "assumption_broken"` で時刻 None。`fit_line`・`fit_sync` は廃止 | `obs_decoder.py`・`fpga_obs.py`・`probe_fpga_obs.py`・`collector.py` |
| N8 の残り（記録の外の `fpga_obs_read`） | `obs_decoder` が 2^47 未満の戻りを後ろへの時刻にする | 展開した時刻が前の時刻より前（一周の 2^47 以上の戻りではない）= タイムベースが 0 に戻った事実（§14.1 と同じ判定）: 前の時代の印・鎖を捨てて数え直し、`ObsDecoder.restarts` に数える。前後の時代を 1 本の直線に混ぜない | `obs_decoder.py` |

### 13.2 API の出力の変化

- `events_read`（raw = false）: SWO の Manchester のバイトの時刻は最初のビットの中央の変化（新しい回路。前は最後のビットの中央 + 3 =
  約 7 ビット + 1 刻み後）。`line.point` は `first_bit_center`（古い回路は `last_bit_center` のまま）。補助マイコンの計測値（kind "aux"）に
  `line`（組の先頭のバイトの開始ビット。新しい回路の時刻はそこで、前より約 30 ビット + 4 刻み前。`unknown` に `target_internal`）。
  UART 送信の `line` は PYNQ で 1 刻み後（端子の段）。
- `fpga_obs_read`: 値に `time_lo_us`・`time_hi_us`（範囲。`time_us` はその中点）と `copy`。`time_basis` に "assumption_broken"。`time_us`
  の値は最小二乗の直線の値から範囲の中点に変わる（窓の幅の半分以内の差）。記録の stream fpga の行に不確かさ（範囲を覆う ±）。
- `merge`（`python -m fprobe merge`）: `uncertainty` が `{assumption, phase_span_ticks, window_max_ticks, rounding}` に（前は
  `quantization_ticks`・`residual_max_ticks`）。結果に `unmapped`（範囲の出ない出来事の数）・`link`。子の出来事の body に
  `sync_window_ticks`、uncertainty は範囲を覆う ±（前は 1 + 子の不確かさ）。
- `fprobe.timesync`: `ParentTime` は `map`・`pairs`・`link`・`slope`（範囲）・`feasible`（前の `local0`・`parent0`・`ratio`・`residual_max` は
  無い）。`to_parent` は `t_lo`・`t_hi`・`t`（中点）・`inside`・`assumption`・`unknown`・`link`（前の `quantization_ticks`・`residual_max_ticks` は
  無い）。`interpolator` は `neighbour_converter`（dict を返す）。`fprobe.fpga_obs.fit_line`・`fit_sync` は無い。`SyncSegment` は窓の組を持つ形。
- ケイパビリティ: TLV TIMING の情報源 25〜27（`aux`・`xtrig_out`・`xtrig_out_pins`）、点 9・10（`first_bit_center`・`output_edge`）。
  `swo_manchester` の点・段数が変わり、PYNQ の `uart_tx`（−1）・`sync_tx`（−2）の段数が変わる。古いホストは知らない情報源を読み飛ばす。
- 記録の meta に `timing`（caps.timing）。
- イベントの時刻の意味（種類 5・15 の Manchester、種類 6）が変わる。種類の番号は同じで、TLV TIMING の申告で区別する（§6 の判断 3）。

### 13.3 古い申告の読み方

S3 より前の回路（S1〜S2 の TIMING を申告する回路を含む）の `uart_tx`・`sync_tx` は、PYNQ-Z2 の端子の IOB の FF 1 段を段数に入れていない。
その 1 段は含まない項目 `output_delay`（FF → 端子）に入っていたと読む: 申告の点は「端子の辺はこれ以後」として正しいまま（ちょうどの
位置ではない）。ホストは申告どおりに使う（段数の数字をホストに持たない）。同期の出力は、S3 の前の PYNQ には端子の FF が無く、その申告は
ちょうどだった。

### 13.4 実機で確かめること（未確認）

- ビルド: 全部の PYNQ の構成と Primer の `fprobe`・`fprobe_la`。タイミング（100 MHz）、EX1998 が無いこと。増えた FF: `swo_manchester_rx` の
  48 ビット × 2、`aux_link` の 48 ビット × 2、`event_collector` の補助の時刻の保持（前と同じ数）、PYNQ の `sync_pad`（IOB に詰まること:
  Vivado の報告の IOB の欄）。観測の回路（`target_ip`）を対象の FPGA に入れ直す（レジスタ 10・11・溢れの直し）。
- 見る値: (1) Nucleo の SWO（Manchester）をロジアナにもつなぎ、バイトの `line`（最初のビットの中央）とロジアナで見た中央の変化が
  入出力の遅れ + 1 刻み以内。(2) UART の TX → RX の折り返しで、送信の `line` と受信の `line` の差が線の遅れ（数 ns）+ 1 刻み以内
  （PYNQ は S3 で送信が 1 刻み後ろ）。(3) クロストリガの出力の端子をロジアナの別のピンに戻し、`xtrig_out_window` の `t_on`・`t_off` と
  ロジアナの辺が入出力の遅れ + 1 刻み以内（unit 1・2 の脈の長さも）。(4) Primer ↔ PYNQ の同期で `merge` の `sync_window_ticks` に、
  両方のロジアナで見た同じ辺の親の時刻が入ること（配線の遅れは使う側が渡す）。(5) 補助マイコンの組の時刻と、Pico の TX を
  ロジアナで見た開始ビットの辺。(6) 観測の口の値の `time_lo_us`〜`time_hi_us` に、対象で同じ信号を出したピンの辺が入ること。

### 13.5 §2.1 の表で直すもの（文書の担当へ）

- 12: 時刻 = バイトの最初のビットの中央の変化を見つけた周期（R4）。差 = 同期化 2 段、位相の幅 1。送り手の速さに依らない（ばらつかない）。
- 14: 端子 = 時刻 + ボードの出力の段（PYNQ 1。R9）。
- 15: 時刻 = 組の先頭のバイトの開始ビットの辺を見つけた周期（B2）。端子の辺は時刻 − 2 以上 + 1 未満。補助マイコンの中は不明（target_internal）。
- 16: 源のパルス S → 出力の FF S + 2 → 端子 + ボードの段（PYNQ 計 3）、端子の条件 → 4 + 段（PYNQ 5）。脈の長さは (L − 1)·16^unit + 1 〜
  L·16^unit 周期で、源の時刻から 1 つに決まる（`xtrig_out_window`）。
- 17: 「+1」を 0 に（イベントの時刻 = 端子の TCK の辺）。記録型の印は Update-DR を出る立ち上がりから [3, 4] 周期（1 クロック版 [6, 7]。
  申告はレジスタ 10・11）、標本型の取り込みは Capture-DR を出る立ち上がりから (2, 3] 対象の clk（申告なし）。
- 18: PYNQ は同期の出力にも IOB の FF（TX の段 = 1 + 1）。換算は窓を通る直線の範囲（`clockmap`）。

### 13.6 判断したこと

- **clockmap の範囲**（§6 の判断 4・6）: S3 で実装し、`timesync`・`merge`・`obs_decoder` を置き換えた（最小二乗を残さない）。
  `linux/clock.py`（対象の Linux の時刻 → PC の時計）は形を変えていない: 隣り合う 2 組の中点の内挿 ± 往復の半分の大きい方は、隣り合う
  組の間で比が一定なら本当の時刻を含む確かな範囲で、仮定も同じ（docstring に書いた）。clockmap に入れる組の作り方 1〜3（§4.6: DAP で
  対象のカウンタを読む・対象が印を出す・SWO / ITM の時刻）は、組を作る側の道具（どの番地を読むか等は使う側・プラグインが渡す）で、
  S3 では作っていない（`clockmap.pair` に窓を渡せば使える）。秒への換算は判断 6 のとおり公称の周波数のまま「ppm は不明」。
- **全部の組を 1 本の直線に通すか、隣り合う 2 組か**: `merge`・観測の口は隣り合う 2 組（仮定が弱い: 温度で比がゆっくり変わっても
  破れない。窓の幅は間の点で窓そのものの幅、延ばすと広がる）。`fit_parent` は全部の組（仮定が破れたら `feasible = False` で返す）を
  残し、どちらを使うかは使う側が選ぶ。区間の切れ目は使う側が明示する（`gap_ticks`。暗黙の既定にしない）。
- **S2 の残りの、窓の後端を縮めること**（§12.3）:
  - SWD のか所の境界の窓の後端を直前の TAR の書きの ACK まで縮めるには、その転送の点を種類 17 で出す必要があり、停止の見張りの
    普通の使い方（1 語のか所）では標本ごとに件が 1 つ増える（サンプラの件の帯域が約半分になる）。縮まる幅は 1 転送（SWD 4 MHz で
    約 12 µs）で、窓の後端はいまも確かな上限。**実装しない**（正しさは変わらず、件の増えが大きい）。要るなら使う側が明示して選ぶ
    設定（CSR の 1 ビット）として後で足す。
  - JTAG の窓の後端を値を返したスキャンの Capture-DR まで縮めるのは、回路の件を増やさずホストの式（設定・鎖の位置・区間の境目の 1 周期）
    で出せる。**S3 では実装しない**: 式を cocotb（`jtag_phy_tb` で Capture-DR を出る立ち上がりを測る）で確かめてから入れる（確かめて
    いない式で上限を縮めると、誤った上限になりうる）。段 H3 か S4 で行う。

### 13.7 残したこと

- JTAG の窓の後端を Capture-DR まで縮める式（§13.6）。SWD の境界の後端（§13.6。実装しない判断）。
- 標本型の観測の回路に遅れの申告の場所を作ること（レジスタの形の変更。標本型は id だけのエンジンの読みで、レジスタが無い）。
- clockmap の組を作る道具（DAP で対象のカウンタを読む・GPIO の印・ITM の時刻）と、`probe_testpoint`・`testpoint/live` の対象の時計
  （`core_clock_hz` の直線。§9.2）を clockmap に載せること。
- Primer のトップの出力（`BOARD_OUTPUT_TICKS` は 0）は、Gowin の IOB の登録を確かめていない（トップに FF が無いことだけを読んだ）。
- `ObsDecoder.restarts` は API（`fpga_obs_read`）には出していない（記録の中は §14.1 の切れ目で記録が止まる）。

## 14. 段 H2 の実装（2026-11-02）

段 H の続き（ホストだけ）。§9.6 の全部、§9.5 の N7・N8、§9.1 の A7、§12.3（S2）の残りのホスト側。1 項目ずつ今の振る舞いを示す
失敗する試験から直した。§14.1 は N8 の設計（実装の前に決めた）。

### 14.1 タイムベースの時代の切れ目（N8）の設計

**事実**: タイムベース（`timebase.vhd` の 64 ビットの `time_count`）は、ビットストリームの書き込み・電源・クロックのロック外れ
（`rst_g(0)`）で 0 に戻る。64 ビットは実用上一周しない（1 GHz で 584 年）ので、**同じ機器のタイムベースの読みが前の読みより
小さくなったら、それは 0 に戻った事実**（閾値・推定を使わない判定）。0 に戻った後の時刻は前の時刻と比べられない（間に何秒
あったかは分からない）。この「0 から数え続けた区間」を**時代**と呼ぶ。

**原則**: 1 つの記録は 1 つの時代だけを持つ。時代をまたぐ時刻を 1 本の軸に並べない（ずらして続けるのは、分からない間隔を
決め打ちすることになる）。

| 所 | 今（N8） | 決めたこと |
|---|---|---|
| `Recorder` | 単調性を見ない | `observe_time(ticks)`: 記録に入れる時刻の基準として読んだタイムベースを渡す。前の読みより小さければ、記録に事実の行（stream `timebase`・kind `restart`、t = 前の時代の最後の読み、本体 `{ticks_after, host_s}`）を書き、meta の `ended` に `{"reason": "timebase_restart", "last_ticks", "ticks_after"}` を残し、以後の行を受けない（捨てた数を meta の `rejected_rows`。黙って混ぜない）。記録を止めるのは使う側（recording_stop）。`recording_status` に `ended` |
| `sample_clock`・`Collector` | 時計の点・イベントの基準の読みを確かめない | 読んだタイムベースを `observe_time` に通す。切れ目を見たら集めるのをやめる（`Collector.ended`） |
| `ClockMap` | 前後の時代を 1 本の直線に当てはめる | `add` で前の点より小さいタイムベースが来たら、前の点を捨てて新しい時代から当てはめ直し、回数を `restarts` に数える（記録の外の使い方: トレースの取り込み・利用者の出来事） |
| `Collector._read_new` | written < 読んだ数 の間は黙って待ち、超えたら前の件を数えずに失う（`EventCursor` は restarts に数える） | written < 読んだ数 は、イベント記録が始め直された事実: 記録に annotation `events_restarted`（written・読んだ数）、`restarts` に数え、0 から読む（`EventCursor` と同じ）。始め直しの後に読んだ数を超えて書かれていた場合は、数からは分からない（タイムベースの切れ目なら上の検出が先に効く） |
| `_switch_design` | 記録を止めない | ビットストリームを書く前に、記録中なら記録を止める（`ended` に `design_switch`・before・after。応答の `recording_stopped` に止めた記録の名前） |
| `recover_link` | 記録を止めない | USB が切れた間の出来事は失われ、FPGA が電源から立ち上がり直したかも開き直す前には分からない: 記録中なら止める（`ended` に `link_lost` と前の時代の最後の読み `last_ticks`）。応答の `recording_stopped` に止めた記録の名前と、開き直した後のタイムベースが前の読みより小さかったか `timebase_restarted`（true / false、前の読みが無ければ null）。記録は閉じた後なので meta には入れない |
| `obs_decoder`（2^47 未満の戻りを後ろへの時刻にする） | — | 残した: 記録の中では切れ目で記録が止まるので、切れ目をまたいだ観測の口の組は記録に入らない。記録の外の `fpga_obs_read` の切れ目は、S3（obs_decoder の時刻）で一緒に直す（並行の担当と同じファイル） |

### 14.2 直したこと

| 項目 | 前 | 後 | コミット |
|---|---|---|---|
| トレースの時刻の区切り（共通） | 形式の中で時刻のつながりが切れても 1 本の ts | `TraceEvent.segment`・`Trace.breaks`・`Trace.timed`。`TargetClock` は区切りごとの対応点、対応点の無い区切りは写さない（`place` が None） | b0d93cd9 |
| SystemView | 最初の SYSTIME_CYCLES で絶対の値へ跳ぶ・2 回目の START の後に止めていた時間がずれる・読み飛ばしで差を失う | 差の和（跳ばない）。TRACE_START・INIT・resync・SYSTIME の食い違いで区切り | b0d93cd9 |
| Event Recorder | 1 周未満の仮定で読みをまたいで unwrap | 対象の ts_overflow を上位に入れた 64 ビット。init_count が変われば区切り | b0d93cd9 |
| Tracealyzer | 減るタイマも unwrap・欠けの印は前の時刻だけ | 時刻にしない（timed false・raw_ts）・after_ts・before_ts | b0d93cd9 |
| CTF | ts_hz 1e9 の決め打ち・時刻の無い CTF に ts_hz | TSDL の clock の freq か使う側の ts_hz（食い違えば誤り）。時刻の無い CTF には付けない | b0d93cd9 |
| unwrap | 戻りを黙って 1 周に・unwrap32 は数えない | ts_wrap_min_back と assumptions。unwrap32 を無くした | b0d93cd9 |
| rtos の時間軸 | 端の区間を不確かさ 0 で閉じる・stats に切った区間・範囲外のオフセット・分解能 0 | start_cut・end_cut、統計は両端が切り替えの区間（cut_runs・max_cut_run）、範囲外は誤り、分解能 None。画面は null を「—」 | 11a7bac9 |
| rtos の解析 | memory_series の点が窓の始め・逆転の読みを同時とみなす | 点はその値のサンプルの時刻・pair_read_span・opened_reads | b2623e98 |
| 各 OS | 0 と 32 ビットの決め打ち | overdue_ticks・tick_mask・None・remaining_excludes | 22c15330 |
| flow・model | 平均の暗黙の t1・番号を見ない出口 | mean_t1・番号で対に | 09166ab7 |
| A7 | t_fire・t_prev が 48 ビット | 64 ビット | 3ba35cca |
| S2 の残り | 最後の語の t_hi が null・correlate が窓を使わない・欠けの後の窓・watch・窓（probe_window）が線上の点の回路を知らない | 最後の語を持ち越す（flush_carry）・t_hi_after_loss・correlate の time_point と変化の範囲・watch の time_point と time_lo_us / time_hi_us・記録の窓が線上の点の回路の読めた語を捨てていた（本体の有無で判定）のを直した | cd7aaa23 |
| S2 の残り（画面・文書） | 画面が assumptions・bound_without_assumptions_ns を出さない・linux-halt.md の式 | API の値のまま描く・線上の点の回路では D = 0 | 385473d5 |
| N8 | §14.1 の表 | §14.1 の表のとおり | 61daecbe |

### 14.3 API の出力の変化

- トレース（trace_status・trace_stop・trace_snapshot・trace_import）: clock は `anchors`（区切りごとの対応点の並び。前の
  `anchor_ts`・`anchor_t`・`uncertainty` は無くした）、basis は `anchor_per_segment_declared_rate`。`unplaced_events`・`segments`・
  `breaks`・`timed`、桁あふれを戻したときは `ts_wrap_min_back`・`assumptions`。出来事に `segment`（0 以外のとき）。trace_events の
  `t` は対応点の無い区切りでは無い。SystemView の `ts` は差の和（前は最初の SYSTIME_CYCLES の後は絶対の値）。Tracealyzer の減るタイマの
  `ts` は順の番号で `raw_ts`。CTF は TSDL の clock か ts_hz が無ければ ts_hz なし（時間軸・記録に写さない）。trace_start に `ts_hz`。
- rtos_timeline_get・recording_cpu_load: stats の `max_run`・`min_run`・`mean_run` は両端が切り替えの区間だけ（無ければ null）、
  `cut_runs`・`max_cut_run`。区間の `uncertainty` は null がありうる。`resolution` は null がありうる。記録の run の本体に
  `start_cut`・`end_cut`。rtos のメモリの推移の点の時刻、逆転の `pair_read_span`・`opened_reads`、待ちの `overdue_ticks`、
  Zephyr のタイマの `remaining_excludes`、queue_usage の `mean_t1`。
- watch_values・watch_series: `time_point`（read_request / value_returned）、線上の点の回路では `time_lo_us`・`time_hi_us`。
- recording_correlate の reads: `time_point`・`change_window_s`・`change_window_unknown`。
- 記録: 線上の点の回路の最後の語の `t_hi` が埋まる（記録の終わりの語だけ null）、`t_hi_after_loss`。stream `timebase`・kind
  `restart`、annotation `events_restarted`、meta の `ended`・`rejected_rows`。recording_status の `ended`。
- probe_design_switch の `recording_stopped`、recover_link の応答の `recording_stopped`（name・timebase_restarted）。
- 画面: 停止の報告に `assumptions`・`bound_without_assumptions_ns`（API の値のまま）。CPU の表の最長・最短・平均が null なら「—」。

### 14.4 残したこと

- `obs_decoder` の 2^47 未満の戻り（N8）: 記録の中は時代の切れ目で記録が止まる。記録の外の `fpga_obs_read` は S3（obs_decoder の
  時刻）で一緒に直す（並行の担当と同じファイル）。
- probe_window の窓の値の列（rtos の方法 A・メモリの推移・逆転）に、線上の点の回路の語の窓（t_lo・t_hi）をまだ通していない
  （時刻の意味は窓の notes に書いた。解析の式に範囲を入れるのは各解析の出力の形の変更）。RTT の起こし（probe_observe）は値だけを
  使い、時刻を使わないので変えていない。
- SystemView の TRACE_START の時刻（流れの最初の START も INIT との間隔が分からないので別の区切り。記録・時間軸に写さない
  unplaced_events が 1 になる）は事実どおり。START の直後の SYSTIME_CYCLES の扱い（区切りをまたいで絶対の値で続けるには、間が
  32 ビット 1 周未満の仮定が要る）は入れていない。
- Tracealyzer の複数コアの流れの出来事の順の入れ替わり（コアごとの unwrap）、減るタイマの時刻の作り直し（タイマの周期と OS の
  ティックから）。
- 行を書いた後の設定の変化（持ち越した語の後に種類 16 が来たかたまり）: 持ち越した語の窓の下限は、そのかたまりの前の設定で
  出す（いまはかたまりの最後の設定）。
