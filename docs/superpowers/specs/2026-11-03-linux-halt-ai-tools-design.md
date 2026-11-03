# AI が JTAG の全コア停止で止めて調べる道具（linux.halt.debug.*）の設計（2026-11-03）

指示書: `docs/superpowers/specs/2026-11-03-cloud-sessions-plan.md` §I。ブランチ `cloud/linux-halt-ai-2026-11-03`。

## 1. 目的と、今までの穴

Linux の全コア停止デバッグで「止めて調べる」入口は、人が gdb をつなぐ `linux.halt.gdb.serve`（RSP）だけだった。AI は RSP を
話せず、`linux.gdb.batch`（ssh で対象の上の gdb を attach する。JTAG・全コア停止を使わない）に逃げ、2 スレッドの足し損ね
（データ競合）を**同じ時点**で見られなかった（2026-11-03 の実機の評価、Haiku）。

この設計は、gdb を通さずに、API（したがって MCP と画面）から同じ全コア停止の機能を 1 回の呼び出しずつ使えるようにする。

## 2. 作り直さないもの（使う部品）

| 部品 | 使い方 |
|---|---|
| `linux_halt/engine.py` `Engine` | 全コアの停止・再開・1 命令のステップ・レジスタ・スレッドの同定・当たりの分類・保留。**待つ上限**（§5.3）と、止めたまま返す／走らせたまま返すの区別を足す |
| `linux_halt/bp.py` `BreakpointTable` | SW/HW のブレークポイントと journal（そのまま） |
| `linux_halt/process.py` `Process`・`usermem.py` | スレッド一覧・auxv・ユーザ空間のメモリ（そのまま） |
| `linux_halt/service.py` `HaltSession` | 開いたバックエンドと錠・後始末（停止デバッグを切る・DAP のつなぎ直し）。gdb のサーバと**同じ扱いの持ち主**として、新しい `HaltDebugger` を持つ |
| `fprobe/symbols.py` `Symbols` | 場所（関数名・`ファイル:行`・番地）→ 番地、番地 → 関数・ファイル・行 |
| `fprobe/dwarf/` `DebugInfo`・`Inspector`・`Unwinder` | 型・値・変数の場所・C の式・CFI の巻き戻し。**読み込み位置のずれ（load bias）**を足す（§4） |
| `linux_halt/sim_cores.py` `SimCores` | 試験（実機なし） |

新しく書くのは `linux_halt/debugger.py`（`HaltDebugger`。上の部品を組む）と、`linux_halt/image.py`（プロセスの写像から
実行ファイルの読み込み位置と共有ライブラリを事実として求める）、API の層（`probe_linux_halt.py` のメソッドと仕様）だけ。

## 3. 道具（API）

名前は既存の `linux.halt.*` にそろえ、`linux.halt.debug.*` に置く（gdb のサーバ `linux.halt.gdb.*` と並ぶ、もう 1 つの
「持ち主」）。どれも `linux.halt.open` の後。人の画面も同じ API を使う（画面は今回は作らない: §9）。

| メソッド | 引数（**既定なし**は必須） | すること |
|---|---|---|
| `linux.halt.debug.attach` | pid, elf, skew_limit_ns, thread_id, kernel_isa, max_step_retries, bp_unmapped, max_halt_s; fetch_from_target（任意） | 全コアを止め、前のデバッガの BKPT を journal で戻し、プロセスを読み、ELF の読み込み位置を求め、**止めたまま**返す |
| `linux.halt.debug.status` | — | 持ち主の事実（止まっているか・ブレークポイント・最後の停止・自動の再開・数え）。コアは触らない |
| `linux.halt.debug.break.insert` | location, kind | 関数名・`ファイル:行`・番地にブレークポイントを置く（止まっているときだけ） |
| `linux.halt.debug.break.remove` | id | 外す（止まっているときだけ） |
| `linux.halt.debug.run` | wait_s | 再開して、当たるまで最大 wait_s 秒待つ。当たれば全コアを止めて止めの事実を返す。時間切れは「当たらなかった」を返し、全コアは走らせたまま |
| `linux.halt.debug.halt` | — | 走っている全コアを止める（要求の停止） |
| `linux.halt.debug.threads` | variables, expressions, max_elements | 止まっている間に、全スレッドの tid・どのコアにいたか・pc・関数・ファイルと行・主なレジスタ（と、各スレッドのフレーム 0 の引数・局所変数・式の値）を**1 回で**読む |
| `linux.halt.debug.read` | expressions, tid, frame, max_elements | 指定のスレッドの指定のフレームで、式（大域・静的・局所・引数）を読む |
| `linux.halt.debug.memory` | address, length | プロセスの仮想メモリを読む |
| `linux.halt.debug.step` | tid, mode（into / over）, max_instructions | 1 行進める |
| `linux.halt.debug.close` | — | ブレークポイントを全部外し、全コアを走らせ、停止デバッグを切り、journal を片付ける |

`linux.halt.close`（とデーモンの終了・機器の開き直し）は、開いている持ち主を先に閉じる（gdb のサーバと同じ）。

### 3.1 排他

- 持ち主は 1 つ: gdb のサーバが動いている間は `debug.attach` を、`debug` が開いている間は `gdb.serve` を断る（Engine の
  all-stop の状態と BKPT を 2 者で乱さない）。
- `debug` が開いている間、`linux.halt.halt_all`・`resume_all`・`processes`・`threads`・`recover`・`rescue` も断る（理由:
  Engine の知らない停止・再開・BKPT の書き戻しが Engine の世界の見えを壊す）。`status` は読むだけなので通す。
- 断るときは、誰が使っているかと、閉じる方法（`linux.halt.debug.close`）をメッセージに書く（`Busy`、E_DEVICE）。

### 3.2 止まっているとき・走っているときの約束

- `attach`・当たった `run`・`halt`・`step` の後は全コアが止まっている。`break.*`・`threads`・`read`・`memory`・`step` は
  止まっているときだけ（走っていれば「走っている。halt か run で止めてから」と断る。勝手に止めない: 止めると対象の時間が
  変わり、それは使う側が選ぶこと）。
- `run` は止まっていれば再開してから待ち、走っていればそのまま待つ（前の `run` の時間切れの続き）。wait_s = 0 は「再開して、
  いまある当たりだけ見て返る」（再開）。

## 4. ELF と読み込み位置（PIE・共有ライブラリ）

- `elf`: PC の上の ELF のパス（DWARF の付いたビルドの出力）。`fetch_from_target`（任意。`linux.connect` のターゲットの名前）を
  渡すと、先にそのターゲットの `/proc/<pid>/exe` を ssh で `elf` に写す（`linux.core.fetch` と同じ道。bytes・sha256 を返す）。
  対象の上のファイルは strip されていることが多い（その時は DWARF が無いことが応答の事実に出る）。
- **実行ファイルの読み込み位置**は、カーネルが exec のときに保存した補助ベクタ（`mm->saved_auxv`、JTAG で読む）から求める:
  `load_bias = AT_PHDR − (ELF の PT_PHDR の p_vaddr)`。PT_PHDR が無ければ `AT_ENTRY − e_entry`。応答に、使った式（basis）・
  AT_PHDR・AT_ENTRY・e_entry・ELF の型（ET_DYN / ET_EXEC）を返す。
- **ELF が動いているものと同じか**は、推定せずに事実で返す: AT_PHDR の番地にあるプロセスのメモリ（プログラムヘッダの表）と、
  ELF のプログラムヘッダの表のバイト列が一致するか（`phdr_match`）と、`AT_ENTRY == e_entry + load_bias` か（`entry_match`）。
  一致しなくても断らない（使う側が判断する）。
- **共有ライブラリ**は、実行ファイルの動的セクション（`PT_DYNAMIC` + load_bias）の `DT_DEBUG` → `r_debug.r_map` → `link_map`
  の鎖（動的リンカが書く）をたどり、各ライブラリの名前と `l_addr`（読み込み位置のずれ）を返す。鎖は循環を検出して止める。
  DT_DEBUG が無い・0 なら、その理由を返す（静的リンク・動的リンカがまだ書いていない）。ライブラリの DWARF は今回は読まない（§9）。
- **load bias を DWARF の部品に通す**: `Symbols`（ELF のリンク時の番地）と `DebugInfo` はリンク時の番地のまま。実行時の番地と
  の間は次の 3 か所で足し引きする（`fprobe/dwarf` に `load_bias` の引数を足す。既定 0 = 今までと同じ）:
  1. 位置式の `DW_OP_addr`・`DW_OP_addrx`（大域・静的変数の番地）に + load_bias（`EvalContext.load_bias`、`FrameView.load_bias`）。
  2. 関数・行・スコープ・位置リストを引く番地は、実行時の pc − load_bias（`Inspector(load_bias=)`）。
  3. CFI の表を引く番地も実行時の pc − load_bias（`Unwinder(load_bias=)`）。CFA と復元したレジスタは実行時の値のまま。
  ブレークポイントの番地は `Symbols.resolve(location) + load_bias`。応答には実行時の番地とリンク時の番地の両方を返す。

## 5. 止める・待つ・当たる

### 5.1 ブレークポイント

- `location`: 関数名（`Symbols.after_prologue` で前置きの後。行表の prologue_end が無ければ関数の先頭のまま。どちらだったか
  を `prologue_end` で返す）・`ファイル:行`（その行の最小の番地）・番地（整数か "0x…"。実行時の番地として扱う）。
- `kind`: `sw` か `hw`（既定なし。`bp_unmapped` は attach で決める既存の約束）。
- 返す事実: id・location・address（実行時）・link_address・kind（実際に置いた方式。bp_unmapped=hw で替わったらそう返す）・
  width（ARM 4 / Thumb 2。ELF のシンボルの bit 0 と、`$t`・`$a` の写像の印は使わず、`Symbols` の関数の番地の bit 0 の事実）・
  function・file・line。置けなかったら理由（未マップ・HW の空きなし・場所が無い）を誤りとして返す（`EngineError`）。

### 5.2 止めの事実

`run`（当たったとき）・`halt`・`step`・`attach` は、Engine の `StopResult` をそのまま返す: 方式（sequential / simultaneous）・
止まったコア・コア間のずれの上限（ns か null）・根拠の文・仮定・仮定なしの上限・`skew_limit_ns` との比較（within / exceeded /
unknown）。根拠の文は、バックエンドが `fpga_request_times`・FPGA の停止の見張り・CTI の有無で書き分けたものを変えずに返す。
ブレークポイント等でコアが自分で止まった停止も、FPGA の時刻で上限を出せれば出す（FPGA の見張りの発火の記録、または
`fpga_request_times` のイベント記録。出せなければ理由を basis に書く。docs/linux-halt.md「コアが自分で止まった停止」。
2026-11-03 の実機の評価の指摘で足した）。
加えて、止まった理由（kind: swbreak / hwbreak / watch / trap / int / exit / signal / timeout）・当たったスレッド・番地・
関数・ファイルと行・ブレークポイントの id。

### 5.3 待つ上限（Engine に足すもの）

`Engine.run(timeout_s, clock)`: 今の `cont()` と同じ道（報告済みの当たりの踏み越え・キャッシュの同期・全コアの再開・
他のプロセスの当たりの踏み越え・保留の当たり）に、**待つ上限**を足す。上限を過ぎたら、全コアを走らせたまま
`Stop("timeout")` を返し、Engine は「走っている」を覚える。次の `run` は再開せずに待ちを続け、`halt()` は要求の停止で
止めて世界を読み直す。上限の判定は `wait_event` を 1 回呼んだ後（上限 0 でも、いまある当たりは拾う）。
gdb の道（`cont()`）は変えない。

### 5.4 止めたまま放置しない

- `attach` の `max_halt_s`（必須。既定なし）: 道具が全コアを止めたまま、API の呼び出しが無いまま max_halt_s 秒たったら、
  見張りのスレッドが全コアを走らせる（Engine の再開の道。報告済みの当たりの踏み越えを含む）。走らせた事実（時刻・止めて
  いた秒数）を `status` と次の応答の `auto_resumed` に返す。呼び出しのたびに数え直す。
- 閉じ忘れ・切断: デーモンの終了・機器の開き直し・`linux.halt.close` は持ち主を閉じる（BKPT・HW の bp/wp を外し、全コアを
  走らせ、停止デバッグを切る）。後始末が DAP の誤りで失敗したら、gdb のサーバと同じく DP をつなぎ直してもう 1 度だけ
  後始末する。デーモンごと落ちたら、BKPT は journal に残り、次の `attach`（と `linux.halt.recover`）が戻す。

## 6. スレッドと変数を 1 回で読む

`threads` は、止まっている間に（コアを動かさずに）プロセスの全スレッドについて返す:

- tid・comm・`on_core`（そのスレッドを走らせていたコアの番号。どのコアにもいなければ null）・`mode`（コアの上なら user /
  kernel。コアにいなければ null）・`registers_from`（`core` = コアのレジスタ / `pt_regs` = カーネルに入ったときに保存した
  ユーザのレジスタ）・`registers`（r0〜r12・sp・lr・pc・cpsr）・pc・function・file・line（pc − load_bias を ELF で引いた事実。
  ELF の外なら null）。
- `variables=true`: フレーム 0 の引数・局所変数（`Inspector.frame_variables`。読めないものは値の代わりに理由）。
- `expressions`: 各スレッドのフレーム 0 で評価した式の値（大域変数は全スレッドで同じ値が出る。それが同時点の読みの確かめ
  になる）。式ごとの誤りは、その式の欄に理由を返し、他の式・スレッドは続ける。
- 応答に、読んだときの停止（`last_stop`）を付ける（どの停止の時点の値か）。

`read` は 1 つのスレッドの任意のフレーム（巻き戻しは CFI）で同じことをする。`memory` はプロセスの仮想メモリを読む
（未マップは理由を返す）。

## 7. 1 行進める

`step(tid, mode, max_instructions)`: Engine の 1 命令のステップ（他のコアは止めたまま）を繰り返す。

- 始めの位置の (関数, ファイル, 行) と違う行の、文の始まり（行表の is_stmt の番地ちょうど）に着いたら止める。
- 関数の先頭に入った（呼び出し）とき: over は、そのときの lr（戻り先）まで Engine の run-to で走らせ、続ける。into は、
  入った関数に行の情報があれば、前置きの後（prologue_end）まで run-to して止める。行の情報が無ければ（ELF の外・ライブラリ）
  戻り先まで走らせて続ける。
- max_instructions（必須）で止まらなければ、そこで止めて `reached_new_line=false` と位置を返す（誤りにしない）。
- 途中でブレークポイント・終了・シグナルに当たったら、その停止を返す。

## 8. 誤りと説明

- 誤りは種類（`Busy` / `EngineError` / 型の誤り）とメッセージと、どの引数かを返す（API の E_DEVICE / E_INVALID_PARAMS）。
- 既存の linux.halt.* も含め、`kernel_isa`・`thread_id`・`bp_unmapped`・`fpga_request_times`・`skew_limit_ns`・
  `max_step_retries` の意味と許す値を、仕様（`fprobe_api.json` の params の description）に書く。
- `linux.gdb.batch` の説明に「対象の上の gdb を ssh で使う。JTAG・全コア停止は使わない（gdb が attach したスレッドだけが
  止まり、他のプロセスは走り続ける）。全コアを止めて同じ時点で調べるなら linux.halt.debug.*」と書く。MCP の案内
  （`mcp_instructions.py`）にも同じ事実を書く。

## 9. 今回しないこと（残す）

- 画面（パネル）。API は画面からも同じに使える。画面の型（`npm run check`）は生成した型で通す。
- 共有ライブラリの DWARF（名前と読み込み位置は返す）。
- ウォッチポイント（Engine と WatchTable はあるので、次に足せる）。
- 実機（PYNQ → Pi 2B）の試験: `tests/linux_halt/hw` に同じ流れの試験を足し、手元で回す。

## 10. 試験

- `SimCores` と合成のカーネルのメモリの上に、2 スレッドの競合のアプリの模型を置く: `tests/data/linux_halt/race.c`
  （同じ大域変数 counter を読み・足し・書く add_one を 2 スレッドが呼ぶ）の ARM の PIE（clang、-O0 -g。`build.sh`）を、
  読み込み位置 0x00400000 で模擬のページ表に写す。
- 端から端: attach → `race.c:11` に SW のブレークポイント → run → スレッド 101 が当たる（全コアの停止の事実）→ threads で
  両スレッドの行と counter・before・after を 1 回で読む → run(0) で再開 → close（BKPT が消え、全コアが走り、停止デバッグが切れる）。
  同じ流れを API（本物のデーモン + Probe）でも通し、応答の照合（`tests/api_result_check.py`）を通す。
- 個別: 時間切れは走らせたまま、`max_halt_s` の自動の再開、排他（gdb のサーバと）、止まっていないときの断り、load bias の
  計算と一致の事実、共有ライブラリの鎖（循環を含む）、DWARF の load bias（大域・局所・CFI）、1 行のステップ（into / over）。
