# Eclipse ThreadX のヘッダ（取り込んだ部分）の出典

- 名前: Eclipse ThreadX（旧 Azure RTOS ThreadX）
- 版: ThreadX 6.5.2.202603（`THREADX_MAJOR_VERSION` 6・`MINOR` 5・`PATCH` 2・`BUILD` 202603）
- 出典: https://github.com/eclipse-threadx/threadx のタグ `v6.5.2.202603_rel`
  （コミット `93387b0a60382a29579bbad5045c68328e3751df`）。raw.githubusercontent.com から個々のファイルを取得した
  （スクリプト等は取得も実行もしていない）
- 取得日: 2026-10-18
- 著作権: Copyright (c) 2024 Microsoft Corporation / Copyright (c) 2026-present Eclipse ThreadX contributors（各ファイルの先頭）
- ライセンス: MIT License（SPDX-License-Identifier: MIT。各ファイルの先頭）。全文は同じディレクトリの `LICENSE.txt`
  （リポジトリの LICENSE.txt をそのまま写したもの）
- 変更: なし（中身はそのまま。ファイル名は元のまま、ディレクトリだけを平らにした）

| ファイル | 元の場所 | SHA-256（取得したもの） | 用途 |
|---|---|---|---|
| `tx_api.h` | `common/inc/tx_api.h` | `0d5dce7ce5cc80208b31dcd403817f447686c28dfcce7fd049108fb84da61657` | 制御ブロック（TX_THREAD・TX_MUTEX・TX_TIMER 等）と状態・id の定数。試験用の ELF の DWARF の形の元 |
| `tx_port.h` | `ports/cortex_m4/gnu/inc/tx_port.h` | `9fbcdbfb384b005cedad0f044aefabcca8b62196557c847474ebb8de5ec47381` | Cortex-M4 の GNU の port（型の大きさ・TX_THREAD_EXTENSION_*・TX_MAX_PRIORITIES） |
| `tx_thread.h` | `common/inc/tx_thread.h` | `17e77de4777ba5361127c75ca2de0f4caf94247882bd11291f914cc0a98a7132` | カーネルの変数（_tx_thread_created_ptr・_tx_thread_current_ptr 等）の宣言と TX_THREAD_ID |
| `tx_timer.h` | `common/inc/tx_timer.h` | `4e05bc546647f1db9d02afff265a3b409f15f63ada9c5b28d172c5ae96d32551` | 時間の輪（_tx_timer_list・_tx_timer_current_ptr）・_tx_timer_system_clock と TX_TIMER_ID |
| `tx_mutex.h` | `common/inc/tx_mutex.h` | `d0e0b6c17d30d82f3ecbd5618f51ca9d47f7e574cc98e229dc99070678d7eb7e` | 作られた環の変数と TX_MUTEX_ID |
| `tx_semaphore.h` | `common/inc/tx_semaphore.h` | `f3e35c0758812ff047004ab3fc03d4f5e5560fb6d5d525ef2665318240c6b2fe` | 同（セマフォ） |
| `tx_queue.h` | `common/inc/tx_queue.h` | `bdec805e25afd18b9eadf4178a9c7d2803c308a9505cfb746a3667db6774163a` | 同（キュー） |
| `tx_event_flags.h` | `common/inc/tx_event_flags.h` | `cc3b935d030b2657dc745c1c60914fd179b64e5d18da195fece7a0871614aa0a` | 同（イベントフラグ） |
| `tx_block_pool.h` | `common/inc/tx_block_pool.h` | `f104895488e144b4450a4ead4669c46aaec601d66c4f24ee2bc884dd3a309bf0` | 同（ブロックプール） |
| `tx_byte_pool.h` | `common/inc/tx_byte_pool.h` | `a484d23c38952daaf628970b0f4fa9605f8c4c2961974c2c5a118e8e255d8d67` | 同（バイトプール） |
| `tx_initialize.h` | `common/inc/tx_initialize.h` | `f651f319e638e5e6cb5dcb691c9501dd3cda1cb8fac238d9bd15ad2165ccc649` | _tx_thread_system_state の値（TX_INITIALIZE_IN_PROGRESS 等） |
| `LICENSE.txt` | `LICENSE.txt` | `4dc83ba591696918e00aaceff4f3aa6766fc1cf44f212abdee2786f7a2faf1d8` | MIT License の全文 |

リポジトリでは改行が LF に正規化されることがある（ハッシュは取得したままのファイルのもの）。

ThreadX のカーネルの実装（common/src・ports/*/src）は含まない。読み手（`host/fprobe/rtos/threadx.py`）が従う
カーネルの振る舞いは、同じコミットの次のファイルを読んで確かめた（取り込んではいない）:

- `ports/cortex_m{0,4,7,23,33}/gnu/src/tx_thread_schedule.S`: PendSV が積む文脈（メインラインは EXC_RETURN・
  [s16〜s31]・r4〜r11、ベースラインは EXC_RETURN・r8〜r11・r4〜r7）
- `ports/cortex_m4/gnu/src/tx_thread_stack_build.S`: 新しいスレッドの最初の文脈（PC は _tx_thread_shell_entry）
- `common/src/tx_thread_create.c`: スタックの 0xEF の塗りと tx_thread_stack_end（最後のバイト）
- `common/src/tx_timer_info_get.c`・`tx_timer_system_activate.c`: 時間の輪からの残りのティックの計算
- `common/src/tx_queue_send.c`・`tx_event_flags_get.c`: 待ち手の tx_thread_suspend_info・suspend_option の使い方

試験用の ELF（`../threadx_shape.elf`）は、このヘッダの構造体と変数で「止めたときの様子」を置いたもの（`../threadx_shape.c`）。
