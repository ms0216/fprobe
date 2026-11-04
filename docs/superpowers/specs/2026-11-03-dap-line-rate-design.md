# DAP の線を sys/2 まで・転送の間の空きを 0 に（SWD・JTAG）

2026-11-03。状態: 実装（cocotb まで。実機・ビルドは主担当）。利用者の指示: 「超絶アグレッシブに、妥協なく、完全無欠の設計に。
性能は理論値（docs/reports/2026-11-02-performance-abcd.md の D 列）へ。横着しない」。点検の P3・P4・P5
（2026-11-03 の回路の点検）と stop-precision §3.8（TCK を sys/2 に）の実装。

## 1. なぜ・何を

- 線の速さ: SWCLK・TCK は sys/4（PYNQ 25 MHz）が上限だった（half_period ≥ 2）。理論の上限は sys/2 = 50 MHz（1 相 = 1 周期）。
- 転送の間の空き: TransferBlock は「転送 → 応答を 4 バイト書く → 次の転送」と直列で、書きは毎回 get(4) してから転送した。
  さらに PHY の応答 → dap_link → ユニット → 次の命令 → dap_link → PHY の往復が入り、転送の間に SWCLK の High 相が
  延びていた。cocotb で数えた値（起点 086f4e9f の回路、sys 100 MHz、TransferBlock 24 語、25 MHz・10 MHz とも同じ）:
  書きは転送の間ごとに 16〜19 周期、読みは 16 周期、ほかに命令の頭で 1 回 8 周期。25 MHz の書き 24 語は線の上で 5046 周期
  （今の作りは 4654 周期、約 8% 短い）。1 転送 184 周期（25 MHz）の約 9%、50 MHz なら 92 周期の約 18% にあたる。
- 守ること: 誤りの確認（ACK・パリティ・CTRL/STAT・FAULT）の原則。失敗した転送の後の転送を線に出さない（参照実装 DAP.c と
  同じ応答・処理数）。速さのために正しさを捨てない。

## 2. 線の速さ（PHY）

- swd_phy・jtag_phy とも 1 ビット = Low 相 half_period + High 相 half_period 周期、half_period ≥ 1。1 で sys/2。
  `dap_cmd_config` の SWJ_Clock は half_period = ceil(sys/2 / (1.01 × 要求))、1〜65535（下限を 2 → 1）。
- 取り込みの遅れ: 往復（端子の出力の段 + 配線 + ターゲットの出力の遅れ）が 1 ビットを超えても、遅延線（段 k = k 周期前に
  予約したビット）で順に取る。SWD の遅れは 0〜3 → 0〜7（CSR `debug.sample_delay.swd` 3 ビット）。
  - SWD: サンプル = SWCLK を立ち上げる辺 + d 周期の辺の直前のピン。正しく読める d は
    d·T ∈ (往復 − 2·hp·T, 往復)（往復 = 端子の段 × T + ターゲットの遅れ）。cocotb `sample_delay_window_matches_the_round_trip`
    が hp 1・2、端子の段 0・1、遅れ 3〜33 ns のすべてで導出と一致することを確かめる。
  - JTAG: サンプル = 次の立ち下がりの辺 − 1 + d 周期の辺の直前のピン。d·T ∈ (往復 − 2·hp·T + T, 往復 + T)。
    cocotb `tdo_sample_delay_window_at_sys_half`（hp 1・2、遅れ 3〜43 ns）。
  - 次を決める値（SWD の ACK・命令の結果のパリティ）は、取り込むのと同じ辺でその値を使う（half_period 1・遅れ 0 で
    High 相を延ばさない）。遅れが High 相に間に合わない分だけ High 相が延びる（SWD: ACK で max(0, d + 1 − hp)、読みの終わり
    （命令の終わり、または自分が線を駆動する相 = idle_cycles の idle 相の前）で max(0, d + 1 − hp − 2·hp·turnaround)）。
    読みのデータ相の終わりでは待たない。（2026-11-03 のレビューで、idle 相の前に待たず、遅れた取り込みが自分の駆動とぶつかった
    線を読む不具合を直した。docs/reports/2026-11-03-cloud-A.md）
  - JTAG の結果（ACK・値）は取り込む側が作り、線を作る側は止めない（RDBUFF を続けるかの ACK はスキャンの本体の最初の 3 ビット
    で、本体の後に十分なビットがある）。応答（rsp_valid）は「その命令のビットを出し終え、最後の取り込みも済んだ」とき。
- JTAG の区間の境目・命令の境目も 2·hp（前のビットの立ち上がりの辺で次のビットを決めておき、High 相の最後の辺で出す。
  長さ 0 の BEFORE / AFTER は飛ばす）。前は区間の境目ごとに High 相が 1 周期延びていた。

## 3. JTAG の TMS（half_period 1）

- 実機で「TCK の立ち下がりで TMS と TDI が同時に 0 → 1 になると化けた」（jtag_phy の先頭のコメント）ので、TMS は Low 相の中ほどで
  変える規則。half_period 1 では Low 相が 1 周期なので、jtag_phy は `tms`（立ち下がりの辺で変わる）と `tms_lead`（その周期の
  前半の TMS = 前の値）を出す。PYNQ-Z2 のトップは JA の出力を ODDR（`xilinx_oddr`、前半 = pin_o_lead・後半 = pin_o）で出し、
  TMS は TCK の立ち下がりの 5 ns 後に変わる（TCK の立ち上がりの 5 ns 前）。前と同じく端子の段は 1 周期（出力の時刻の申告は
  変わらない）。cocotb `tms_lead_keeps_tms_off_the_fall_at_sys_half`。
- ODDR の無いボード（Primer・Nano 9K）は `swdio_o` だけを出し、half_period 1 の TMS は TDI と同じ辺で変わる（50 MHz で上の化けが
  出るかは未確認。SWD には関係しない）。

## 4. 転送の間の空き 0（先出し）

- PHY は命令の最後のビットの High 相の最後の周期に次の命令を受け、同じ辺で次の最初のビットを始める（空き 0）。
- `cmd_cond`（条件つきの命令）: PHY は直前の命令が OK の転送（SWD: ACK OK でパリティ異常なし、JTAG: ACK OK/FAULT。IDCODE・
  ABORT・転送でない命令も OK）のときだけ受ける。dap_link は前の転送の応答の前に次の転送を条件つきで出しておく。前が失敗すれば
  次は線に出ない。
- dap_link は 2 段の待ち行列。応答は順に、ユニットが取る（ack_take）まで保つ。失敗の応答を作ったら後ろの命令を捨て、その失敗を
  ユニットが取るまで（取る周期を含む）に来た命令も捨てる。WAIT の再試行は古い方を条件なしで出し直す（次は条件つきなので
  受けられていない）。
- dap_cmd_transfer の TransferBlock は応答を待たずに次を出す（応答を取っていない転送は 2 つまで）。読みは応答を取った周期に
  次を出してから put32、書きは次の値を get(4) してから出す。失敗の応答を取ったら止める。応答の並び・数・失敗の扱いは参照実装と
  同じ。
- dap_cmd_transfer の DAP_Transfer も先出し（2026-11-04）: 要求を読んで、その要求の転送（AP の読みの連鎖・ポストの回収の
  RDBUFF・読み・書き）を決めて出す前の列に置き、応答を待たずに出す（2 つまで）。どの転送を出すかは要求のビットとポストの有無
  だけで決まり、前の転送の値によらない。応答は順に取り、値を put32、要求の最後の転送の成功で処理数を足す。値一致の読み
  （繰り返す）と一致マスクの書き（以後の要求に効く）は、応答を取り終えてから 1 つずつ。線に出る転送の並びと応答は参照実装と
  同じ（cocotb `dap_transfer_reference_tb`: ランダムな並び 160 組を DAP.c の写しと突き合わせる）。転送の間の空きは
  9〜21 周期 → 0（`transfer_has_no_gap_between_transfers`）。
- 受理は前の命令の応答より最大 1 つ先になる。受理と応答を対にする側（dap_monitor・swd_injector・dap_sampler）は 2 段で数える。
  割り込み書き込み・サンプラが線を借りるのは DAP の命令の境目（dap_busy = 0）だけなので、命令の途中には入らない（前と同じ）。
- 性能の約束（cocotb で固定）: `transfer_block_has_no_gap_between_transfers`（dap_processor_tb）— TransferBlock の読み・書き・
  DP の読みで、線の上の High 相はすべて half_period（hp 1・2・5）。SWD の PHY 単体は `back_to_back_transfers_have_no_gap`、
  JTAG は `back_to_back_transfers_have_no_gap_at_any_half_period`（区間・命令の境目を含めて TCK の数が導出と一致）。

## 5. 理論の速さ（TransferBlock、線の上。USB/Ethernet・ホストは含まない）

| | 1 語の線の長さ | 25 MHz | 50 MHz |
|---|---|---|---|
| SWD（ターン 1、idle 0） | 46 ビット | 1.84 µs → 2.17 MB/s | 0.92 µs → 4.35 MB/s |
| JTAG（JTAG-DP だけのチェーン、IR を省く） | DR 40 TCK（3 + 35 + 2） | 1.6 µs → 2.5 MB/s | 0.8 µs → 5.0 MB/s |

チェーンに他の装置があれば DR に dr_before + dr_after ビットが足される。前の作りは SWD 25 MHz で 1 語 184 + 16〜19 周期
（約 2.0 µs → 約 2.0 MB/s）だった。

## 6. 互換

- ケイパビリティ DEBUG の bit4 `DEBUG_HALF_PERIOD_1`: half_period の下限 1・SWD の遅れ 0〜7。ホストは
  `Capabilities.min_half_period`（無い古いデバイスは 2）・`swd_sample_delay_max`（3）を見る（`fprobe.sampler.swclk_hz`・
  `round_estimate`・`swd_hz_for_rate`・`probe_lowlevel.jtag_half_period` は下限を引数に取る）。
- イベント 16（PHY の設定）の SWD の遅れの bit2 は [105]（payload の [57]。下位 2 ビットは [76:75] のまま）。
- `fprobe_core` の新しい口 `swdio_o_lead`（ODDR を使わないボードはつながない）。

## 7. 残したこと

- （2026-11-04 に解消）パケットの間: 次のパケットを前の応答を送り終えるまで読まず、送出も 1 周期おきだったので、続けて
  送った TransferBlock（120 語）の間に約 1000 周期の空きがあった（50 MHz で線の時間の約 9%）。dap_packet の応答バッファを
  2 面にし（送っている間に次を処理）、送出を 1 周期 1 バイトにした: 空きは 42 周期。cocotb
  `queued_packets_follow_each_other_on_the_wire`。
- （2026-11-04 に実装）DAP_Transfer（複数の要求）の先出し。§4。
- SWD の ACK の後の 1 ビット（ターン / RDATA の 1 ビット目）を ACK を待たずに出すと、ACK の取り込みの遅れを
  2·hp 周期まで隠せる（どの ACK でも線の上は同じビットなので正しさは変わらない）。未実装。
- dap_sampler の命令の間の空き（P5）: 2 段で数えるようにしただけで、サンプラ自身の命令はまだ 1 つずつ（並行の担当の作業と
  ぶつからないよう最小の変更に留めた）。cocotb で数えた値（SWD 25 MHz、1 転送 184 周期、前後で同じ）: か所の中の転送の間
  4 周期、巡回・か所の境目 9 周期。1 か所 1 語は 1 巡回 192.8 周期（1 転送 + 9）、1 か所 4 語は 944.8 周期（5 転送 + 25）、
  2 か所 1 語は 761.8 周期（4 転送 + 26）。詰めるには サンプラも次の転送を応答の前に条件つきで出す（dap_link と同じ形）。
- 実機: PYNQ → Pi 2B の JTAG 50 MHz（DSCR の読み 2 万回の失敗数）、`jtag.sample_delay_scan` で遅れの範囲、ビルドでタイミング。

## 8. 実機の結果（2026-11-03、C:\fw85 = fprobe_pynq_deep、PYNQ-Z2 → Raspberry Pi 2B の JTAG、チェーンは JTAG-DP 1 つ）

- 取り込みの遅れの走査（`jtag.sample_delay_scan`、IDCODE 0x4BA00477 を各 2000 回）:
  - 25 MHz（half_period 2）: 遅れ 2・3・4 が 2000/2000、ほかは 0。
  - 50 MHz（half_period 1、TMS は ODDR）: 遅れ 4 だけが 2000/2000、3・5 を含むほかは 0（合う窓は 1 段）。
- 4 コアの DBGDSCR の読み 2 万回（`ArmACores.halted()`）: 25 MHz・遅れ 3、50 MHz・遅れ 4 とも誤り 0。どちらも 1 秒に約 1730 回で
  同じ（この読みは往復の回数で決まり、線の速さでは変わらない）。
- 25 MHz の Pi の一式（`tests/linux_halt/hw/test_hw.py`）は 29 件すべて通った。止まりのずれ 11〜12 µs。FPGA の見張りでの
  「最後のコアが事象の後に走った時間」は 44 回のうち 43 回が 0〜125 µs、1 回が 358.8 ms（その回の方式は記録がなく理由は未確認。
  試験が回ごとに方式と根拠を出すようにした、e9c227fe）。
- 既定は 25 MHz のまま（50 MHz は合う遅れが 1 段で、Pi のこの読みでは速さも変わらないため）。50 MHz を使うには、使う側が
  走査の結果から遅れを選んで `jtag.sample_delay_set` で明示する。
