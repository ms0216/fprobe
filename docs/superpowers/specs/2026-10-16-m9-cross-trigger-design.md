# M9: クロストリガ（第 1 段）設計

- 日付: 2026-10-16
- 親: `2026-10-15-fprobe-architecture-design.md` §4.2（DebugActionInjector / TransactionMonitor）、§4.5（CrossTrigger）

## 1. ゴールと完了条件

| 完了条件 | 測り方 |
|---|---|
| 「ロジアナのトリガでターゲットを停止」がハードウェアだけで（ホストを介さず）動く | HIL: 走っている Nucleo を、ロジアナのトリガ（UART 送信の立ち下がり）で止め、トリガ→停止の遅れを測る |
| 「デバッガがこの番地に書いたら波形トリガ」が動く | HIL: pyOCD が RAM の番地に書いた瞬間でロジアナがトリガし、その SWD 転送が波形に入る |
| 割り込みはホスト（pyOCD）から見えない（キャッシュした SELECT・CSW・TAR を戻す） | シミュレーション（ブロック転送の途中で割り込み → 続きが正しい番地に入る）+ HIL（割り込み後も pyOCD が使える） |

## 2. 構成

```
dap_processor ──命令──▶ swd_injector ──▶ swd_phy
      ▲   │                ▲ request
      │   └──覗く──▶ dap_monitor ─ select_shadow ─┘
      │                    └ match ─┐
la_capture ─ triggered ─┐          │   csr_xtrig.command.fire
                        ▼          ▼          ▼
               源 = {LA トリガ, 番地一致, ホスト}  ── route.to_la ──▶ la_capture.ext_trigger
                                                 └─ route.to_inject ─▶ swd_injector.request
```

- **swd_injector**（`rtl/debug/`）: dap_processor と swd_phy の間のアービタ。要求が来たら DAP コマンドの境界
  （dap_processor が次のコマンド待ちで、PHY に命令が残っていない）で PHY を借り、SELECT ← AP（bank 0）、
  CSW・TAR を読んで保存、CSW ← 32 ビット・増加なし、TAR ← 番地、DRW ← 値、CSW・TAR・SELECT を戻す。
  WAIT は 255 回まで再試行。FAULT 等は中止して SELECT だけ戻し、結果を CSR に出す。
  - 境界で割り込む理由: CMSIS-DAP のコマンドの中では AP 読み出しのポスティングが続いている。コマンドの終わりでは
    必ず RDBUFF で閉じられている（参照実装 DAP.c と同じ）。
- **dap_monitor**（`rtl/debug/`）: dap_processor の転送（応答 OK のもの）を覗き、SELECT の控え・CSW（サイズ・増加）・
  TAR を追って DRW アクセスごとの番地を求める。番地（マスク付き）・向き・書き込み値（マスク付き）の一致でパルス。
  割り込みの転送は覗かない（ホストの転送だけを見る）。
- **クロストリガ**（fprobe_core 内、1 周期）: 源 3 本 × 動作 2 本の AND-OR。各源・動作の時刻（番地一致、割り込みの
  受理）をタイムベースで記録する。
- CSR `xtrig`（docs/registers.md）。CSR 生成器に `PULSE` 種別（1 を書いた次の周期に 1 周期だけ 1）を追加。
- ホスト: `fprobe.xtrig.CrossTrigger`、MCP の道具 `xtrig_*`。

## 3. 判断

| 判断 | 理由 | 誤っていた場合のコスト |
|---|---|---|
| 停止は DHCSR への書き込み（汎用の「1 語書き込み」として実装） | SWD しか無い（EDBGRQ・CTI のピンは無い）。汎用にしておけば GPIO を叩く等にも使える | 低 |
| 割り込みは DAP コマンドの境界まで待つ | コマンドの途中で割り込むとポスティング中の読み値を壊す。待ちは最大でコマンド 1 つ分（数十 µs〜数 ms） | 中: 長いブロック転送中は停止が遅れる。必要ならパケット内の転送境界での割り込みを検討 |
| 番地一致はホストの転送（DRW）だけを見る | ターゲット CPU 自身のアクセスはプローブから見えない。それは DWT + SWO（データトレース）で M6 第 2 段 | 低 |
| AP は区別しない、BD0〜3 は追わない | Cortex-M の MEM-AP は 1 つで、pyOCD 等は DRW だけを使う | 低 |
| 多段シーケンストリガ・EventFabric（イベントのストリーム）は後段 | 単段 + 時刻記録で「なぜ壊れたか」の主な用途を満たす | 低 |

## 5. 改訂: 停止までの遅れの短縮（2026-10-16）

成功基準「ロジアナトリガ成立から SWD 停止要求開始まで ≤ 1 µs」に対し、実測はトリガから停止の書き込みの受理まで約 90 µs
（4 MHz）。内訳は、DAP コマンドの境界を待つ時間と、停止の書き込み（DRW）までに CSW・TAR を読んで保存する転送
（SELECT、CSW 読み + RDBUFF、TAR 読み + RDBUFF、CSW、TAR の約 7 転送）。

- **定義の明確化**: 「停止要求開始」= 割り込みの最初の SWD 転送が線上で始まる時刻。SWD は転送単位でしか割り込めないので、
  ホストの転送中ならその転送（25 MHz で約 2 µs）の終わりまで待つ。HIL はトリガから最初の SWCLK の変化までを測る。
- **CSW・TAR を読まない**: dap_monitor はホストが書いた SELECT・TAR（自動増加を含む）を追っている。CSW も 32 ビット全体を
  控えれば、戻す値はホストが書いた値そのもの（ホストのキャッシュと一致する）になり、読み出しの転送が要らない。
  手順: SELECT ← ap → CSW ← 32 ビット・増加なし → TAR ← 番地 → DRW ← 値 → CSW・TAR・SELECT を控えから戻す。
  停止の書き込みまで 4 転送（約 7 → 4）。25 MHz で約 8 µs、4 MHz で約 45 µs。
- 控えはホストの書き込みからしか作れないので、ホストがまだ CSW / TAR を書いていない（接続直後）ときは、戻す代わりに
  リセット値（CSW = ホストが書くまで不明）を使わず、従来どおり読んで保存する手順に切り替える（控えの有効ビット）。
- **結果**: トリガ → 最初の SWD 転送の開始 80 ns（成功基準 ≤ 1 µs を満たす）。トリガ → 停止の書き込みの受理
  90 → 50 µs（SWCLK 4 MHz。25 MHz なら約 8 µs）。シミュレーションで、控えが無いときは従来の 10 転送、
  あるときは読み出しなしの 7 転送になり、TAR が戻ることを確かめる（`injection_uses_the_host_state_shadow`）。
