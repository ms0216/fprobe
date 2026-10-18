# U45 RTOS の対応の拡大（続き）: Eclipse ThreadX

RTX5（`2026-10-18-u45-rtx5-design.md`）に続いて、U45 の **ThreadX** を足す。ThreadX は CUBE-08・VSC-09・TZ-14・
SEG-65・TRX-03 が求める（STM32Cube の標準の RTOS の一つ、TraceX は既に `tracefmt/tracex.py` が読む）。

## 1. どれを足すか

| RTOS | 試せる本物 | 判断 |
|---|---|---|
| ThreadX | GitHub の eclipse-threadx/threadx（MIT）。v6.5.2.202603 のヘッダを取り込む（出典は `host/firmware/threadx_shape/threadx/SOURCE.md`） | **足す** |
| µC/OS-III | weston-embedded/uC-OS3 は Apache-2.0（MIT ではない） | 残す。取り込む条件（MIT）に合わない。Apache-2.0 で良ければ同じやり方で足せる |
| NuttX・Zephyr の完全化 | 前と同じ | 残す |

## 2. 口（既存の層に合わせる）

`fprobe.rtos.threadx.ThreadX`（`KernelAwareness`）を `open_rtos` の表に足す（`detect` は `"threadx"`）。API・MCP の
`rtos.*` は RTOS に依らないので、そのまま ThreadX で動く（summary の種類の列を更新）。

- 検出: 大域変数 `_tx_thread_created_ptr`・`_tx_thread_current_ptr` と型 `TX_THREAD`（`tx_thread_created_next`）。
- スレッドの一覧: 作られた環 `_tx_thread_created_ptr`（`tx_thread_created_next`）。id（`TX_THREAD_ID`）が違うものは警告して
  外し、環が元に戻らない・数（`_tx_thread_created_count`）が合わないときは警告する。
- 実行中: `_tx_thread_current_ptr`（NULL ならスケジューラが待っている。実行中のスレッドは無い）で状態が `TX_READY`。
- 状態: `TX_READY` → running / ready、`TX_SUSPENDED` → suspended、`TX_COMPLETED`・`TX_TERMINATED` → deleted、
  それ以外の待ち → blocked。待ちの言葉: `TX_SLEEP` delay、キュー（空なら receive、そうでなければ send。ThreadX のキューの
  待ち手は片方だけ）、セマフォ・ミューテックス・ブロック / バイトプール receive、イベントフラグ event、
  IO_DRIVER・FILE・TCP_IP は pend。
- 待っているもの: `tx_thread_suspend_control_block`（先頭の id で種類を見分ける）。名前は `*_name` → シンボル → 番地。
  ミューテックスなら持ち主（`tx_mutex_owner`）。
- 残りのティック: スレッドの `tx_thread_timer` を時間の輪（`_tx_timer_list`・`_tx_timer_current_ptr`）で、
  `tx_timer_info_get.c` と同じく計算する（枠の差 + 1、32 ティックより先は `remaining_ticks − 枠の数` を足す）。
- 優先度: 小さい方が高い（`priority_order = -1`）。元の優先度は `tx_thread_user_priority`。
- スタック: `tx_thread_stack_start`〜`tx_thread_stack_end`（最後のバイト）。0xEF（`TX_STACK_FILL`。乱数の塗りなら
  `tx_thread_stack_fill_value` の下位バイト）で塗る。底が塗りのままでなければあふれ（notes）。
- 保存された文脈: `tx_thread_stack_ptr` から `cortexm.read_exc_return_first_context`。port の積み方は
  - `"armv7m"`（M3・M4・M7・M33 等のメインライン）: EXC_RETURN、[s16〜s31]、r4〜r11、ハードのフレーム
  - `"armv6m"`（M0・M0+・M23 のベースライン）: EXC_RETURN、r8〜r11、r4〜r7、ハードのフレーム
  を ELF の属性 `Tag_CPU_arch`（`cortexm.arm_baseline`）で決める（`port=` で上書きできる。`rtos_info` の `port` に出る）。
  巻き戻しは `_tx_thread_shell_entry`（新しいスレッドの最初の PC。本物の呼び出し元）まで出し、その先は止まる。
- オブジェクト（`fprobe.rtos.threadx_objects`）: 種類ごとの作られた環（`_tx_<種類>_created_ptr`）から全部を見つける。
  mutex（持ち主・継承・ロックの数）・semaphore（count）・queue（msg_size・count・max_msgs・free。大きさは語の数 × 4）・
  event_flags（flags、遅れて消すビット）・block_pool（ブロックの数）・byte_pool（大きさ・空き・断片の数）・
  timer（動いているか・周期・残り・呼ぶ関数と引数）。待ち手は `*_suspension_list` の環（イベントフラグは待つビットと
  all / clear、バイトプールは確保したいバイト数）。変数の名前は aliases（カーネルの `_tx_*` は外す）。
- サンプラ（時間軸・逆転の推移）: いまのスレッドの語 `_tx_thread_current_ptr`（0 はアイドル）、保存された SP
  `tx_thread_stack_ptr`、ミューテックスの `tx_mutex_owner`・`tx_mutex_suspension_list`（差 0、空は 0）。
- スケジューラ: `_tx_thread_system_state` が初期化中なら not_started、`_tx_thread_preempt_disable` なら suspended、
  ほかは running。版は `_tx_version_id`、ティックは `_tx_timer_system_clock`。
- ヒープ: ThreadX にシステムのヒープは無い（バイトプールがその役。オブジェクトとして出す）。`heap()` は None。

## 3. 試験

`host/firmware/threadx_shape/`: 本物のヘッダ（`threadx/`）の構造体とカーネルの変数で、止めたときの様子を .data に置いた
ELF（arm-zephyr-eabi-gcc、Cortex-M4F。ThreadX のカーネルそのものではない）。`host/tests/test_rtos_threadx.py` が、
11 のスレッド（実行中・準備完了・セマフォの時間切れ付きの待ち・スリープ・ミューテックス待ち・停止・満杯のキューへの送り・
空のキューからの受け取り・イベントフラグ・ブロックプール・終わり）、残りのティック（輪の折り返しと 32 ティックより先）、
スタックの塗りと壊れた底、両方の port の積み方と巻き戻し、オブジェクトと優先度の逆転、壊れた環と違う id を確かめる。
