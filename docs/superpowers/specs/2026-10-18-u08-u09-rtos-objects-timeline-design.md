# U08・U09: RTOS のカーネルオブジェクトの一覧と、時間軸の解析の拡張（設計）

日付: 2026-10-18。台帳: `docs/superpowers/plans/2026-10-18-backlog.md` の U08・U09。
競合の機能: U08 = SEG-53, IAR-33, CUBE-08, MCUX-13, E2S-05, CCS-06, VSC-09, ZEP-04。
U09 = SEG-66, TZ-02, TZ-06, TZ-07, TZ-08, TZ-09, TRX-03, TRX-05, FRT-02, CTX-04。
前提: F6（`docs/superpowers/specs/2026-10-17-f6-rtos-design.md`）の読み手（`fprobe.rtos`）と時間軸。

## 1. U08: カーネルオブジェクトの一覧（`rtos.objects`）

### 1.1 見つけ方（名前の無いものも出す）

FreeRTOS はオブジェクトの一覧を持たない（キューの登録簿は任意で、タイマ・イベントグループ等は載らない）。
競合（CubeIDE 等）は登録簿に無いものを出せない。fprobe は次を合わせ、同じ番地は 1 つにまとめる（名前の別名は `aliases`）。

| 出どころ (`source`) | 中身 |
|---|---|
| `registry` | FreeRTOS の `xQueueRegistry`（名前付きのキュー・セマフォ・ミューテックス） |
| `variable` | 大域・ファイルの static 変数（構造体・配列の中も、深さ 4・要素 64 まで）のうち、型がオブジェクトの構造体か、その指す先（`QueueHandle_t` 等のハンドル）のもの。名前は変数の式（`app.queues[2]`） |
| `kernel` | カーネルが持つリスト: FreeRTOS のタイマのリスト（動いているタイマ）、タイマのキュー |
| `waiter` | タスクが待っているもの（TCB のイベントリストの入れ物・Zephyr の `pended_on`）。名前が無ければ `0x...` |

名前の優先: 登録簿 → オブジェクト自身の名前（FreeRTOS のタイマの `pcTimerName`）→ 変数 → 番地。

### 1.2 出すもの（`KernelObject.to_dict()`）

共通: `handle`・`kind`・`name`・`source`・`aliases`・`waiters`（タスク名・番地・優先度・待ちの向き）・`notes`。
種類ごとの `fields`:

| kind | fields |
|---|---|
| queue | length・item_size・messages・free・locked（rx/tx のロック中）・number |
| binary_semaphore / counting_semaphore | count・max |
| mutex / recursive_mutex | available・holder（タスク名・番地・優先度・元の優先度・継承中か）・recursion |
| timer | period_ticks・auto_reload・active・expiry_tick・remaining_ticks・callback（関数名）・id |
| event_group | bits・waiters の bits / all / clear_on_exit |
| stream_buffer / message_buffer | capacity・used・free・trigger_level・next_message（message のみ）・waiting receiver / sender |
| Zephyr k_sem / k_mutex / k_msgq / k_queue / k_fifo / k_lifo / k_stack / k_event / k_timer / k_heap | 構造体にあるメンバーだけ（版・設定の違いは DWARF の有無で吸収） |

ヒープ（`heap`）: FreeRTOS heap_1/2/4/5 の残量・最小残量・総量・確保と解放の回数・空きブロックの数と最大（heap_2/4/5）。
Zephyr は `_system_heap`（k_malloc）の `z_heap` の統計（CONFIG_SYS_HEAP_RUNTIME_STATS）。詳しい走査と壊れの検出は U27。

### 1.3 止めて読む・走行中に読む

既定は止まっているときだけ（リストが一貫する）。`live=true` で走行中にも読む（Cortex-M の AHB-AP は止めずに読める）。
走行中の読みは一貫しない: 結果に `consistent: false` と注意を付け、リストの巡回の壊れ（巡回・数の超過）は警告にして止める。
`rtos.tasks` にも同じ `live` を足す（E2S-05 の実時間のスタック表示）。

### 1.4 取り合いの解析（止めた瞬間）

タスクとオブジェクトから「待ち」のグラフ（タスク → 待つミューテックス → 持ち主）を作る。
- **優先度の逆転**: 持ち主の元の優先度 < 待つタスクの優先度。継承されているか（持ち主の優先度が上がっているか）と、
  その間に実行可能・実行中の中間の優先度のタスク（逆転を伸ばしている候補）を出す。
- **デッドロック**: 待ちのグラフの閉路。
優先度の向きは RTOS で違う（FreeRTOS は大きい方が高い、Zephyr は小さい方が高い）: `KernelAwareness.priority_order`。

## 2. U09: 時間軸の解析の拡張

計算は `fprobe.rtos.analysis`（入出力なし）。Probe は `rtos.timeline.*` の道具で呼ぶ。

### 2.1 窓ごとの CPU 負荷の推移（SEG-66・TZ-02・FRT-02・CTX-04）
`load_series(timeline, window)`: 窓ごとに、情報のある時間（抜けを除く）に対するタスクごと・割り込み・アイドル以外（負荷）の割合。
アイドルのタスク（FreeRTOS `IDLE`、Zephyr `idle`）は負荷に入れない。最大の窓（いつ）も出す。

### 2.2 ジョブ（TZ-07）
ジョブ = タスクが待ちから起きて、また待つまで。区間の終わりが「待った」のか「横取りされた」のかは、
- U10 の出来事（ready / block）があればそれで決める（正確。応答時間 = 終わり − ready）。
- 無ければ次に動いたタスクの優先度で決める（固定優先度のスケジューラでは、より高い優先度へ移った = 横取り、
  低い方へ移った = 待った。同じ優先度は区切り（時分割か譲りか分からないので `uncertain`）。
  優先度の継承で動的に変わった間は誤ることがある（注意に出す）。応答時間は分からず、「実行の始め → 終わり」を下限として出す。
ジョブごとの実行時間（横取り・割り込みを除く）・経過時間・横取りの回数・周期（始めの間隔）と、タスクごとの分布と最悪値（いつ）。

### 2.3 横取りを除いた区間（TZ-08）
記録の 2 点（`timing.measure` と同じ条件）の各組について、そのタスクが実際に CPU を使った時間（他のタスク・割り込みを除く）。
経過時間と自分の時間の両方の分布。

### 2.4 タスクのスタックとヒープの推移（TZ-06）
DAP サンプラで各タスクの「保存された SP」（FreeRTOS `pxTopOfStack`・Zephyr `callee_saved.psp`）とヒープの残量の語を読む
（`rtos.timeline.start(memory=true)`）。切り替えのたびに保存される値なので、待ったとき・横取りされたときの深さの推移。
方法 sampled はいまのタスクの語と同じサンプラに載せる（1 巡回が長くなる）。方法 itm はサンプラが空いているので
`memory_rate_hz` で読む。サンプラは 8 か所まで: 足りなければ `tasks` で絞る。

### 2.5 割り込みとタスクの合成（TRX-03・TZ-02）
方法 itm で `exceptions=true` なら DWT の例外トレース（EXCTRCENA）も同じ SWO に流し、同じデバイスの時刻で
割り込みの区間を作る。タスクの区間から割り込みの時間を引き、スレッド・割り込み・アイドルの割合（実行のプロファイル）、
窓ごとの負荷の割り込みの分、ジョブの実行時間（割り込みを除く）に使う。方法 sampled では割り込みは見えない（明示する）。

### 2.6 優先度の逆転（TRX-05・TZ-09）
DAP サンプラでミューテックスの「持ち主」と「待ちの先頭（最も優先度の高い待ち手）」の語を読む
（`rtos.timeline.start(mutexes=[...])`）。待ちのリストは優先度順なので、先頭の項目の番地からタスクが分かる（組み込み無し）。
持ち主より高い待ち手がいる間を逆転の区間とし、その間に動いた中間の優先度のタスクの時間（際限の無い逆転の印）を出す。
ミューテックスを読まなかった時間軸には `available: false` と理由を返す（推測はしない）。U10 の出来事でも同じ計算を使う
（入力は `MutexState` の列）。

## 3. API

| 名前 | kind | 内容 |
|---|---|---|
| `rtos.objects` | control（observes） | 1 節。kinds で絞る。live |
| `rtos.tasks` | control（observes） | live を足す |
| `rtos.timeline.start` | control | memory・memory_rate_hz・tasks・mutexes・exceptions を足す |
| `rtos.timeline.load` | control（observes） | 2.1 |
| `rtos.timeline.jobs` | control（observes） | 2.2 |
| `rtos.timeline.interval` | control（observes） | 2.3（記録が要る） |
| `rtos.timeline.memory` | control（observes） | 2.4 |
| `rtos.timeline.profile` | control（observes） | 2.5 |
| `rtos.timeline.inversions` | control（observes） | 2.6 |

時間軸の解析の道具が control なのは、一覧に無い TCB の名前を動作中に読むことがあるため（`rtos.timeline.get` と同じ）。

## 4. 試験用ファーム

`freertos_demo` にタイマ（自動・一度だけ）・イベントグループ・ストリーム / メッセージバッファ・数えるセマフォを足し、
worker が log_mutex を約 6 ms 持つ間に ctrl（優先度 3）が待つ = 継承付きの逆転を 20 ms ごとに起こす。
`zephyr_shape` に k_mutex（逆転・継承無し）・k_msgq・k_timer・k_event・k_fifo・k_heap を足す。
