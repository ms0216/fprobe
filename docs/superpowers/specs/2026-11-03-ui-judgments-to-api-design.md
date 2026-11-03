# 画面の中の判断を API に移す（2026-11-03）

元: 人と AI の見え方の点検（audit-human-ai-parity.md）の §E。原則: 画面（UI）は考えない層（CLAUDE.md）。画面の中の
計算・判断はホストに移し、API（= MCP）で返す。経験の閾値・推測は本体に入れず、事実だけを返す。

行番号は fix/linux-halt-debug-hw の ecfc7a3a で確かめた。

## E1. 故障か・Fault の窓を自動で開くか（lib/fault.ts:34, 37-39, 49-63, 71-72）

- 事実: 画面が例外名の表（HardFault・MemManage・BusFault・UsageFault・SecureFault）を持ち、`target.fault` の
  `exception.name` と突き合わせて「故障の例外の中か」を決めていた。見出し（headline）と、止まったときに窓を開くかに使う。
  AI には同じ値が無い（`faulted` は故障のビットの事実で、別の値）。
- 直し方: `target.fault` の `exception` に `fault`（IPSR が 3〜7 = 故障の例外。ARMv7-M ARM B1.5.2 の定義）を足す。
  画面は `faulted`・`exception.fault` を読むだけにし、表を消す。

## E2. 止める前に確かめるか・致命的の区分（lib/safety.ts:147-149, 175-177）

- 事実: `needsWarning` = `risk.matched && target.debug_mode !== "monitor"`、`isFatal` = `kind !== "tick_stopped"` を
  画面が決めていた。
- 直し方:
  - `debug.halt_risk` に `debug_mode`（いまの方式 halt / monitor）と `confirm_before_halt`（matched かつ debug_mode が
    halt = 止める操作がコアを止める）を足す。条件は使う側が `debug.halt_risk.set` で渡したもの（既定は空）で、
    本体の経験則は入らない。画面は `confirm_before_halt` を読む。方式が変わったら（target.debug_mode.set・状態の
    debug_mode の変化）読み直す。
  - `target.status` の `fatal` に `alert`（lockup = アーキテクチャの定義、pc_in = 使う側が「ここに居たら知らせて」と
    渡した関数の中 → true。tick_stopped = 事実だけで正常かは決めない → false）を足す。画面は `alert` で色を選ぶ。

## E3. 書き込む API の選択（lib/valuewrite.ts:29-33, 110-115）

- 事実: 式は、画面がターゲットの状態で `watch.write`（走行中）か `debug.write_expression`（停止中）かを選んでいた。
  命令のバイトは、画面が `target.flash.regions` を読んで命令の範囲との重なりを計算し、memory か flash かを決めていた。
- 直し方:
  - `debug.write_expression` が状態で道を選ぶ: 止まっていれば選んだフレームで（今まで通り）、走行中（RUNNING・
    SLEEPING）なら止めずに（`watch.write` と同じ道。frame は 0 だけ）。ほかの状態は誤り。結果に `via`
    （halted / running）と、走行中は `atomic`。画面は状態を見ずに `debug.write_expression` を呼ぶ。
  - 逆アセンブル（`debug.disasm.*`）の命令に `in_flash`（命令のバイトが Flash の領域に掛かるか。未接続・
    メモリの地図の無いターゲットは null）を足す。画面は重なりを計算しない。

## E4. TCK の換算と設定の定数（lib/fpgaobs.ts:43-48, 85, 139-145）

- 事実: 画面が TCK = sys / (2 × 半周期) で換算していた。観測の回路も jtag_phy を使うので、実際は
  sys / (2 × 半周期 + extra)（extra = `Capabilities.jtag_bit_extra_cycles`。古い回路は 1）で、古い回路ではずれる。
  PRESETS（TCK 10 MHz・64 語・印 20 / 2.5 MHz・1 語・印 0）は経験の値で、AI に見えない。
- 直し方:
  - `fpga_obs.open` に `tck_hz`（求める TCK。超えない最も速い半周期をホストが extra を入れて選ぶ。half_period と
    同時には渡せない）を足し、`fpga_obs.status`（と system.status の fpga）に `tck_hz`（実際の TCK。開いていない・
    システムのクロックの申告が無ければ null）を返す。`config` は半周期で覚え、tck_hz を入れない（そのまま
    `fpga_obs.open` に渡せば同じ設定で開き直せる。SystemPanel の開き直しもこの形）。画面は MHz の欄を tck_hz で送り、
    実際の値は status の tck_hz から描く。
  - PRESETS とその釦を消す（値は使う人が入れる。目安は docs/guide/fpga-analysis.md の「制限」）。

## E5. 条件の文字の構文解析（lib/system.ts:52-77）

- 事実: 画面が recording.correlate と同じ書き方を自前の正規表現で解析し（ホストの parse_condition と引用符の扱いが
  違う）、条件の欄の検査と、候補が「FPGA の信号だけの条件か」（FPGA の条件の表に足す釦）を決めていた。
- 直し方:
  - `recording.correlate` の候補ごとに `terms`（{variable, kind = mcu / fpga / pin, op, value?}）と `fpga_only`
    （項の変数がすべて FPGA の信号）を返す。
  - `recording.condition.parse`（読みの API。機器・記録に触らない）: 条件の文字 → 項。読めなければ誤り（理由）。
    画面は欄の検査にこれを使う。

## E6. 型の文字の解釈（lib/vartree.ts:104-107, 137-146, 184-188）

- 事実: 画面が C の型の文字を正規表現で読み、ポインタの先を読めるか（void・関数へのポインタ）、配列の要素の型、
  ポインタの先の型、構造体の型の名前（`debug.type_info` でメンバーの型を引く）を決めていた。
- 直し方: 値（fprobe.dwarf.values）に事実を足す: ポインタ `deref`（番地が 0 でなく、指す先が void・関数でない）、
  配列 `element_type`、構造体 `member_types`（メンバー名 → 型の名前）。画面は型の文字を解釈しない。
- 残す: lib/debugger.ts の markedLines（location を ":" で分ける）・ファイル名の最後の部分での突き合わせ
  （debugger.ts・SourcePanel・profile.ts）は、debugger.ts を別の作業が直している最中なので、この作業では触らない。

## 試験

- ホスト: 各事実の単体の試験（fault・liveness・halt_risk・write_expression の道・disasm の in_flash・fpga_obs の
  tck_hz・correlate の terms / fpga_only・condition.parse・values の deref / element_type / member_types）。
- 画面: lib の試験を、API の欄を読むだけの形に書き換える。
