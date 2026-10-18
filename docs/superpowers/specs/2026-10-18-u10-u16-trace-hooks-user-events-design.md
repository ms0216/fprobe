# U10・U16: トレースフックの拡張と、ターゲットが出す値・イベント（設計）

日付: 2026-10-18。台帳: `docs/reports/2026-10-18-gap-analysis.md` の U10・U16。
競合の機能: U10 = SEG-69, TZ-01, TZ-04, TZ-09, TRX-04, TRX-05, FRT-03。
U16 = SEG-67, SEG-70, IAR-10, KEIL-02, ADS-04, TZ-01, TZ-05, MPLB-02, VSC-02, ZEP-03, FRT-04。
前提: F6（`2026-10-17-f6-rtos-design.md` の 5.2 節。ポート 31 の 1 語）、U09（`2026-10-18-u08-u09-rtos-objects-timeline-design.md`）。

## 1. U10: カーネルの出来事（ターゲット側 `fprobe_trace.h` / `fprobe_trace.c`）

### 1.1 ポートと語の形

ポート 31（タスクの切り替え・作成・削除）は F6 のまま（1 語。互換）。カーネルの出来事は **記録**（見出し 1 語 + 中身 0〜3 語）で、

| ポート | 中身 |
|---|---|
| 30 | 見出し: bit 7..0 = 出来事の番号、bit 9..8 = 続く中身の語の数、bit 18..10 = IPSR（0 = タスク、15 = SysTick、16 以上 = IRQ）、bit 31..19 = 引数（13 ビット、超えたら飽和） |
| 29 | 中身の語（見出しの後に数だけ） |

見出しと中身を別のポートにしたのは、あふれ（Overflow）で語が落ちても、次の見出しから確実に読み直せるため
（ポート 30 の語は必ず記録の始まり）。中身の数が足りないまま次の見出し・あふれが来た記録は捨てて数える（`lost`）。
記録は割り込みを禁止（PRIMASK を保存して `cpsid i`、最後に戻す）して書く: ISR のフックの記録が途中に割り込まない。
ITM の FIFO の順序は書いた順なので、ポート 31 の語とカーネルの記録は時刻でなく **並び** で正しく前後が決まる。

オブジェクトの番地の語は bit 1..0 に種類（0 = キュー、1 = セマフォ、2 = ミューテックス、3 = その他: イベントグループ・
ストリームバッファ・通知）。キューの構造体は 4 バイトに揃うので番地は失わない。種類はフックの中で決める
（`pcHead == NULL` = ミューテックス、`uxItemSize == 0` = セマフォ）。トレースの設定（`configUSE_TRACE_FACILITY`）に依らない。

### 1.2 出来事

| 番号 | 名前 | フック | 中身 | 引数 |
|---|---|---|---|---|
| 1 | ready | `traceMOVED_TASK_TO_READY_STATE` | TCB | — |
| 2 | delay | `traceTASK_DELAY`・`traceTASK_DELAY_UNTIL`（タイマのタスクの待ちも） | TCB（いまのタスク） | — |
| 3 | suspend | `traceTASK_SUSPEND` | TCB | — |
| 4 | block | `traceBLOCKING_ON_QUEUE_RECEIVE/SEND/PEEK`・`traceBLOCKING_ON_STREAM_BUFFER_*`・`traceEVENT_GROUP_WAIT_BITS_BLOCK`・`traceEVENT_GROUP_SYNC_BLOCK`・`traceTASK_NOTIFY_*_BLOCK` | オブジェクト（通知は 0） | 0 = 受け、1 = 送り、2 = 覗き、3 = 通知 |
| 5 / 6 / 7 | prio_inherit / prio_disinherit / prio_set | `traceTASK_PRIORITY_INHERIT` / `_DISINHERIT` / `_SET` | TCB | 新しい優先度 |
| 16 / 17 | send / send_failed | `traceQUEUE_SEND(_FROM_ISR)(_FAILED)`（セマフォ・ミューテックスの give も） | オブジェクト | 操作の前のメッセージ数 |
| 18 / 19 | receive / receive_failed | `traceQUEUE_RECEIVE(_FROM_ISR)(_FAILED)`（take も） | オブジェクト | 同上 |
| 20 | peek | `traceQUEUE_PEEK(_FROM_ISR)` | オブジェクト | 同上 |
| 21 | create | `traceQUEUE_CREATE` | オブジェクト、長さ | ucQueueType |
| 22 / 23 | stream_send / stream_receive | `traceSTREAM_BUFFER_SEND/RECEIVE(_FROM_ISR)` | オブジェクト | バイト数 |
| 24 | event_set | `traceEVENT_GROUP_SET_BITS(_FROM_ISR)` | オブジェクト | 立てたビット（下 13） |
| 25 | notify | `traceTASK_NOTIFY(_FROM_ISR)`・`traceTASK_NOTIFY_GIVE_FROM_ISR` | 通知先の TCB | 通知の番号 |
| 32 / 33 | isr_enter / isr_exit | `traceISR_ENTER`・`traceISR_EXIT`・`traceISR_EXIT_TO_SCHEDULER`（port の SysTick）、アプリの ISR は `FPROBE_ISR_ENTER()` / `FPROBE_ISR_EXIT()` | — | — |

だれがしたか（actor）は語に入れない: IPSR が 0 ならその時点で実行中のタスク（並びで直前のポート 31 の task_in）、
0 でなければ割り込み（IPSR の番号）。語を減らすため（SWO の帯域がフックの負荷の主）。あふれの後、次の task_in までは
タスクが分からない（actor = 不明）。再帰ミューテックスの 2 回目以降の take / give（数だけ変わる）は記録しない。

### 1.3 有効にする手順

既定は無効。デバッガが `fprobe_trace_enabled` に 1 を書き、ITM（TCR.ITMENA と TER の 29・30・31）と SWO を有効にすると出る。
SWO の受信を始めてから `fprobe_trace_enabled` を書く（逆だと始めのバイトで解読がずれる）。

### 1.4 フックの負荷の見積もり

1 語の書き込み（FIFO が空いているとき）: 有効かの確認（ロード 3・分岐）+ FIFO の確認 + ストア ≈ 10〜15 サイクル。
記録: 関数呼び出し + IPSR・PRIMASK の読み + 語ごとに FIFO の確認とストア ≈ 25 + 8 × 語 サイクル（`fprobe_trace.c` の
逆アセンブルの命令数から。試験 `test_freertos_demo_calls_the_record_from_the_kernel_and_the_record_code_is_small` が命令数の上限を見張る）。
**主な負荷は SWO の帯域**: 1 語 = 5 バイト = 50 ビット時間。2 Mbaud で 25 µs。FIFO（Cortex-M4 は 1 語ぶん）が埋まると
次の書き込みは前の語が出るまで待つ（割り込み禁止の間も）。ホストは実際の語の数から帯域の使用率と、CPU の負荷の
見積もり（語 × サイクル / コアのクロック）を `rtos.timeline.overhead` で出す。実機の計測（トレースの有無で
アイドルの回る数の差）は本流（`gateware/tests/hil/test_trace_hooks_hw.py`）。

## 2. U10: ホスト（`fprobe/rtos/kernel_trace.py`・`fprobe/rtos/flow.py`）

- `kernel_trace(packets)`: ITM のパケット（時刻付き）→ `KernelOp`（時刻・名前・actor・IPSR・オブジェクトと種類・対象の TCB・引数）
  の並び。ポート 31 の語も同じ並びで読んで actor を決める。
- 応答時間: `kernel_job_events(ops, task_events)` → `KernelEvent`（ready / block・delay・suspend・delete）。U09 の
  `find_jobs(events=…)` がそれで区切り、`release`（ready）からの応答時間を出す（`rtos.timeline.jobs`）。
- **Communication Flow**（TZ-04・FRT-03）: `communication_flow(ops)` → 節（タスク・割り込み・オブジェクト）と有向の辺
  （送り・give: 行為者 → オブジェクト、受け・take: オブジェクト → 行為者、通知: 行為者 → タスク、event_set）と回数・失敗の回数・
  待った回数。Graphviz の DOT も返す。
- **ブロック時間**（TZ-09）: `blocking(ops)` → 待ちの区間（block / delay / suspend → 次の ready）。タスクごと・待ったものごとの
  回数・合計・最大（いつ）と、結果（その後の同じオブジェクトの操作が成功 = ok、失敗 = timeout）。
- **キュー使用率**（TZ-09）: `queue_usage(ops, lengths)` → オブジェクトごとのメッセージ数の推移（操作の後の数）、最大・時間で重みを
  付けた平均・長さに対する割合、満杯（send_failed・送りの待ち）と空（receive_failed）の回数。長さは `rtos.objects`（始めに
  走行中に読む）か create の出来事から。
- **優先度の逆転の正確な区間**（TRX-05・TZ-09）: `inversions_from_ops(tl, ops, priorities)` → ミューテックスごとに持ち主（take した
  タスク）と待ち手（block した、まだ ready でないタスク）を並びで追い、持ち主の元の優先度より高い待ち手がいる区間
  [待ち手の block, 持ち主の give か待ち手の ready）。中間の優先度のタスクの時間（U09 と同じ計算）と、継承の出来事（いつ・何に）。
  サンプラ（U09）と違い取りこぼしが無い。
- **割り込みの区間**: isr_enter / isr_exit（IPSR で対にする）→ `Activation`。例外トレース（`exceptions=true`）が無いときに使う。
- 記録（stream `rtos`）: 出来事（kind = 名前、a = オブジェクトか対象の TCB、b = actor、body に名前）と、待ちの区間
  `blocked`（t_end 付き）、逆転の区間 `inversion`（t_end 付き）。

### 2.1 API

| 名前 | kind | 内容 |
|---|---|---|
| `rtos.timeline.events` | control（observes） | カーネルの出来事の一覧（kinds・task・object で絞る） |
| `rtos.timeline.flow` | control（observes） | Communication Flow |
| `rtos.timeline.blocking` | control（observes） | ブロック時間 |
| `rtos.timeline.queues` | control（observes） | キュー使用率 |
| `rtos.timeline.overhead` | control（observes） | トレースの語の数・帯域・CPU の負荷の見積もり |
| `rtos.timeline.inversions` | 既存 | 出来事があればそれで（`source: "trace"`）、無ければサンプラ（`source: "sampled"`） |
| `rtos.timeline.jobs` | 既存 | 出来事があれば ready / block で区切る（応答時間） |

## 3. U16: ターゲットが出す値とイベント

### 3.1 ターゲット側 `fprobe_user.h`（ヘッダだけ。RTOS に依らない）

- 値: `FPROBE_PLOT_I32(port, v)`・`FPROBE_PLOT_U32`・`FPROBE_PLOT_F32`・`FPROBE_PLOT_I16`・`FPROBE_PLOT_I8`: ITM のポート
  （1〜23）に 1 回の書き込み。型の解釈はホストの設定（J-Scope・DataPlot と同じ: ポートごとに型）。
- 名前付きのイベント（自己記述。ホストの設定が要らない）: ポート 24 の語 = 名前の文字列の番地 | 種類（bit 1..0:
  0 = mark、1 = start、2 = stop、3 = value）。value はポート 25 に値の語が続く（2 語は割り込みを禁止して書く）。
  名前の文字列は 4 バイトに揃えた static const（`FPROBE_EVENT_START("adc")`）。ホストは ELF の中身から文字列を読む。
  value の名前の先頭の `i:` / `u:` / `f:` / `q:` が型（i32・u32・f32・Q16.16。マクロが付ける）。
- RTT: 同じ形の語をチャンネルに書く `fprobe_user_rtt_event(...)` 相当は、アプリの RTT の書き込み関数を受ける
  `FPROBE_USER_RTT_WRITE` を定義したときだけ（SEGGER のコードを同梱しない）。1 回の書き込み = 1 記録（4 か 8 バイト）。
  ITM の無い Cortex-M0+ でも使える。

### 3.2 ホスト（`fprobe/usertrace.py`、入出力なし）

- 型: `i8`・`u8`・`i16`・`u16`・`i32`・`u32`・`f32`・`qM.N`（符号付きの固定小数点、M + N = 8・16・32。例 `q16.16`・`q1.15`）、
  `struct`（`fields: [[名前, 型], …]`。詰めた little endian）。
- 出どころ: ITM のポート（スカラーは 1 回の書き込み = 1 値。書き込みの大きさが型と違えば誤りに数える。struct は
  バイトを貯めて大きさごと。あふれで貯めたものは捨てる）、RTT のチャンネル（バイトの流れを大きさごと）、
  名前付きイベント（ITM のポート 24・25 か RTT のチャンネル `events`）。
- 区間: start と stop を名前ごとに後入れ先出しで対にする（入れ子・割り込みの中の区間も正しい）。対の無い stop は数える。
- 統計: 値は名前ごと（struct は欄ごと）の数・最小・最大・平均・最後。区間は `interval_report`（`timing.measure` と同じ:
  分位・分布・外れ値と時刻）。Keil の Event Statistics・SystemView のユーザイベント・Tracealyzer の User Event 相当。

### 3.3 Probe（`probe_user.py`）と記録

- `user.trace.start(channels, events, event_rtt_channel, swo_baud, core_clock_hz)`: ITM を使うなら SWO の受信を始め、窓を開いてから
  ポートを有効にする（受信が先）。RTT は `rtt_start` 済みの受信を横で受け取る（ホストの時刻 → 機器の時刻は時計の対応付けで、
  不確かさ付き）。
- `user.trace.stop`: 記録中なら stream `user` に書く: `value`（a = ポートかチャンネル、body = name・value・source）、
  `mark` / `start` / `stop`（body = name）、`interval`（t_end 付き、body = name・duration）。よって
  `timing.measure({"stream":"user","kind":"start","body":{"name":"adc"}}, {"stream":"user","kind":"stop","body":{"name":"adc"}})`
  がそのまま使え、`recording.find`・GUI の時間軸にも出る。
- `user.trace.get`（値とイベントの一覧）・`user.trace.stats`（統計）は取っている最中にも読める。

| 名前 | kind |
|---|---|
| `user.trace.start` | control |
| `user.trace.stop` | control |
| `user.trace.get` | control（observes） |
| `user.trace.stats` | control（observes） |

## 4. 試験

すべて偽の機器。ターゲット側の定数（ポート・番号）とホストの解読の一致は、ヘッダを読む試験で見張る。
freertos_demo は新しいフックで作り直し、`fprobe_trace_record` の命令数・フックの記号の存在を ELF で確かめる。
実機の手順は `gateware/tests/hil/test_trace_hooks_hw.py`（本流）。
