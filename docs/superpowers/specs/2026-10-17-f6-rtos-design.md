# F6: RTOS の認識（タスクの一覧・タスクごとの呼び出し履歴・タスクの時間軸）

日付: 2026-10-17
対象: ホストのライブラリ `host/fprobe/rtos/`、試験用ファーム `host/firmware/freertos_demo/`
関係: F2（DWARF。`fprobe.dwarf`）、F3（共通の時間軸の記録。`fprobe.recording`）、F4（DAP サンプラ。`fprobe.sampler`）、
F1 の ITM（`fprobe.itm`）

## 1. 目的と範囲

J-Link の周辺で言えば Ozone・Keil・IAR の「RTOS の認識」と、SystemView・Tracealyzer の「タスクの時間軸」を、
fprobe の部品（DWARF・DAP サンプラ・ITM・共通の時間軸）の組み合わせで作る。

| 目標 | 確かめ方 |
|---|---|
| 止まったターゲットから FreeRTOS のタスクの一覧（名前・優先度・状態・スタック・待っているもの）を出す | 単体（偽のメモリ） |
| タスクごとの呼び出し履歴と変数（保存された文脈 → F2 の CFI の巻き戻し）。FPU の文脈の有無の両方 | 単体（偽のスタック） |
| 方法 A（組み込み無し）: DAP サンプラの `pxCurrentTCB` の値の列からタスクの時間軸。分解能と取りこぼしの印を付ける | 単体（作った値の列） |
| 方法 B（組み込みあり）: トレースフックが ITM に書く 1 語から、正確なタスクの時間軸 | 単体（ITM のバイト列） |
| どちらも共通の時間軸の記録（stream `rtos`）に書ける | 単体（`Recorder`） |
| Zephyr の最小（スレッドの一覧・状態・スタック・呼び出し履歴） | 単体（Zephyr と同じ形の構造体の ELF） |
| 試験用ファーム（FreeRTOS V11.1.0、Cortex-M4F、SRAM で動く） | ビルド（zig cc）。実機は本流 |

非目標（後で）: Cortex-M33 等の ARMv8-M の port（文脈に PSPLIM・CONTROL 等が加わる）、SMP の FreeRTOS（コアごとの
`pxCurrentTCBs[]`）、ThreadX・embOS・RTX、タスクのメモリ（ヒープ）の解析、方法 A でのスタックの使用量の時系列。

## 2. 構成

```
fprobe/rtos/
  __init__.py      detect(info) → "freertos" / "zephyr" / None、open_rtos(...)
  model.py         Task・TaskState・StackInfo・WaitInfo（RTOS に依らない結果の形。JSON にできる）
  structs.py       DWARF の型からメンバーの位置を引く StructLayout、ターゲットのメモリの読み Memory
  cortexm.py       保存された文脈（port ごとの積み方）→ レジスタ。ContextAccess（TargetAccess の約束を満たす）
  stackcheck.py    スタックの塗りつぶし（0xA5 等）から最大の使用量
  frames.py        TaskUnwinder（戻り先が罠の番地のフレームで止める）、task_inspector
  base.py          KernelAwareness: RTOS の読み手の共通部分（タスクの引き方・task_access・inspector・backtrace）
  freertos.py      FreeRTOS: リストを辿ってタスクを集め、状態・待ち・スタックを出す。タスクごとの Inspector
  zephyr.py        Zephyr の最小: _kernel.threads のリスト、thread_state、callee_saved
  timeline.py      タスクの時間軸: 方法 A（サンプル列）・方法 B（ITM の出来事）→ Timeline → 記録（stream rtos）
```

メモリとレジスタは `fprobe.dwarf.access.TargetAccess`（`read_memory(address, length)` / `read_register(name)`）で受け取る。
pyOCD・デーモン等には依存しない（本流が `Probe` の読み手を渡す）。

### 2.1 構造体の位置は DWARF から

FreeRTOS の TCB・リストの形は設定（`configUSE_TRACE_FACILITY`・`configRECORD_STACK_HIGH_ADDRESS`・
`configUSE_LIST_DATA_INTEGRITY_CHECK_BYTES`・`configUSE_MINI_LIST_ITEM`・MPU 等）で変わる。OpenOCD のように位置を
決め打ちにせず、ELF の DWARF の型（`TCB_t` / `struct tskTaskControlBlock`、`List_t`、`ListItem_t`、`Queue_t`）から
メンバーの位置を引く。メンバーが無ければ（設定で外れていれば）その欄は `None` にする（例: `pxEndOfStack` が無ければ
スタックの大きさは分からない）。カーネルの変数（`pxReadyTasksLists` 等、`tasks.c` の static）も DWARF の位置で引く。

前提: ELF に DWARF があること（`-g`）。カーネルの変数が最適化で消えていないこと（FreeRTOS は `static` の変数を
`portDONT_DISCARD` 等で残す。消えていれば分かりやすい誤りにする）。

## 3. FreeRTOS: タスクの一覧（止めて調べる）

### 3.1 集め方

止まったターゲットで、次のリストを辿る（`xListEnd.pxNext` から、番地が `&xListEnd` に戻るまで。項目の数
`uxNumberOfItems` と、上限 256 で、壊れたリストでも止まる）。項目の `pvOwner` が TCB。

| リスト | 状態 |
|---|---|
| `pxReadyTasksLists[0..configMAX_PRIORITIES-1]` | 実行可能（`pxCurrentTCB` なら実行中） |
| `xDelayedTaskList1`・`xDelayedTaskList2` | 待ち（時間切れ付き） |
| `xPendingReadyList`（項目は `xEventListItem`） | 実行可能（スケジューラの停止中に起こされた。`pending_ready`） |
| `xSuspendedTaskList` | 停止。ただし `xEventListItem` がリストに入っている、または `ucNotifyState` が通知待ち（1）なら、時間切れ無しの待ち（FreeRTOS の `eTaskGetState` と同じ規則） |
| `xTasksWaitingTermination` | 削除待ち（アイドルタスクが片付ける前） |

同じ TCB が 2 つのリストに見えたら（`xPendingReadyList` は `xEventListItem` で繋がるため）、`pending_ready` を優先する。
リストの外の `pxCurrentTCB` も実行中として加える（片付け途中等）。

### 3.2 タスクの欄

| 欄 | 出し方 |
|---|---|
| `handle` | TCB の番地 |
| `name` | `pcTaskName`（NUL まで、`configMAX_TASK_NAME_LEN`） |
| `priority` / `base_priority` | `uxPriority` / `uxBasePriority`（ミューテックスの優先度の継承中は違う） |
| `number` | `uxTCBNumber`（`configUSE_TRACE_FACILITY`。無ければ None） |
| `state` | running / ready / blocked / suspended / deleted |
| `wait` | 3.3 |
| `stack` | 3.4 |

### 3.3 待っているもの

- `xEventListItem.pxContainer`（イベントのリスト）が NULL でなければ、そのリストを持つものを探す:
  1. キューの登録簿（`xQueueRegistry[]`、`configQUEUE_REGISTRY_SIZE`）の各ハンドルについて、
     `&handle->xTasksWaitingToReceive` / `&handle->xTasksWaitingToSend` と一致するか。
  2. 一致しなければ、番地のシンボル（静的に確保したキュー `StaticQueue_t` 等）。シンボルの先頭からの位置が
     `xTasksWaitingToReceive` / `xTasksWaitingToSend` の位置なら、そのキュー。
  3. どちらも無ければ `object = リストの番地 − 受信のリストの位置` を推定とせず、`list` の番地だけを返す。
- キューの種類: `ucQueueType`（0 キュー、1 ミューテックス、2 計数セマフォ、3 2 値セマフォ、4 再帰ミューテックス）。
  無ければ `pcHead == NULL` → ミューテックス、`uxItemSize == 0` → セマフォ、それ以外 → キュー。
  ミューテックスなら持ち主（`u.xSemaphore.xMutexHolder` → タスク名）。
- `reason`: `receive`（キューから受け取る・セマフォ・ミューテックスを取る）/ `send`（キューに送る）/ `event`（その他の
  イベントのリスト）/ `notification`（`ucNotifyState` = 1）/ `delay`（時間だけ）。待ちの無い状態（実行中・実行可能・
  停止・削除待ち）は `wait = None`。Zephyr は `pend`（`pended_on`）/ `sleep`。
- 遅延リストにいれば `wake_tick`（`xStateListItem.xItemValue`）と、`xTickCount` からの残り（`remaining_ticks`、
  `TickType_t` の幅で折り返す）。時間切れ無しの待ちは `wake_tick = None`。

### 3.4 スタック

ARM の FreeRTOS はスタックが下へ伸びる（`portSTACK_GROWTH < 0`）。`pxStack` が最も低い番地（スタックの底の外れ）。

| 欄 | 内容 |
|---|---|
| `base` | `pxStack` |
| `end` | `pxEndOfStack + 4`（`configRECORD_STACK_HIGH_ADDRESS`。無ければ None） |
| `size` | `end − base`（`end` が無ければ None） |
| `pointer` | いまの SP（止まっているタスクは `pxTopOfStack`、実行中のタスクは PSP） |
| `used` | `end − pointer`（いまの使用量） |
| `peak_used` | `size − untouched`（スタックの最大の使用量） |
| `untouched` | `base` から上へ、塗りつぶしの値（`0xA5`、`tskSTACK_FILL_BYTE`）が続くバイト数 |
| `fill` | `"intact"`（底の 16 バイトが塗りのまま）/ `"none"`（底が塗られていない: 塗らない設定か、あふれた） |
| `overflow` | `pointer < base`、または塗りがあるのに底の 16 バイトが壊れている |

塗りつぶしは、FreeRTOS が `configCHECK_FOR_STACK_OVERFLOW > 1`・`configUSE_TRACE_FACILITY`・
`INCLUDE_uxTaskGetStackHighWaterMark` のいずれかで行う。塗りの確かめは 256 バイトずつ読む（大きなスタックでも
読みの回数を抑える）。結果は「塗りが残っていた最も深い位置」で、塗りの値と同じ値を書いたスタックは使っていないと
数える（FreeRTOS の `uxTaskGetStackHighWaterMark` と同じ限界）。

## 4. タスクごとの呼び出し履歴（保存された文脈 → CFI の巻き戻し）

### 4.1 Cortex-M の FreeRTOS の port の積み方

止まっているタスクは、PendSV（`xPortPendSVHandler`）が PSP に積んだ文脈を持ち、その先頭が `pxTopOfStack`。

| port | `pxTopOfStack` から上へ |
|---|---|
| `armv7m_fp`（GCC/ARM_CM4F・ARM_CM7） | r4〜r11、EXC_RETURN（r14）、[EXC_RETURN の bit 4 = 0 なら s16〜s31]、ハードのフレーム |
| `armv7m`（GCC/ARM_CM3・ARM_CM0 等、FPU の文脈なし） | r4〜r11、ハードのフレーム（EXC_RETURN は 0xFFFFFFFD とみなす） |

ハードのフレーム（例外の入口で積まれる）: r0 r1 r2 r3 r12 lr pc xpsr、拡張フレームなら続けて s0〜s15・FPSCR・予約。
xpsr の bit 9 が 1 なら 4 バイトの詰め物。タスクの SP = フレームの直後。拡張フレームの s0〜s15 は、PendSV の
`vstmdbeq {s16-s31}` が遅延退避を済ませるので有効。

port は `"auto"` が既定: ELF に `vPortEnableVFP`（ARM_CM4F・CM7 の port にだけある）があれば `armv7m_fp`、無ければ
`armv7m`。指定もできる。

### 4.2 巻き戻しへのつなぎ

- 保存された文脈のレジスタ（r0〜r12・sp・lr・pc・xpsr・psp、FP の文脈なら s0〜s31）と、ターゲットのメモリを合わせた
  `ContextAccess`（`TargetAccess` の約束）を作る。F2 の `Inspector(info, ContextAccess, symbols)` がそのまま使え、
  呼び出し履歴（CFI）・各フレームの引数とローカル変数・C の式の評価が、タスクごとに得られる。
- 実行中のタスクは実レジスタ（渡された読み手そのまま）。例外の中で止まっていれば、F2 の巻き戻しが EXC_RETURN の
  フレームをたどってタスクのコードへ戻る。
- タスクの関数の戻り先は `prvTaskExitError`（port がタスクの最初の文脈に積む）。そこから先は呼び出し元ではないので、
  その戻り先のフレームで巻き戻しを止める（`Unwinder` を包む）。一度も動いていないタスクは、フレーム 0 がタスクの関数の
  先頭になる。
- 保存されていないレジスタ（FP の文脈が無いタスクの s0〜s31、msp）は「分からない」（`AccessError`）とし、嘘の値を出さない。

## 5. タスクの時間軸（止めずに見る）

結果の形は方法によらず `Timeline`:

| 欄 | 内容 |
|---|---|
| `method` | `"sampled"`（方法 A）/ `"itm"`（方法 B） |
| `t0`, `t1` | 対象の時刻の範囲（タイムベース） |
| `resolution` | 時刻の分解能（タイムベース）。区間の端の不確かさ |
| `runs` | `Run(task, handle, t0, t1, uncertainty, samples)`: そのタスクが CPU を使っていた区間 |
| `switches` | `Switch(t, from, to, uncertainty)` |
| `gaps` | 情報の無い区間（サンプルの抜け・ITM のあふれ）。統計から除く |

`stats()`: タスクごとの合計時間・CPU の使用率（情報のある時間に対して）・実行の回数・最大／最小の実行時間、全体の切り替えの回数。

### 5.1 方法 A（組み込み無し）: `pxCurrentTCB` のサンプル

入力は `(t, 値)` の列（F4 の DAP サンプラ → 記録の stream `sample` の、`a == &pxCurrentTCB` の出来事）と、TCB の番地 →
タスク名（止めて取った一覧、または動作中にメモリを読んで作る。分からない番地は `"0x2000ABCD"`）。

- 同じ値が続くサンプルを 1 つの区間にまとめる。値が変わった 2 つのサンプルの間で切り替えが起きた。切り替えの時刻は
  その 2 点の中点、不確かさは間隔の半分。
- 分解能 `resolution` は、指定された周期、無ければサンプルの間隔の中央値。
- 使う側が `gap_factor` を明示したときだけ、間隔が `gap_factor` × 分解能を超えたところを `gap` にし、区間を切る（既定は判定しない。経験則の倍率を持たない。2026-10-21）。
- **取りこぼし（仕様の限界）**: サンプリングの周期より短い実行は、まるごと見えないことがある。A → B と見えても、間に
  C が短く走ったかもしれない。したがって
  - 切り替えの回数は **下限**、実行の回数も下限。
  - 各区間の長さの誤差は ±1 周期。1 サンプルだけの区間（`samples == 1`）は「周期より短いかもしれない」。
  - CPU の使用率は、周期より十分長く走るタスクについては標本の割合として偏りなく推定できる（短いタスクは
    サンプルの割合で統計的には数えられるが、個々の実行は見えない）。
  - RTOS のティック（例えば 1 ms）より細かい切り替え（割り込みからの起床）を見るには、周期を 10〜100 µs にする。
- 結果に `resolution`・`lower_bound: true`（回数が下限であること）を付ける。

### 5.2 方法 B（組み込みあり、正確）: トレースフック → ITM

試験用ファームの `fprobe_trace.h`（`FreeRTOSConfig.h` から読む）が、FreeRTOS のトレースフックで ITM のスティミュラス・
ポート 31 に 1 語を書く。

| bit | 内容 |
|---|---|
| 31..2 | TCB の番地の bit 31..2（TCB は 4 バイト以上に揃う） |
| 1..0 | 0 = 実行を始めた（`traceTASK_SWITCHED_IN`）、1 = やめた（`traceTASK_SWITCHED_OUT`）、2 = 作った、3 = 消した |

既定は無効（`fprobe_trace_enabled = 0`）。デバッガが 1 を書き、ITM（`TCR.ITMENA` と `TER` の bit 31）と SWO を
有効にすると出る。ITM が無効なら FIFO を待たない（止まらない）。

ホスト:
- `itm_packets(bytes_with_time)`: 記録の stream `swo`（`t`, バイト）を `ItmDecoder` で解読し、パケットの先頭のバイトの
  時刻を付ける（`Instrumentation.offset` で引く）。
- `task_events(packets, port=31)`: 1 語の出来事 `TaskEvent(t, kind, handle)` に。`Overflow`・`DecodeError` は
  `gap`（あふれ。その後の最初の `task_in` まで区間を作らない）。
- `timeline_from_events(events, names, resolution)`: `task_in` で区間を始め、`task_out` か次の `task_in` で閉じる。
  `task_out` と次の `task_in` の間（スケジューラの中）はどのタスクにも数えない。同じタスクの out → in は切り替えに数えない。
- 分解能: SWO のバイトの時刻（FPGA のタイムベース）は、出来事からバイトが線に出るまでの遅れ（1 語 = 5 バイト。
  2 Mbaud で約 25 µs、FIFO が詰まればもっと）を含む。遅れはほぼ一定なので区間の長さへの影響は小さいが、
  他の段（ロジアナ等）との比較では、この遅れを `resolution`（既定: パケットの 5 バイトの時間）として付ける。
  ITM のローカルのタイムスタンプ（`Timestamper`）を使う形は後で（CPU のクロック → タイムベースの写しが要る）。

### 5.3 共通の時間軸の記録（stream `rtos`）

`write_timeline(rec, timeline)` と `write_task_events(rec, events, names)`:

| kind | t | t_end | a | b | body |
|---|---|---|---|---|---|
| `run` | 区間の始め | 区間の終わり | TCB | 0 | `{"task", "method", "resolution", "samples"?}`、不確かさ = 端の不確かさ |
| `switch` | 切り替え | — | 前の TCB | 次の TCB | `{"from", "to"}`、不確かさ |
| `gap` | 抜けの始め | 抜けの終わり | 0 | 0 | `{"reason"}` |
| `task_in` / `task_out` / `create` / `delete` | 出来事（方法 B の生の出来事） | — | TCB | 0 | `{"task"}` |

`run` が時間軸の段（タスクごとの帯）、`switch` が切り替えの印、`task_in`・`task_out` は方法 B の生の出来事
（`Recording.intervals` で「task_in → task_out」の時間の統計が取れる）。

## 6. Zephyr の最小

- 前提: `CONFIG_THREAD_MONITOR`（`_kernel.threads` のリスト）、`CONFIG_THREAD_NAME`（名前。無ければ番地）、
  `CONFIG_THREAD_STACK_INFO`（スタックの範囲。無ければ None）。
- 一覧: `_kernel.threads` → `next_thread`。実行中は `_kernel.cpus[0].current`（または `_kernel.current`）。
- 状態（`base.thread_state` のビット、`kernel_structs.h`）: `_THREAD_DEAD`（bit 3）・`_THREAD_ABORTING`（bit 5）→ deleted、
  `_THREAD_SUSPENDED`（bit 4）→ suspended、`_THREAD_PENDING`（bit 1）・`_THREAD_SLEEPING`（bit 2。古い版の
  `_THREAD_PRESTART` = 起動前も同じビット）→ blocked、それ以外 → ready（`current` なら running）。
  待ち: `base.pended_on`（待ちキューの番地。シンボルがあれば名前）。
- 優先度: `base.prio`（符号付き。小さいほど高い）。
- 文脈（Cortex-M）: `callee_saved`（v1〜v8 = r4〜r11、psp）。ハードのフレームは psp から（FP の文脈は扱わない）。
- 構造体の位置は FreeRTOS と同じく DWARF から。試験は Zephyr と同じ名前・形の構造体を持つ小さな ELF（試験用の C）で行う。

## 7. 判断

| 判断 | 理由 | 誤っていた場合のコスト |
|---|---|---|
| 構造体の位置は DWARF から（決め打ちしない） | 設定・版で TCB の形が変わる。F2 の部品がある | 低（DWARF の無い ELF では使えない。そのときは誤りにする） |
| 保存された文脈を `TargetAccess` にして F2 の `Inspector` をそのまま使う | 巻き戻し・変数・式を作り直さない。実行中のタスクと同じ道具 | 低 |
| port の積み方は表（`armv7m` / `armv7m_fp`）で、自動は `vPortEnableVFP` の有無 | port の数は少なく、違いは積む順だけ | 低（ARMv8-M は表に足す） |
| `prvTaskExitError` で巻き戻しを止める | そこから先は呼び出し元ではない（偽の履歴を出さない） | 低 |
| 方法 A の切り替えの時刻は中点、不確かさは間隔の半分 | 長さの推定に偏りが無い。端の誤差を数で示す | 低 |
| 方法 B の語は TCB の番地 + 2 ビットの出来事 | 1 回の書き込み（ITM の 5 バイト）で済む。名前はホストが TCB から引く | 低（語の形はファームと `timeline.py` の 2 か所） |
| `run`（区間）と `switch` を記録に書く。方法 B は生の `task_in`/`task_out` も | 時間軸の段は区間で描け、生の出来事は統計（`intervals`）に使える | 低 |

## 8. 試験

- 偽のメモリ: ELF の PT_LOAD の像（`ImageAccess.from_symbols`）に `.bss` を 0 で足し、試験の組み立て役が DWARF の
  型の位置で TCB・リスト（`vListInitialise`・`vListInsertEnd` と同じ繋ぎ方）・キュー・登録簿・スタックの塗りを
  `ucHeap` の中に組み立てる。
  - 一覧: 6 つのタスク（実行中・実行可能・遅延の待ち・キューの無期限の待ち・セマフォの時間切れ付きの待ち・停止・
    削除待ち）、名前・優先度・状態・待っているもの（キュー名・種類・ミューテックスの持ち主）・残りのティック。
  - スタック: 塗りの残り・最大の使用量・いまの使用量・あふれの印。
  - 巻き戻し: 試験の `StackBuilder`（逆アセンブルで前置きをなぞる。CFI とは独立）で `worker_task → recurse × n` の
    スタックを作り、PendSV と同じ積み方で文脈を積む（FP あり・なし）。タスクごとの呼び出し履歴の関数名・引数の値、
    `prvTaskExitError` で止まること、実行中のタスクは実レジスタから。
- 方法 A: 作った値の列（周期・抜け・1 サンプルだけの短い実行）から、区間・中点・不確かさ・使用率・切り替えの回数・gap。
- 方法 B: `fprobe_trace.h` と同じ語を ITM のパケットにしたバイト列（時刻付き）から、区間・使用率・あふれの gap。
- 記録: 一時ディレクトリの `Recorder` に書き、`Recording.events(streams=["rtos"])` で読み返す。
- Zephyr: Zephyr と同じ名前・形の構造体を持つ小さな ELF（`host/firmware/zephyr_shape/`。スレッドのリストと状態は .data の初期値）で、一覧・状態・待ち・スタック・呼び出し履歴。

## 9. 本流での API・MCP への組み込みの提案

（担当 F はライブラリと単体試験だけ。組み込みは本流。）

**組み込んだ形（担当 I）**: `host/fprobe/probe_rtos.py`（`RtosTools`、`Probe` の mixin）。API は `rtos.info`・`rtos.tasks`・
`rtos.backtrace`・`rtos.read`・`rtos.timeline.start` / `stop` / `get`（`docs/api.md`）。下の提案との違い:

- RTOS の種類はケイパビリティでなく `rtos.info` で申告する（ELF の性質で、機器の性質ではないため）。読み手は呼ぶたびに作る。
- 時間軸の周期は `rate_hz`（`watch.set` と同じ）。方法 A は FreeRTOS に限らず `KernelAwareness.current_address()`
  （Zephyr は `_kernel.cpus[0].current`）を読む。DAP サンプラは `watch` と取り合うので、持ち主を決める（`probe_window.py`）。
- 方法 B は SWO の受信のエンドポイントを読まず、イベント記録の SWO のバイト（デバイスの時刻付き）を読む。記録中に始めたら記録の
  stream `swo` から、そうでなければイベント記録のリングから（窓。`probe_window.py`）。`swo_baud`・`core_clock_hz` を渡すと
  SWO の受信と TPIU も設定する（ピンの設定はチップ固有なので含まない）。
- 一覧に無い TCB（始めた後に作られたタスク・動作中に始めたとき）の名前は、動作中の読みで `handle_name` から引く。

- `Probe`（デーモン）:
  - 接続時・ELF の読み込み時に `fprobe.rtos.detect(DebugInfo)` で RTOS を見つけ、`rtos` のケイパビリティとして申告。
  - 止まったときに `open_rtos(info, access, symbols)` を作り直す（ターゲットが動いたら捨てる。`Inspector` と同じ寿命）。
- API（`fprobe_api.json`）・MCP の道具（案）:
  | 名前 | 内容 |
  |---|---|
  | `rtos.info` | RTOS の種類・版（分かれば）・ティック・タスクの数 |
  | `rtos.tasks` | タスクの一覧（`Task.to_dict()`。止まっているときだけ） |
  | `rtos.backtrace` | `task`（名前か番地）、`variables`。タスクごとの呼び出し履歴 |
  | `rtos.read` | `task`、`expression`、`frame`。そのタスクの文脈で C の式を読む |
  | `rtos.timeline.start` | 方法 `sampled`（周期 µs）/ `itm`。A は `Sampler.configure([(&pxCurrentTCB, 4)])`、B は `fprobe_trace_enabled = 1` と ITM・SWO の設定 |
  | `rtos.timeline.stop` / `rtos.timeline.get` | 記録の範囲から `Timeline`（`stats()` 付き）を返し、`write_timeline` で stream `rtos` に書く |
- GUI: 時間軸の段に「RTOS」（タスクごとの帯 = `run`、`switch` の印）。タスクの一覧の表から呼び出し履歴へ。
  デバッガのスレッドの一覧（`Cortex-Debug` の RTOS のスレッドと同じ見せ方）。
- 方法 A の名前の表: 止めた一覧の `{handle: name}` を `rtos.timeline.start` のときに取って持っておく（動作中に作られた
  タスクは、未知の番地を見つけたら動作中の読みで TCB の名前を読む）。

## 10. 実機で確かめる手順（本流）

1. ファームを作る: `python host/firmware/freertos_demo/build.py`（生成済みの ELF もリポジトリにある。担当 F の worktree で作ったため DWARF のソースの場所は worktree を指す。本流の場所で作り直すと、ソースの表示が本流のファイルを指す）。
2. Nucleo F401RE（ST-LINK ではなく fprobe の SWD に配線）に SRAM で読み込む: VTOR・SP・PC をベクタ表から設定して
   実行（sram_demo と同じ手順。ファームも VTOR を自分で設定する）。
3. 1 秒ほど走らせて止め、`rtos.tasks`:
   - タスクは `ctrl`・`sensor`・`worker`・`fpu`・`dormant`・`IDLE` の 6 つ。`dormant` は suspended。`ctrl` は
     blocked（`cmd_queue` の receive、時間切れ無し）。`sensor` は blocked（delay）か実行可能。`worker` は blocked
     （`done_sem`、時間切れ付き）か `recurse` の中。
   - 各タスクのスタックの `peak_used` が `size` より小さく、`overflow` が false。`worker` の `peak_used` が最も大きい。
4. `rtos.backtrace task=worker variables=true`: `xQueueSemaphoreTake` 等 → `worker_task`（`prvTaskExitError` で止まる）。
   `recurse` の中で止めたとき（ブレークポイント）、`recurse` が 7 段・引数 `depth` が 0〜6。
5. `rtos.backtrace task=fpu`: `fpu_step` の中で切り替わったときは FP の文脈（EXC_RETURN = 0xFFFFFFED）。`x` が浮動小数点で読める。
6. 方法 A: `Sampler.configure([(&pxCurrentTCB, 4)], period_s=20e-6)`、記録を 1 秒。`timeline_from_samples`:
   `sensor` が約 200 回（5 ms ごと）、`fpu` が約 500 回、`IDLE` の使用率が最も大きい。`resolution` が 20 µs。
7. 方法 B: SWO を有効にし（ITM ポート 31、`TER` の bit 31）、`fprobe_trace_enabled = 1` を書く。記録の stream `swo` から
   `itm_packets` → `task_events` → `timeline_from_events`。6 と同じ区間が、より細かく（数 µs の実行も）見えること。
   切り替えの回数が方法 A 以上であること（A は下限）。
8. 方法 A と B を同時に: 同じ区間の端の差が、A の `resolution` 以内であること。
