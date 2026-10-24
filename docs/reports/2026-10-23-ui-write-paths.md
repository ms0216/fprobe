# 見えている値と書き換え（2026-10-23）

画面（host/ui/src/components/panels）に表示している値の種類ごとに、書く API があるかと、その値を表示している窓で人が
書けるかを調べた。API パネル（ApiPanel.svelte）からは書く API をすべて呼べるが、ここでは「値を表示している窓そのもの」
で書けるかを見る（原則: AI ができることは、人がそれを見ている場所でできる）。

## その窓で書けるもの

| 窓 | 値 | API | 操作と条件 |
|---|---|---|---|
| Registers | コアレジスタ | target.registers.write | ダブルクリック。止まっているときだけ（API も停止中だけ） |
| Memory | 語・文字の列 | target.memory.write | ダブルクリック。接続中なら走行中も |
| Watch | 変数（停止中） | debug.write_expression | ダブルクリック。止まっていて古くないとき、数・真偽・列挙・ポインタだけ |
| Locals | ローカル変数 | debug.write_expression | ダブルクリック。止まっているとき |
| Peripherals | レジスタ・フィールド | target.peripheral.write | ダブルクリック・チェックボックス・選択。read-only でないもの |
| Dashboard | ボタン・スライダ・切り替え | 走行中 watch.write、停止中 debug.write_expression | 押す・離す |
| Terminal | 送信 | rtt.write・uart.write | 入力欄 |
| Flash | オプションバイト | target.option_bytes.write | 欄 → Write（確認あり） |
| Breakpoints・FPGA Obs の信号の定義・Linux の core | 設定 | 各 API | 欄・ボタン |

## 書く API はあるのに、表示している窓で書けないもの

1. Watch の走行中の値（ライブウォッチ）: watch.write がある。窓は止まっているときだけ書ける（WatchPanel.svelte:17,38）。
2. Data Sampling の値・配列の要素: watch.write / debug.write_expression がある。表示だけ（DataSamplingPanel.svelte:398-405）。
3. System の MCU の番地の語: target.memory.write / watch.write がある。表示だけ（SystemPanel.svelte:474-482）。
4. Dashboard のゲージ・数値・棒・グラフ・ランプ: 書けるのはボタン・スライダ・切り替えの部品だけ。
5. Fault の CFSR・HFSR・MMFAR・BFAR とスタックに積まれたレジスタ: target.memory.write / target.peripheral.write。表示だけ。
6. Threads のタスクの情報・RTOS Objects の数や状態: 汎用の debug.write_expression / target.memory.write。表示だけ。
7. Disassembly / Source の命令: target.memory.write（RAM）/ target.flash。表示だけ。
8. FPGA Obs の CONTROL / SELECT: fpga_obs.register_write は画面のどこからも呼ばれない（select・start・stop の操作はある）。
9. どの窓にも表示が無いもの: target.dp.write・target.ap.write・probe.csr.write・probe.pins.write・gpio.set・xtrig.output_set。

## 書き換えられる性質なのに、書く API が無いもの

1. 対象の FPGA の中の信号・レジスタ（FPGA 観測は読むだけの経路。fpga_obs.register_write は観測の回路自身の設定だけ）。
2. RTOS のタスク・オブジェクトを RTOS として書く API（汎用のメモリ・式の書き込みはできる）。別のタスクの文脈の
   ローカル変数を書く API（rtos.read の書き込み版）。
3. Linux のターゲットのメモリ（リング・memap で読む値）。
（電圧は測った値で、書く対象ではない。ターゲットの電源を制御する API も無い = ハードが要る U39。）

## 気づいた点

- Watch / Locals の書き込み（VarRow.svelte:83 → debugger.ts:388-392）は、AI の作業中に出す確認 allowed(app) を
  通っていない。Registers・Memory・Peripherals・Dashboard は通している。
- Memory は走行中も書けるが、Auto Refresh を入れていないと表が古いまま。
