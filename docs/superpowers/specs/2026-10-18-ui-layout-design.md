# 画面の構成と文言を競合ツールの慣習に合わせる（デバッガの型の画面）

- 日付: 2026-10-18
- 背景（利用者）: 「画面構成や文言が競合ツールと違いすぎて違和感がある」「デバッガのタブがすぐ表示されない」
  （ELF を読むまで何も出ない）。
- 入力: `docs/reports/2026-10-18-competitors-commercial.md`・`2026-10-18-competitors-vendor-oss.md`・
  `2026-10-17-debug-ui-ux-survey.md`（第 3 部）、画面の要件 G01〜G11（`2026-10-18-gap-analysis.md` §3）、
  `2026-10-18-ui-framework-evaluation.md` §4（ドッキング）、`2026-10-18-timeline-tiles-design.md`（時間軸の部品）。

## 1. 競合に共通する型（Ozone・Keil µVision・IAR C-SPY・VS Code + Cortex-Debug）

| 場所 | 共通するもの | fprobe での形 |
|---|---|---|
| 上 | メニュー（File・View・Debug/Run・Target/Project・Help） | メニューバー: File / View / Debug / Target / Help |
| 上 | デバッグのツールバー（Download・Reset・Continue/Halt・Step Over/Into/Out・Run to Cursor・Breakpoints・接続） | 同じ順で、アイコン + ツールチップ（名前とショートカット） |
| 中央 | ソース（行の左の余白にブレークポイント、いまの行の強調、逆アセンブルの混在） | Source（エディタの位置。ファイルのタブ、余白の ● と ▶、Mixed の切り替え） |
| 周り | Registers・Watch・Locals・Call Stack・Breakpoints・Memory・Peripherals（SVD）・Disassembly・Terminal（RTT/SWO/UART）・RTOS（Threads/Tasks） | ドッキングのパネル（dockview）。並べ替え・分割・タブ・閉じる、配置は覚える。View メニューで開く |
| 下 | ステータスバー（接続・プローブ・ターゲットの状態・SWD の速度・CPU） | 同じ。右に fprobe 独自の「AI の作業 / 一時停止」 |
| 下のパネル | Ozone の Timeline・Data Sampling、IAR の Timeline | Timeline（WebGL2 の部品）。Terminal・Activity と同じ下の段にタブで |

- 今のタブ（作業・デバッガ・波形）と独自の言い回し（「1 命令」「RAM へ読み込む」「見張る」等）はやめる。
- 標準に無い fprobe の強み（AI と人の操作の記録・調停、共通タイムライン）は、標準の場所（下のパネル・ステータスバー）に
  置き、名前は英語の一般的な語（Activity・Timeline）にする。

## 2. 操作とショートカット（VS Code の慣習。F6 は Ozone）

| コマンド | ショートカット | API |
|---|---|---|
| Continue | F5 | `target.resume` |
| Halt | F6 | `target.halt` |
| Disconnect | Shift+F5 | `target.disconnect` |
| Step Over / Step Into / Step Out | F10 / F11 / Shift+F11 | `debug.step`（mode over / into / out） |
| Run to Cursor | Ctrl+F10 | `debug.step`（mode until、location = 選んだ行） |
| Toggle Breakpoint | F9 | `debug.breakpoint.set / clear`（選んだ行） |
| Reset | Ctrl+Shift+F5 | `target.reset`（halt） |
| Download | F8（Keil µVision） | `target.flash`（読み込んだ ELF） |
| Open ELF | Ctrl+O | `debug.elf.load` / `debug.elf.upload` |

コマンドは 1 か所（`lib/commands.ts`）に定義し、メニュー・ツールバー・キーボードが同じものを使う（使えない状態では
灰色、理由をツールチップに）。ツールチップは「名前（ショートカット）— 説明」。入力欄にフォーカスがあるときは
F キー以外を奪わない。

## 3. 文言の表（英語・日本語）

- すべての画面の文字列を `lib/i18n.ts` の表に置く（コンポーネントに直書きしない）。既定は英語、Settings で切り替え、
  `localStorage`（`fprobe.lang`）に覚える。
- 英語は Ozone・Keil・VS Code の実際の語: Continue, Halt, Step Over, Step Into, Step Out, Run to Cursor, Reset,
  Download, Toggle Breakpoint, Call Stack, Watch, Locals, Registers, Peripherals, Memory, Disassembly, Breakpoints,
  Terminal, Threads, Timeline。
- 日本語は VS Code / Visual Studio の日本語版の標準の訳: 続行、中断、ステップ オーバー、ステップ イン、
  ステップ アウト、カーソル行の前まで実行、リセット、ダウンロード、ブレークポイントの切り替え、呼び出し履歴、
  ウォッチ、ローカル、レジスタ、ペリフェラル、メモリ、逆アセンブル、ブレークポイント、ターミナル、スレッド、
  タイムライン。
- 説明（ツールチップの後半・空の状態の説明）はどちらの言語でも日本語でよい。

## 4. 空の画面を出さない

- 何も読んでいないとき、Source に「Open ELF…（ファイルの選択・パス・最近使ったもの）」と「Load Demo」。
  ファイルの選択はブラウザからデーモンへ送る（新設 `debug.elf.upload`。遠隔の画面でも使える）。Load Demo は
  同梱の `host/firmware/sram_demo/sram_demo.elf`（新設 `debug.elf.load_demo`）。
- 各パネルは「何が要るか」と「その操作のボタン」を出す（未接続 → Connect、走行中 → Halt、SVD が無い → Load SVD、
  RTT を始めていない → Start RTT）。
- 偽の機器のデーモン（`host/tests/fake_daemon.py --demo`。`--demo-recording` も同じ）は起動時からデモの ELF を開き、
  接続して止まった状態にし、レジスタ・メモリ・呼び出し履歴・ローカル・RTOS のタスク・周辺レジスタ・RTT の出力・
  捕捉と記録（Timeline）を返す。機器なしで各パネルの働きが見える。

## 5. 構成（コード）

```
App.svelte        MenuBar / DebugToolbar / Dock（dockview）/ StatusBar / ダイアログ（Open ELF・Connect・Settings）
lib/i18n.ts       文言の表と言語（store）
lib/commands.ts   コマンド（ラベル・ショートカット・使える条件・実行）とキーボード
lib/dock.ts       dockview の生成・既定の配置・配置の保存・パネルを開く
lib/ui.ts         画面の共有の状態（選んだ行・最近の ELF）
components/panels/*.svelte  各パネル（Svelte の mount で dockview のパネルに入れる。context は App と同じ）
```

- 既定の配置: 左 = Source（中央、広い）、右上 = Watch / Locals / Registers（タブ）、右中 = Call Stack / Threads、
  右下 = Breakpoints / Peripherals / Memory、下 = Timeline / Terminal / Disassembly / Activity。
- dockview のテーマは app.css の色の変数に合わせる（明暗に従う）。
- Timeline は同じ部品（WebGL2 + Worker）。隠れている間は描かない。

## 5.1 パネルの働き（2026-10-18 に足したもの）

| パネル | 働き | API |
|---|---|---|
| Watch / Locals | 構造体・配列・ポインタを ▸ で開く（ポインタの先は開いたときに `*式` を読む。多い配列は続きを読む）。値のダブルクリックで書く（数・0x・列挙の名前）。前に止まったときと変わった値は赤 | `debug.evaluate`・`debug.frame_variables`・`debug.type_info`・`debug.write_expression` |
| Call Stack | フレームを選ぶと Locals・Watch・Disassembly・Source（緑の ▷）がそのフレームに従う。↑/↓ | 各 API の `frame` |
| Memory | 番地は C の式（変数はその番地、ポインタは指す先）。16 進の値のダブルクリックで書く（Tab で次へ）。止まるたび・誰かが書いたとき・Auto Refresh で読み直し、変わった値は赤 | `target.memory.view`・`target.memory.write` |
| Peripherals | フィールドを書く（1 ビット = チェックボックス、値の名前 = 選択、ほか = 数）。レジスタ全体はダブルクリック。読むと状態が変わるレジスタは ⚠ と確認 | `target.svd.registers`・`target.peripheral.write` |
| Terminal | 源のタブ（RTT のチャンネル・SWO の ITM のポート・UART・セミホスティング）、色の制御コード、行末の選択、ログの保存 | `terminal.sources`・`terminal.read`・`rtt.write`・`uart.write` |
| （画面の状態） | 視点・カーソル・段・出来事・フレームをデーモン・別の窓・AI・URL とそろえる | `ui.state.get/set`・通知 `ui.state` |

偽の機器のデモ（`fake_daemon.py --demo`）は、偽の RAM の上の型付きの変数（`host/tests/fake_values.py`。本物の C の式の
評価と値の解読）、フレームごとのローカル変数、書ける周辺レジスタ（GPIO の MODER に値の名前）、6 つの端末の源を返す。

## 5.2 G02〜G11 のビュー（2026-10-18 に足したもの）

View メニューはまとまり（`PanelDef.group`: debug / trace / analysis / tools）ごとに区切る。閉じていたパネルは、
debug は右の列、ほかは下の段に開く。既定の配置は 5 のまま。

| パネル（id） | 競合の窓 | 要件 | 主な API |
|---|---|---|---|
| Registers（書き込み） | Ozone・VS Code | G02 | `target.registers.write` |
| Memory（文字の列の編集） | Ozone・VS Code Hex Editor | G02 | `target.memory.write` |
| Breakpoints（条件・Hit Count・Log Message・ログ・ベクタキャッチ・Flash BP） | VS Code・Ozone | G02 | `debug.breakpoint.*`・`target.vector_catch` |
| Fault Analyzer（fault。故障のハンドラで止まったら自動で開く） | STM32CubeIDE・IAR | G08 | `target.fault` |
| Terminal（行の時刻・時間軸へ・Start SWO・ボーレート） | VS Code Serial Monitor・RTT Viewer | G04 | `terminal.read`（times）・`uart.configure`・`swo.start / configure` |
| SWO Trace（swoTrace） | IAR SWO Trace・CubeIDE SWV | G04 | `swo.configure / trace / unconfigure` |
| Event Log・Recordings（eventLog・recordings） | Tracealyzer・SystemView | G07 | `recording.*`・`timing.measure`・`recording.mark`（t） |
| Activity（役割・誤りの絞り込み・時間軸へ） | fprobe 独自 | G07 | `session.activity.list`・`recording.find` |
| Data Sampling（dataSampling） | Ozone・J-Scope・IAR Live Watch | G03 | `watch.*` |
| Power | J-Link Power・Ozone Power Graph | — | `probe.voltage`・記録の aux |
| CPU Load・RTOS Objects・Threads（呼び出し履歴） | Tracealyzer・SystemView・VS Code RTOS | G05 | `rtos.*` |
| Code Profile・Interrupts・Call Graph | Ozone・IAR・SystemView | G06 | `profile.*`・`debug.stack.static` |
| Logic Analyzer（解読・トリガ・測定） | Saleae・PulseView | G09 | `la.decode`・`xtrig.*`・`la.measure` |
| Dashboard | STM32CubeMonitor・DMCI | G10 | `watch.*`・`debug.write_expression` |
| Flash | J-Flash Lite | G11 | `target.flash*`・`target.flash.upload`・`target.pack.*` |

時間軸との連携（共通の部品）: ビューの行 → 時間軸は `shared().reveal({ source, t | us })`（時刻の欄は `TimeLink`、行の
ダブルクリックでも）、時間軸の 1 回クリック → ビューは `onTimeline({ select })` でその時刻の行を選ぶ、ダブルクリック →
App が `showMemory` / `showAddress`（Memory・Source・Disassembly）。ビューの結果は `tl.setMarks(owner, source, marks)` で
時間軸の目盛りの帯と縮図に印を置く。ほかの窓・AI のマウスのカーソルは `ui.state` の pointers（デーモンが呼び出し元の
name・role を付ける）を役割の色の点線で描く。グラフは `components/charts/Chart.svelte`（`lib/chart.ts`）。

### 5.3 共通の部品と振る舞い（2026-10-18 の仕上げ）

- 部品: パネルの中のタブは `panels/TabBar.svelte`（下線の型。件数・ツールチップ・←/→）。グラフは `Chart.svelte` の
  型 `type`（line / stacked / scatter。棒は bars）: CPU Load のタスク別は積み上げ（タスク・割り込み・その他 = 上端が
  負荷）、Data Sampling の XY は散布図。
- 時刻: すべての時刻の欄・グラフの横軸・時間軸の目盛りとカーソルは「記録（または捕捉）の始めからの秒」（`lib/time.ts`）。
  記録は記録の範囲の始め、捕捉はサンプル 0 を 0 とする（トリガは時間軸の破線）。単位（ns / µs / ms / s）は値の大きさ、
  桁は分解能（表は 1 単位、時間軸は 1 画素の時間、グラフは目盛りの間隔）で決め、表は有効数字 6 桁まで。時間の長さは
  有効数字 4 桁。`TimeLink` は時刻の文字を自分で出す。0 の位置は `TimelineState.origins`（源ごと。変わったときだけ配る）。
- ほかの利用者の変化: 記録を開いた・始めた・止めた は通知 `recording`（デーモンが送る）で、時間軸と記録のビューが
  読み直す。ほかのきっかけは `lib/notify.ts` の `onCalls`（完全一致のメソッド。前方一致は使わない）。読み直しは quiet
  （操作の記録に残らない）にし、通知 → 読み直し → 通知 … と続かないようにする。`setMarks` は同じ印なら知らせない。
- ほかの窓・AI のカーソル（pointers）: デーモンは利用者が切れたらその pointers を消す。画面はデーモンとの接続が
  切れたら消し、窓を閉じる（pagehide）ときは自分のカーソルを消して送る。データの読み直し・ほかの窓の状態を当てた
  ことによる視点の変化は quiet で送る（人の操作だけが操作の記録に残る）。

## 6. 試験

- 既存の試験（`npm test`・`svelte-check`・`pytest host`）は通す。
- 文言の表: 英語と日本語のキーが一致し、コンポーネントに表の外の文字列が無い（キーの参照がすべて表にある）こと。
- コマンド: ショートカットの対応・使える条件・送る API（偽の接続）。
- デーモン: `debug.elf.upload`・`debug.elf.load_demo`。偽の機器のデモ。
- 時間軸の性能は変えない（部品は同じ。測定は `host/ui/bench`）。差し替え後の再測定（Edge・WebGL2・Worker・spin）:
  フレーム間隔 p50 1.9 / p95 8.9 / p99 12.5 / 最大 15.4 ms、16.7 ms 超 0 %（前と同等）。
