"""fprobe-mcp: AI エージェント向けの MCP サーバ（stdio）。

1 つの fprobe を開き、ターゲットのデバッグ（pyOCD）・ロジックアナライザ・UART・CSR を道具として出す。
デバッガとロジアナは同じ USB ハンドルを共有する（fprobe.session）ので、「捕捉を仕掛けてから実行・停止し、
波形を見る」が 1 つの会話の中でできる。

    fprobe-mcp [--serial <USB シリアル>]
    fprobe-mcp --port <COM ポート> [--baud <ボーレート>]     # SerialLink（UART のホストリンク）のとき
    fprobe-mcp --eth <IP アドレス>                          # EthLink（UDP のホストリンク）のとき
"""

import argparse
import json
import re
import threading
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from serial import Serial
from serial.tools import list_ports

from . import generated as g
from . import probe_names
from .aux_mcu import Aux
from .collector import Collector, add_capture
from .events import EventLog, resolve
from .export import write_trace
from .la import DeepAnalyzer, LogicAnalyzer, Transitions, Waveform, channel_names, write_vcd
from .debug import Debugger, Stop
from .frame_link import FrameLink
from .probe_analysis import AnalysisTools
from .probe_breakpoints import BreakpointActions
from .probe_connect import ConnectTools
from .probe_cycles import CyclesTools
from .probe_decode import DecodeTools
from .probe_design import DesignTools
from .probe_elf_files import ElfFileTools
from .probe_elf_watch import ElfWatchTools
from .probe_etm import EtmTools
from .probe_execution import ExecutionTools
from .probe_flash import FlashTools
from .probe_flash_bp import FlashBreakpointTools
from .probe_formats import FormatTools
from .probe_gdb import GdbTools
from .probe_gdb_trace import GdbTraceTools
from .probe_heap import HeapTools
from .probe_hooks import HookTools
from .probe_inspect import SOURCE_LIMIT, InspectTools
from .probe_longrec import LongRecordingTools
from .probe_lowlevel import LowLevelTools
from .probe_monitor import MonitorModeTools
from .probe_multicore import MulticoreTools
from .probe_observe import ObserveTools
from .probe_observe import ByteLog, ObserveTools
from .probe_profile import ProfileTools
from .probe_profile_export import ProfileExportTools
from .probe_rtos import RtosTools
from .probe_rtos_analysis import RtosAnalysisTools
from .probe_rules import RulesTools
from .probe_safety import SafetyTools
from .probe_snapshot import SnapshotTools
from .probe_streams import StreamTools
from .probe_stack import StackTools
from .probe_swo import SwoTools
from .probe_terminal import TerminalTools
from .probe_ui_state import UiStateTools
from .probe_testpoint import TestpointTools
from .probe_timeline import TimelineTools
from .probe_user import UserTools
from .probe_watch import WatchTools
from .probe_window import WindowTools
from .probe_xtrig_out import XtrigOutputTools
from .probe_gpio import GpioTools
from .sampler import Sampler
from .serial_link import DEFAULT_BAUD
from .recording import Recorder, Recording
from .session import SWO_MODES, Session
from .uart_flow import UartFlow
from .session import swo_start as swo_start_mode
from .bitstreams import auto_switch_enabled, design_for, protocol_for_target
from .multidrop import resolve_target
from .riscv_target import open_riscv, riscv_part
from .arm_a_target import arm_a_part, open_arm_a
from .symbols import Location, Symbols, is_line_location, is_network_path
from .xtrig import CrossTrigger, Event, Pattern, Source, Stage, TriggerSequencer

__all__ = ["Probe", "SOURCE_LIMIT", "build_server", "main"]

INSTRUCTIONS = """fprobe（FPGA デバッグプローブ）を操作する。
- まず `probe_info` でケイパビリティ（ロジアナのチャンネル数・深さ等）を確認する。
- ターゲットの操作は `target_connect` の後。target は pyOCD のターゲット名（例: stm32f401xe, cortex_m）。
  "auto" なら部品を判別して選ぶ。つながらないときは未接続で `target_diagnose`（電圧 → SWD / JTAG → DP → AP →
  部品 → 保護を順に調べ、verdict と suggested_connect）。
- Primer の構成（ビットストリーム）は 3 つ: 標準（SWD・DAP サンプラ・故障注入）、JTAG（JTAG と SWD。サンプラ・故障注入
  なし）、fprobe_la（SWD とロジアナの専用の入力 6 本。サンプラ・故障注入なし。明示の書き替えで選び、SWD の接続では
  標準に戻さない）。`target_connect` は要る線の構成でなければ自動で書き替えてからつなぐ（数十秒。結果の design_switch。
  ロジアナ・記録・流れの状態は消える）。今の構成は `probe_design_get`、明示の書き替えは `probe_design_switch`。
- 低レベル: DP / AP の直接の読み書きは `target_dp_read` / `target_dp_write` / `target_ap_read` / `target_ap_write`、
  JTAG は `jtag_scan`（IDCODE・IR 長）→ `jtag_shift`（IR / DR）、生のビット列は `jtag_clock`、ピンは
  `probe_pins_read` / `probe_pins_write`、メモリの速度は `target_speed_test`。線を動かす道具は未接続のときだけ。
- 接続の手順の上書き: `target_connect` の script（pyOCD のユーザスクリプト。内蔵は `target_script_list`、検査は
  `target_script_check`）。STM32 のオプションバイトは `target_option_bytes_read` / `target_option_bytes_write`、保護の解除は
  `target_unlock`（どちらも全消去を伴う変更は confirm_mass_erase が要る。利用者の明示の同意なしに付けない）。
- `la_capture` はトリガ条件を指定して捕捉を仕掛け、完了まで待って要約を返す。ターゲット操作と同時に使うときは
  `la_arm` → ターゲット操作 → `la_collect` の順に呼ぶ。
- ロジアナのチャンネルの並びは `probe_info` の capabilities.la_layout（kind: pin = デバッグコネクタのピン、internal = 内部の
  信号、input = 専用の入力、none = 無し）。Primer: 0..7 = デバッグコネクタのピン、8 = プローブが SWDIO を駆動中、9 = DAP
  コマンド処理中、10..15 = 専用の入力 in0..in5（J14 の 7〜12 番。デバッグの線と兼用しない入力だけ。構成 fprobe_la に
  だけある: `probe_design_switch(design="fprobe_la")`。サンプラ・故障注入は無くなる）。
- 外部トリガの入力 = 専用の入力の ch: ロジアナのトリガ条件（mask・rise・fall）に使うか、ロジアナを動かさずにターゲット停止・
  出力の端子へつなぐなら `xtrig_set_sequence`（パターン A にその ch の rise / fall、1 段）+ 源 "sequence"。外部トリガの出力は
  `xtrig_output_set`（Primer は ch 4 = 予備の端子が普段の機能を持たない）。
- クロストリガ（ハードウェアで数 µs 以内）: `xtrig_route` で源（la_trigger / dap_match / host）を動作（ロジアナの
  トリガ / 割り込み書き込み = 既定はターゲット停止）につなぐ。例: 「波形の条件でターゲットを止める」=
  to_inject=["la_trigger"]、「ホストがこの番地に書いたら波形を取る」= `xtrig_set_match` + to_la=["dap_match"]。
  順序・回数・時間の条件（「A の後 10 µs 以内に B」「3 回目」）は `xtrig_set_sequence` + 源 "sequence"。
- 出力の端子を動かす（オシロへのトリガ・割り込みの端子・ボタンのまね）: `xtrig_output_set`（channel = 端子のロジアナの
  ch、mode pulse / hold / while、条件 = pattern {mask, value, any}（ch 0〜7 の並び）か sources）→ `xtrig_output_enable` /
  `xtrig_output_disable`、端子を UART・SWO に戻すのは `xtrig_output_release`。使える端子・プルアップ・同時に動かせる数は
  `xtrig_output_status`。
- 拡張ヘッダの端子を直接動かす（未接続のときだけ）: `gpio_set` / `gpio_read` / `gpio_sequence`（値の列を書いて読む）、
  戻すのは `gpio_release`。SPI の相手（SPI Flash・センサー）は `spi_transfer`（端子 = ロジアナの ch、mode・語長。
  16 進の data を送り read_length 語を読む）。SPI Flash の消去・書き込みは Python のプラグイン `fprobe.plugins.spiflash`。
- 長時間の波形（トリガなし）は `la_stream` で VCD に流し続ける。
- Flash: `target_flash`（書き込み・既定で検証・段ごとの速度）、`target_flash_erase`（range / chip / mass）、
  `target_flash_verify`（ファイルとの比較）、`target_flash_read`（読んで保存）、`target_flash_regions`（セクタの並び）、
  外付け Flash は `target_flash_algo_add`（.FLM）、速度は `target_flash_benchmark`。デバイスの定義が無ければ
  `target_pack_find` → `target_pack_install` → `target_connect`（返った targets の名前）。
- ソースの言葉で調べる: `target_flash`（ELF）か `debug_elf_load` で ELF を読むと、場所を関数名・変数名・"ファイル:行" で
  指定できる。`debug_breakpoint_set` / `debug_watchpoint_set` → `debug_run_until_halt`（止まった理由と関数名・行が
  返る）→ `debug_backtrace` / `debug_evaluate`。RAM で動くプログラムは `target_program_ram` で読み込む（Flash は書かない）。
  試しに使う ELF が無ければ `debug_elf_load_demo`（同梱のデモ）。
  読み込んだ ELF が再ビルドされると自動で読み込み直し、道具の結果の notices で知らせる（target_image = stale なら
  `target_flash` で書き直す。様子は `debug_elf_status`、設定は `debug_elf_watch`）。
  ブレークポイントには condition（C の式）・ignore_count・log（止まらずに式の値を `debug_breakpoint_log` に残す）を付けられる。
  ハードの比較器が尽きると Flash の命令を BKPT に書き換えて置く（数の制限なし。書くのは再開の直前だけ、切断で戻す）。
  様子と書き換えの回数は `debug_breakpoint_flash`、Flash を元の命令に戻すのは `debug_breakpoint_flash_restore`。
  故障（HardFault 等）で止まったら `target_fault` で原因・故障した行・その時のレジスタ（run_until_halt にも要約が付く）。
  故障の入口で止めるには `target_vector_catch`（例 ["hard_fault", "bus_fault"]。読むだけは `target_vector_catch_get`）。
  リセットの種類は `target_reset` の kind（system / core / hardware = nRESET の線）。
- 止めると壊れるターゲット: 止める・ブレークポイント・ステップ・`debug_call` の前に `debug_halt_risk`（ELF の無線・USB・
  ウォッチドッグ・RTOS の手がかり）。risky なら人に知らせ、止めずに調べる道具かモニタ方式を先に。走っているのに
  致命的エラーで回り続けている（ロックアップ・arch_system_halt 等）と `target_status` に fatal が付く（止めずに見張る）。
- 止めずにデバッグする（モニタ方式。モーター制御・通信の割り込みを止められないとき）: ファームにモニタ
  （host/firmware/monitor）を組み込み、`target_debug_mode_set`（mode="monitor"、priority = DebugMon の優先度）。
  以後も停止・ステップ・ブレークポイント・レジスタ・呼び出し履歴は同じ道具で、止まっている間も DebugMon より優先度の
  高い割り込みは動く（`target_debug_mode_get` の keeps_running）。その高い優先度の処理の中にはブレークポイントを
  置かない（HardFault になる）。Flash の書き込み・ベクタキャッチ・止めてのリセットは mode="halt" に戻してから。
- 行単位のステップ: `debug_step`（mode into / over / out / until。until は location まで。止まった行と、out なら戻り値）。
  命令単位は `target_step`。止まったまま関数を試すなら `debug_call`（function と arguments。数・C の式・{"string": ...}。
  戻り値を返し、レジスタとスタックは元に戻る）。
- 止まったら: `debug_backtrace`（variables=true で各段の引数とローカル変数も）、`debug_evaluate`（C の式。例
  "head->samples[1]"、frame で呼び出し元の変数）、`debug_frame_variables`、書くのは `debug_write_expression`、
  型は `debug_type_info`。走らせたまま式を読むのは `debug_evaluate` の live=true（大域・静的。止めない）、ターゲット
  なしで式と型を調べるのは `debug_expression_check`（ELF だけ）。命令は `debug_disasm_text`（ソースの行を挟む。省略時は PC の前後）。
- 周辺レジスタ: `target_peripheral_read`（"RCC.CR" か "GPIOA"。フィールドと列挙の名前に解読。SVD はターゲット名から
  自動、無ければ `target_svd_load`）、`target_peripheral_write`（fields で名前の値）。メモリは `target_memory_view`
  （compare で前回の読みとの差）。
- 止めずに見る: RTT は `rtt_start` → `rtt_read`（前回の続き。timeout_s で待つ）/ `rtt_write`。ターゲットの線を
  空けたいときは `rtt_start` の wake="sampler"（FPGA が WrOff を見張り、変化したときだけ読む）。速さは `rtt_measure`。
  ほかの道具（telnet・端末・解析ソフト）にも同時に読ませるには `stream_serve`（rtt0〜 / swo / uart / semihosting を
  127.0.0.1 の TCP で。書き込みも）、ファイルへは `stream_log_start`。様子は `stream_list`。SWO は `swo_start` →
  `swo_text`（printf の行）/ `swo_decode`（例外トレース・PC サンプル）。セミホスティング（BKPT 0xAB）は自動で処理され、
  出力は `rtt_read`（channel="semihosting"）、終了は `debug_run_until_halt` の reason="exit" と exit_code。
  人の画面の端末と同じものを読むなら `terminal_sources` → `terminal_read`（rtt0〜・swo0〜・uart・semihosting を位置
  since から。ほかの読み手の位置を動かさない。times で行の時刻）。UART のボーレートは `uart_configure`。
  変数は `watch_set`（名前・C の式・番地。FPGA が周期的に読む。止めない・ファームの変更不要）→ `watch_values`（最新値）/
  `watch_series`（値の列）。長い時間の変化は記録中の `recording_find`（stream "sample"）。
  ターゲットが出す値・イベント（ファームの fprobe_user.h）: `user_trace_start`（channels = ITM のポート・RTT のチャンネルの
  型 i32・f32・q16.16・struct、events = 名前付きの start / stop / mark / value）→ `user_trace_get` / `user_trace_stats`
  （区間の統計）→ `user_trace_stop`（記録中なら stream "user"。区間は `timing_measure` でも）。
  `watch_series`（値の列）。長い時間の変化は記録中の `recording_find`（stream "sample"）。ポインタをたどる式
  （"head->pos.x"）はホストが周期ごとに番地を引き直す。条件（値・クロストリガ）で取るなら `watch_trigger`（pre_s で
  プリトリガ）→ `watch_capture`。走行中に書くのは `watch_write`。ファームが配列に貯める値は `watch_buffer_start` →
  `watch_buffer_read`（取りこぼさない）。
- 既存のログ・トレースの形式: ログは `log_start`（defmt・trice・compact・memfault・text。source rtt / swo / uart）→
  `log_read`（記録中なら stream "log"）。文字の中のコードの番地は `log_annotate`。トレースは `trace_start`（systemview・ctf・
  tracealyzer、eventrecorder は RAM を止めずに）→ `trace_stop`（時間軸になり `rtos_timeline_load` 等で解析）、RAM から
  `trace_snapshot`（ポストモーテム）、ファイルから `trace_import` → `trace_events`。Component Viewer は `scvd_load` → `scvd_view`。
- 命令トレース（ETM、ETMv4 = Cortex-M33 等）: `trace_etm_start`（CoreSight の ETM・ファンネル・TPIU を設定）→ 走らせて止める →
  `trace_etm_stop` → 取ったバイト列を `trace_etm_decode`（16 進・ファイル・ターゲットのメモリのリング。ELF と合わせて命令の
  流れを再構成）→ `trace_etm_history`（止まる直前の N 命令 = 事後の命令履歴。データの値は無い）/ `trace_etm_instructions`
  （一覧・関数で絞る）/ `trace_etm_packets`。RP2350（Pico 2）は配線なしでトレース FIFO + DMA の SRAM のリングに溜められる
  （Python のプラグイン fprobe.plugins.rp2350_trace）。
- RTOS（FreeRTOS / Zephyr / RTX5 / ThreadX / µC/OS-III / NuttX。ELF の DWARF から）: `rtos_info` → 止めて `rtos_tasks`（一覧・待ち・スタック）/
  `rtos_objects`（キュー・セマフォ・ミューテックスの持ち主・タイマ・イベントグループ・バッファ・ヒープと、逆転・
  デッドロック。名前の無いものも番地で）/ `rtos_backtrace`（task はタスク名）/ `rtos_read`（タスクの文脈の C の式）。
  `rtos_tasks`・`rtos_objects` は live=true で走行中にも（一貫しない）。止めずにタスクの時間軸:
  `rtos_timeline_start`（method sampled = 組み込み無し・回数は下限、itm = トレースフック・正確。memory / mutexes /
  exceptions で一緒に読む）→ `rtos_timeline_stop`（要約。記録中なら stream "rtos" へ）→ `rtos_timeline_get`（区間）。
  解析: `rtos_timeline_load`（負荷の推移）/ `rtos_timeline_jobs`（ジョブの実行時間・周期・最悪値）/
  `rtos_timeline_interval`（記録の 2 点の間の横取りを除いた時間）/ `rtos_timeline_memory`（スタック・ヒープの推移）/
  `rtos_timeline_profile`（スレッド・割り込み・アイドル）/ `rtos_timeline_inversions`（優先度の逆転の区間）。
  itm でファームのフック（fprobe_trace.h）がカーネルの出来事も出すと: `rtos_timeline_events`（API 呼び出し・待ち・
  ready）/ `rtos_timeline_flow`（Communication Flow）/ `rtos_timeline_blocking`（ブロック時間）/ `rtos_timeline_queues`
  （キュー使用率）/ `rtos_timeline_overhead`（フックの負荷）。jobs は応答時間、inversions は正確な区間になる。
- ヒープ: 止めて `heap_scan`（FreeRTOS heap_4/5・newlib・newlib-nano。使用量・断片化・壊れ = issues、compare で前回からの
  増減）。リークは `heap_trace_start`（malloc / free の入口にブレークポイント。止めずに記録）→ 走らせる →
  `heap_trace_report`（leak_candidates = 呼び出し元ごとの古く増え続ける確保）/ `heap_trace_events` → `heap_trace_stop`。
- メモリ・スタック: `debug_elf_size`（領域ごとの使用率・大きいシンボル。領域は linker_script / map_file か接続中のメモリマップ）、
  `debug_stack_static`（静的な最大。.su と呼び出しグラフ。再帰・関数ポインタは上限不明 = indirect で候補を渡す）。
  ベアメタルの MSP は止めて `target_stack_paint` → 走らせる → `target_stack_usage`（使った最大・あふれ）。
- 性能解析: `profile_start`（method itm は core_clock_hz が要る。pcsr は SWO 不要）→ `profile_stop` → `profile_result`
  （by function / line / instruction。割合は信頼区間つき）/ `profile_exceptions`（割り込みの時間）/ `profile_coverage`
  （近似）。記録の 2 点間の時間のばらつき・外れ値は `timing_measure`（条件は `recording_find` と同じ）。
  区間（t0_s / t1_s、pcsr は記録の時間軸の t0 / t1）に限る集計・関数のグループ（by file / prefix / groups）は
  `profile_aggregate`、CPU 使用率の推移は `profile_timeline`、gprof / KCacheGrind / lcov / gcov / dot へは `profile_export`。
  テストポイントは `testpoint_aggregate`（区間）/ `testpoint_export`（callgrind・lcov の C0 / C1 等）。
- 記録の集計・検索は `recording_query`（読み取り専用の SQL。表 events、body は json_extract で）。人に見せる時間軸は
  `recording_export`（format perfetto → ui.perfetto.dev で開く。t0 / t1・streams で絞る）。調査の仮説・結論は
  `recording_mark`（t を指定すると、開いた記録のその時刻にも置ける。人の画面の Event Log に並ぶ）。
- 規則の検査: `rules_check`（区間の上限・周期・順序・件数・値・状態遷移の規則を JSON / YAML で書き、記録の違反を探す。
  違反は stream "rule" に印。baseline で前の記録と比べる）、状態遷移図は `rules_states`（dot）、要件と対応づけた
  証跡は `rules_report`。故障注入は `inject_run`（台本: いつ = when、何を = write、どうなるべきか = expect）。
- フック: `hooks_add`（停止・リセット・書き込み・接続・再開の前後に、式の評価・記録の印・API の呼び出し。人・GDB の
  操作でも走る）。人が置いたフックもあるので、操作の前に `hooks_list`、結果は `hooks_log`。
- 日単位の長期の記録: `recording_start` に segment_s / max_bytes / decimate_after_s（分割・容量の上限・間引き）→
  `recording_la_stream_start`（ロジアナを止めずに記録へ。構成 fprobe_la ではイベント記録も同じ流れで取り込み続ける。
  ほかの構成ではその間イベント記録は取り込まない）→ 全体の概観は
  `recording_overview`（事前計算の縮図）→ 気になる範囲を `recording_density` / `recording_events` / `recording_query`
  （t0 / t1）で。分割の一覧は `recording_segments`。
- SWO の ITM・DWT の設定（止めない）: `swo_configure`（ports・exception_trace・counters・data_trace = 変数へのアクセスを
  値・PC・時刻付きで記録・value_watch = 値一致で止める/記録。swo_baud で受信も）→ `swo_trace`（アクセス・カウンタ・例外）
  → `swo_unconfigure`。`profile_start`（itm）とは同時に使えない。
- サイクル: `cycles_measure`（2 つの場所の間のサイクル・CPI 等・命令数）、`cycles_clock`（CPU クロックの実測）、
  `cycles_read`（CYCCNT のいまの値）。
- テストポイント（関数の時間・呼び出しの木・遷移・正確なカバレッジ）: `testpoint_instrument`（C のソースに挿入した写しと
  対応表。変わらない関数の ID は保つ）→ 写しをビルドして書く → `testpoint_start`（method gpio_serial / gpio_parallel は
  ロジアナ、capture stream で長時間。itm は SWO、rtt は RTT）→ `testpoint_stop` → `testpoint_report` / `testpoint_calls`
  （時間軸）/ `testpoint_coverage`（通らなかった行 = 試験を足す手がかり）。記録中なら stream "testpoint" にも入る。
- DAP サンプラは 1 つ: いま使っている道具と止める API は `probe_sampler_status`。
- DAP サンプラは 1 つ: `watch_set`・`rtos_timeline_start`（sampled、または memory / mutexes）・`profile_start`（pcsr）は
  同時に使えない。
- 通信の解読: 捕捉の後 `la_decode`（protocol と settings。例 uart は {"channel": 5}、swd は {"swclk": 0, "swdio": 3,
  "drive": 8}）。errors_only で誤りだけ、件数が多ければ offset / limit。解読器は `la_decoders` で一覧（CAN/CAN FD・
  1-Wire・JTAG・I2S・パラレル・Manchester 等）。積み重ね（spiflash・eeprom24・modbus・jtag_arm）は下の設定を下の名前の
  キーに（例 modbus は {"uart": {"channel": 5, "baud": 9600, "parity": "even"}}）。足りない解読器は Python で書いて
  `la_decoder_load`。PulseView・sigrok で見るなら `la_export`（.sr）、他のロジアナの .sr は `la_load` で読み込む。
- 波形の測定: `la_measure`（最後の捕捉。周波数・周期・デューティ・パルス幅の統計、relations で delay・setup_hold）、
  記録の波形は `recording_waveform_measure`（時刻の範囲 t0 / t1）。
- イベント記録: `events_start` → ターゲット操作 → `events_read` で、線上の DAP 転送（ホストの読み書き・割り込み書き込み）を
  時刻付きで得る。ポストされた AP の読みは値の返った転送に対応付け済み。ロジアナの深いバッファと同時に使える。
- 人の画面と同じところを見る・見せる: `ui_state_get`（時間軸の視点 view・カーソル 3 種・開いている段 rows・選んだ
  出来事 event・呼び出し履歴のフレーム frame）、`ui_state_set`（渡したものだけ変える。例: 異常の区間に view と
  cursors を置いて人に示す。人の画面がそこへ移る）。
- 人間が画面から一時停止しているあいだ、操作の道具は再開まで待たされる（読むだけの道具は通る）。
- 人間が IDE（GDB。デーモンの --gdb）で同じターゲットを同時にデバッグしていることがある: ターゲットは GDB からも
  止まる・動く。`debug_breakpoint_list` の location が "gdb:0x…" のものは GDB が置いたもの（外さない）。
- スナップショット: 止めて `snapshot_save`（レジスタ・RAM・副作用の無い周辺・RTOS・故障・ELF を 1 ファイルに）。
  未接続で `snapshot_open` すると `debug_backtrace`・`debug_evaluate`・`rtos_tasks`・`target_peripheral_read`・
  `target_fault` がそのスナップショットを読む（書き込み・実行は不可）。`snapshot_restore` で RAM とレジスタを書き戻して
  再開、`snapshot_export_core` で GDB の読める ELF のコアファイルに。"""


def _transition_summary(tr) -> dict:
    period_ns = tr.sample_period_s * 1e9
    out = {}
    for ch in range(tr.channels):
        edges = tr.edges(ch)
        initial = tr.values[0] >> ch & 1 if len(tr.values) else 0
        if not edges and not initial:
            continue
        out[ch] = {"initial": initial, "edges": len(edges),
                   "first_edges_ns": [round((i - tr.trigger) * period_ns) for i in edges[:16]]}
    return out


def _channel_summary(cap) -> dict:
    period_ns = cap.sample_period_s * 1e9
    out = {}
    for ch in range(cap.channels):
        bits = cap.channel(ch)
        edges = [i for i in range(1, len(bits)) if bits[i] != bits[i - 1]]
        if not edges and not bits[0]:
            continue                                       # 常に 0 のチャンネルは省く
        out[ch] = {
            "initial": bits[0],
            "edges": len(edges),
            # トリガ（0 ns）からの相対時刻。多いときは先頭だけ
            "first_edges_ns": [round((i - cap.trigger) * period_ns) for i in edges[:16]],
        }
    return out


class Probe(InspectTools, ObserveTools, WatchTools, RtosTools, ProfileTools, SwoTools, CyclesTools, WindowTools,
            BreakpointActions, AnalysisTools, GdbTools, GdbTraceTools, FlashTools, TestpointTools, SnapshotTools, ExecutionTools,
            RtosAnalysisTools, RulesTools, HookTools, FlashBreakpointTools, DecodeTools, UserTools,
            LongRecordingTools, HeapTools, LowLevelTools, ConnectTools, ElfWatchTools, StreamTools, FormatTools,
            ProfileExportTools, StackTools, TimelineTools, ElfFileTools, TerminalTools, UiStateTools, DesignTools,
            MonitorModeTools, XtrigOutputTools, GpioTools, SafetyTools, MulticoreTools, EtmTools):

    def __init__(self, serial: str | None = None, port: str | None = None, baud: int = DEFAULT_BAUD,
                 eth: str | None = None, bitstreams: str | None = None):
        self._serial = serial
        self._port, self._baud = port, baud               # port を指定すると SerialLink で開く
        self._eth = eth                                   # IP アドレスを指定すると EthLink で開く
        self._lock = threading.RLock()
        self._session: Session | None = None
        self._debug = None
        self._symbols: Symbols | None = None                # elf_load / program_ram / flash（ELF）で読み込む
        self._dbg: Debugger | None = None
        self._capture_id = 0                                    # 最後の捕捉（画面の波形 la_view 用）
        self._capture: Transitions | None = None
        self._capture_info: dict | None = None
        self._capture_names: dict[int, str] = {}                # 最後の捕捉の ch の既定の名前（書き出し用）
        self._wave: Waveform | None = None                     # 変化点の表は画面が最初に求めたときに作る
        self._recorder: Recorder | None = None                 # 記録中の共通の時間軸の記録（F3）
        self._collector: Collector | None = None
        self._view: Recording | None = None                    # 読む対象の記録（記録中のもの、または開いたもの）
        self._uart: Serial | None = None
        self._la: LogicAnalyzer | None = None
        self._la_period = 0.0
        self._target_type: str | None = None              # target_connect のターゲット名（SVD を探す）
        self._link: dict | None = None                    # つないでいる先（target・frequency_hz・wire。target_status に出す）
        self._swo_baud: int | None = None                 # swo_start の速度（時刻の分解能の見積もりに使う）
        self._swo_mode = "uart"                           # swo_start の方式（uart / manchester）
        self._init_inspect()
        self._init_breakpoints()
        self._init_flash()
        self._init_flash_bp()
        self._init_gdb()
        self._init_gdb_trace()
        self._init_hooks()
        self._init_observe()
        self._init_profile()
        self._init_rtos()
        self._init_snapshot()
        self._init_swo()
        self._init_testpoint()
        self._init_watch()
        self._init_window()
        self._init_breakpoints()
        self._init_gdb()
        self._init_flash()
        self._init_snapshot()
        self._init_user()
        self._init_longrec()
        self._init_heap()
        self._init_lowlevel()
        self._init_elf_watch()
        self._init_streams()
        self._init_terminal()
        self._init_ui_state()
        self._init_formats()
        self._init_stack()
        self._init_design()
        self._init_design(bitstreams)                     # Primer の構成の書き替え（ビットストリームの置き場所）
        self._init_monitor()
        self._init_safety()
        self._init_xtrig_out()
        self._init_etm()

    # --- 共通 ------------------------------------------------------------------------

    @property
    def session(self) -> Session:
        if self._session is None:
            if self._eth:
                self._session = Session.open_eth(self._eth)
            elif self._port:
                self._session = Session.open_serial(self._port, self._baud)
            else:
                self._session = Session.open(self._serial)
            self._stop_stale_sampler()
        return self._session

    def _stop_stale_sampler(self) -> None:
        """開いたばかりの機器で、前の持ち主（落ちた・起動し直したデーモン等）が動かしたままの DAP サンプラを止める（B30）。
        持ち主はプロセスの中にしかいないので、開いた時点で動いているものは誰の物でもなく、線を借り続けて接続を邪魔する。"""
        if getattr(self._session.device.capabilities(), "sampler", False):
            Sampler(self._session.device).stop()

    @property
    def target(self):
        """接続中のターゲット。未接続でスナップショットを開いていれば、その読み手（読むだけ）。"""
        if self._debug is None:
            offline = self._offline_target()
            if offline is not None:
                return offline
            raise NotConnected("ターゲット未接続（target_connect を先に呼ぶ）")
        # Flash を持てば Flash のブレークポイントで包み、モニタ方式ならモニタの包み（fprobe.monitor）で包む
        return self._monitor_wrap(self._flash_bp_wrap(self._debug.target))

    @property
    def dbg(self) -> Debugger:
        if self._dbg is None or self._dbg.target is not self.target:
            self._dbg = Debugger(self.target, self._symbols)
        return self._dbg

    def _where(self, address: int) -> str:
        return str(self._symbols.describe(address)) if self._symbols else f"0x{address:08X}"

    def _place_of(self, address: int) -> dict:
        return self._place(self._symbols.describe(address)) if self._symbols else {"where": f"0x{address:08X}"}

    @staticmethod
    def _place(location: Location) -> dict:
        """止まった場所: 表示用の where と、分かるときだけ function・file・line（画面が文字列を解釈しないため）。"""
        place = {"where": str(location)}
        for key in ("function", "file", "line"):
            if getattr(location, key) is not None:
                place[key] = getattr(location, key)
        return place

    def close(self) -> None:
        self._release_device()
        self._stop_elf_watch()

    def _release_device(self) -> None:
        """機器とターゲットにつながるものを閉じる（USB の開き直し・構成の書き替えでも使う）。ELF の更新の見張りは
        機器と無関係なので止めない（止めると、開き直しの後に ELF を作り直しても読み込み直さなくなった。2026-10-18）。"""
        self._stop_la_record()
        self._close_streams()
        self._close_formats()
        self._stop_observing()
        self._close_testpoint()
        with self._lock:
            self.target_disconnect()
            self._close_lowlevel()
            if self._uart is not None:
                self._uart.close()
                self._uart = None
            if self._session is not None:
                self._session.close()
                self._session = None

    # --- プローブ --------------------------------------------------------------------

    def probe_info(self) -> dict:
        """プローブのケイパビリティ・タイムベースの現在値・ターゲット接続の有無・個体（device = つなぎ方と番地、
        name = 個体名、version = ビットストリームの版: 入力の要約・ビルドの日時・git）。"""
        with self._lock:
            caps = self.session.device.capabilities()
            device = getattr(self.session, "address", None)
            return {"capabilities": {k: v for k, v in vars(caps).items() if not k.startswith("_")},
                    "time": self.session.device.time(), "target_connected": self._debug is not None,
                    "device": device, "name": probe_names.name_of(device),
                    "version": caps.build.describe() if getattr(caps, "build", None) is not None else None}

    def probe_name_set(self, name: str | None = None) -> dict:
        """今の機器（USB のシリアル番号・COM ポート・IP）に個体名を付ける（~/.fprobe/probes.json）。name を省くと名前を
        外す。以後 fprobe-daemon / fprobe-mcp を --name で起動でき、probe_info が名前を返す。"""
        with self._lock:
            device = getattr(self.session, "address", None)
            if not device:
                raise RuntimeError("この機器のつなぎ方が分からない")
            if name:
                probe_names.set_name(name, device)
            else:
                old = probe_names.name_of(device)
                if old is not None:
                    probe_names.remove(old)
            return {"device": device, "name": probe_names.name_of(device)}

    def csr_read(self, address: int, count: int = 1) -> list[int]:
        """CSR を読む（バイトアドレス、32 ビット語 count 個）。レジスタ表は docs/registers.md。"""
        with self._lock:
            return self.session.device.read(address, count)

    def csr_write(self, address: int, words: list[int]) -> None:
        """CSR に 32 ビット語の列を書く。"""
        with self._lock:
            self.session.device.write(address, words)

    def target_voltage(self) -> dict:
        """ターゲットの電源電圧（補助マイコン Pico の ADC、V）。live が False なら Pico が計測値を送っていない。
        over_range が True なら測れる上限を超えている（volts は None。配線の分圧を確認）。"""
        with self._lock:
            r = Aux(self.session.device).read()
            return {"volts": r.target_volts, "over_range": r.over_range, "live": r.live, "samples": r.samples,
                    "errors": r.errors}

    # --- ターゲット ------------------------------------------------------------------

    def target_connect(self, target: str = "cortex_m", frequency_hz: int = 4_000_000,
                       connect_mode: str = "halt", jtag_chain: list[int] | None = None,
                       script: str | None = None, core: int | None = None) -> dict:
        """ターゲットに接続する。既定は SWD。jtag_chain（JTAG チェーンの IR 長、TDO 側から。例: STM32 は [4, 5]）を
        指定すると JTAG。connect_mode: halt / attach / under-reset。target="auto" なら先に部品を判別して
        pyOCD のターゲット名を選ぶ（判別の結果は auto に。JTAG だけのターゲットなら jtag_chain も）。
        script: 接続・リセット等の手順を上書きする pyOCD のユーザスクリプト（"builtin:<名前>" かファイル。script_check）。
        core: コアごとに DP を持つ複数ドロップの部品（RP2040）のコアの番号（target="rp2040" と組む。fprobe.multidrop）。
        RISC-V（target="rp2350_riscv"。fprobe.riscv_target）ではハートの番号（connect_mode は halt / attach）。
        Cortex-A・ARM11（target="bcm2836" / "cortex_a" / "bcm2835" / "arm11"。fprobe.arm_a_target）ではコアの番号
        （JTAG で直接。connect_mode は halt / attach）。
        Primer の構成（probe_design_get）が要る線のものでなければ、書き替えてから接続する（結果の design_switch）。"""
        switches = []
        auto = None
        if target == "auto":
            target, auto = self._resolve_auto_target(frequency_hz, switches)
            frequency_hz = min(frequency_hz, auto["suggested_connect"]["frequency_hz"])
            if jtag_chain is None:
                jtag_chain = auto["suggested_connect"].get("jtag_chain")
        if riscv_part(target) is None and arm_a_part(target) is None:
            target, _, core = resolve_target(target, core)
        switch = self._ensure_design(protocol_for_target(target, jtag_chain))
        if switch is not None:
            switches.append(switch)
        status = self._connect_target(target, frequency_hz, connect_mode, jtag_chain, script, core)
        if auto is not None:
            status["auto"] = {**auto, "target": target}
        if switches:
            status["design_switch"] = switches[-1] if len(switches) == 1 else switches
        return status

    def _resolve_auto_target(self, frequency_hz: int, switches: list) -> tuple[str, dict]:
        """target="auto" の判別。今の構成でつながらず、JTAG が無い構成なら、JTAG の構成に書き替えて判別し直す。"""
        with self._lock:
            self.target_disconnect()
            self._close_snapshot()
            caps = self.session.device.capabilities()
            try:
                return self._auto_target(frequency_hz)
            except RuntimeError:
                if caps.jtag or not self._switchable(caps) or not auto_switch_enabled():
                    raise
        switches.append(self._switch_design(caps.design, design_for("jtag")))
        with self._lock:
            return self._auto_target(frequency_hz)

    def _connect_target(self, target: str, frequency_hz: int, connect_mode: str, jtag_chain: list[int] | None,
                        script: str | None, core: int | None = None) -> dict:
        with self._lock:
            self.target_disconnect()
            self._close_snapshot()                         # 実物に繋ぐ: 開いていたスナップショットは閉じる
            options = {}
            script_info = None
            riscv, arm_a = riscv_part(target), arm_a_part(target)
            if riscv is not None and (script is not None or jtag_chain is not None):
                raise ValueError("RISC-V の接続（pyOCD を使わない）に script・jtag_chain は使えない")
            if arm_a is not None and script is not None:
                raise ValueError("Cortex-A・ARM11 の接続（pyOCD を使わない）に script は使えない")
            if script is not None:
                options["user_script"], script_info = self._connect_script(script)
            self._close_lowlevel()                         # 低レベルの道具が開いた DAP の接続口を pyOCD に渡す
            if riscv is not None:                          # pyOCD に RISC-V は無い: DM を直接使う（fprobe.riscv_target）
                debug = open_riscv(self.session.dap_access(), riscv, core or 0, frequency_hz, connect_mode)
            elif arm_a is not None:                        # pyOCD に Cortex-A・ARM11 は無い: JTAG で直接（fprobe.arm_a_target）
                debug = open_arm_a(self.session.dap_access(), arm_a, core or 0, frequency_hz, connect_mode,
                                   jtag_chain)
            else:
                debug = self.session.debug(target_override=target, frequency=frequency_hz, connect_mode=connect_mode,
                                           jtag_chain=jtag_chain, **options)
                debug.open()
            self._debug = debug
            self._target_type = target
            self._link = {"target": target, "frequency_hz": frequency_hz, "wire": protocol_for_target(target, jtag_chain)}
            if riscv is not None:
                self._link.update(debug.info())            # arch・hart・debug_module
                core = debug.target.dm.hart
            elif arm_a is not None:
                self._link.update(debug.info())            # arch・core・cores・debug_base 等
                core = debug.target.index
            if core is not None:
                self._link["core"] = core                  # 複数ドロップの部品のコア・RISC-V のハート（target_status）
            recovered = self._flash_bp_attach()           # 前のプロセスが異常終了して残した Flash の BKPT を戻す
            status = self.target_status()
            if recovered is not None:
                status["flash_breakpoints_recovered"] = recovered
            if script_info is not None:
                status["script"] = script_info
            return status

    def target_disconnect(self) -> None:
        """ターゲットから切断する。"""
        with self._lock:
            self._rtt = None
            self._watch_stops(False)
            if self._debug is not None:
                self._monitor_detach()                     # モニタ方式で止まっていれば再開して離す
                self._flash_bp_detach()                    # Flash に書いた BKPT を戻す
                debug, self._debug = self._debug, None
                self._link = None
                debug.close()

    def target_status(self) -> dict:
        """コアの状態（RUNNING / HALTED 等）と、停止中なら PC。つないでいる先（target・frequency_hz・wire）も。
        走っているのに致命的エラーで回り続けていれば fatal（fprobe.probe_safety）。複数ドロップの部品（RP2040）なら
        core（接続中のコア）と cores（各コアの状態。fprobe.probe_multicore）。"""
        with self._lock:
            state = self.target.get_state().name
            status = {"state": state, **(self._link or {})}
            if state == "HALTED":
                pc = self.target.read_core_register("pc")
                status["pc"] = pc
                status.update(self._place_of(pc))
            if self._monitor is not None:
                status["debug_mode"] = "monitor"              # 止まっているのは DebugMon の中（優先度の高い割り込みは動く）
            fatal = self._fatal_state(state)
            if fatal is not None:
                status["fatal"] = fatal
            return self._multicore_status(status)

    def target_halt(self, core: int | None = None) -> dict:
        """コアを停止する。core: 複数ドロップの部品（RP2040）で接続していないコアを止める（その状態を返す）。"""
        with self._lock:
            if core is not None:
                other = self._other_core(core, "halt")
                if other is not None:
                    return other
            self.target.halt()
            self._watch_stops(False)
            return self.target_status()

    def target_resume(self, core: int | None = None) -> dict:
        """コアの実行を再開する（いまの PC にブレークポイントがあれば 1 命令踏み越えてから。同じ場所で止まり直さない）。
        core: 複数ドロップの部品（RP2040）で接続していないコアを再開する（その状態を返す）。"""
        with self._lock:
            if core is not None:
                other = self._other_core(core, "resume")
                if other is not None:
                    return other
            self.dbg.resume()
            self._watch_stops(True)                  # BKPT 0xAB（セミホスティング）で止まったら背景で処理して再開
            return self.target_status()

    def target_step(self, count: int = 1) -> dict:
        """1 命令ずつ count 回ステップ実行する。"""
        with self._lock:
            self._watch_stops(False)
            for _ in range(count):
                self.target.step()
            return self.target_status()

    def target_reset(self, halt: bool = False, kind: str = "default") -> dict:
        """ターゲットをリセットする（halt=True ならリセット直後で停止）。kind: default / system（SYSRESETREQ。
        周辺も）/ core（VECTRESET。コアだけ、ARMv7-M だけ）/ hardware（nRESET の線）/ emulated。"""
        with self._lock:
            self.dbg.reset(kind, halt)
            self._watch_stops(not halt)
            return {**self.target_status(), "kind": kind}

    def read_registers(self, names: list[str] | None = None) -> dict[str, int]:
        """コアレジスタを読む（省略時 r0..r12, sp, lr, pc, xpsr。RISC-V は pc と x1〜x31 の ABI の名前、
        Cortex-A・ARM11 は xpsr の代わりに cpsr）。"""
        with self._lock:
            names = names or getattr(self.target, "default_register_names", None) \
                or [f"r{i}" for i in range(13)] + ["sp", "lr", "pc", "xpsr"]
            return dict(zip(names, self.target.read_core_registers_raw(names)))

    def write_register(self, name: str, value: int) -> None:
        """コアレジスタに書く（停止中）。"""
        with self._lock:
            self.target.write_core_register_raw(name, value)
            self.target.flush()

    def read_memory(self, address: int, length: int) -> str:
        """ターゲットのメモリを読み、16 進文字列（アドレス順のバイト）で返す。"""
        with self._lock:
            return bytes(self.target.read_memory_block8(address, length)).hex()

    def write_memory(self, address: int, data_hex: str) -> None:
        """ターゲットのメモリ（RAM・周辺）に 16 進文字列のバイト列を書く。Flash は flash を使う。"""
        with self._lock:
            self.target.write_memory_block8(address, bytes.fromhex(data_hex))
            self.target.flush()                            # pyOCD は書き込みを溜める。戻る時点で書き終えておく

    # --- シンボル・ブレークポイント・実行 -------------------------------------------------

    def elf_load(self, path: str) -> dict:
        """ELF のシンボルと行番号を読み込む（以後、場所を関数名・変数名・"ファイル:行" で指定でき、止まった場所が
        関数名と行で返る）。flash で ELF を書いたときは自動で読む。"""
        with self._lock:
            self._symbols = Symbols(path)
            self._forget_elf()
            if self._dbg is not None:
                self._dbg.symbols = self._symbols
            self._elf_loaded()
            return {"path": str(self._symbols.path), "entry": self._symbols.entry,
                    "segments": [{"address": s.address, "bytes": len(s.data)} for s in self._symbols.segments],
                    **self.source_list()}

    def source_list(self) -> dict:
        """読み込んだ ELF の行番号の情報が参照するソース（名前・場所・この PC にあるか）。"""
        with self._lock:
            if self._symbols is None:
                return {"files": []}
            return {"files": [{"name": name, "path": str(path), "exists": not is_network_path(path) and path.is_file()}
                              for name, path in sorted(self._symbols.sources().items())]}

    def source_read(self, name: str) -> dict:
        """ソースの本文と、命令のある行（ブレークポイントを置ける行）。読めるのは ELF が参照するファイルだけ。"""
        with self._lock:
            if self._symbols is None:
                raise RuntimeError("ELF が読み込まれていない（debug_elf_load）")
            path = self._symbols.sources().get(name)
            if path is None:
                raise KeyError(f"{name!r} はこの ELF のソースではない")
            if is_network_path(path):
                raise ValueError(f"外の共有のソースは開かない: {path}")
            if path.stat().st_size > SOURCE_LIMIT:
                raise ValueError(f"{name} は大きすぎる（{SOURCE_LIMIT // (1 << 20)} MB まで）")
            return {"name": name, "path": str(path), "text": path.read_text(encoding="utf-8", errors="replace"),
                    "lines": self._symbols.lines_of(name)}

    def symbol_info(self, location: str) -> dict:
        """場所（関数名・変数名・"ファイル:行"・"0x..."）の番地と、その番地の関数名・ファイル:行。"""
        with self._lock:
            if self._symbols is None:
                raise RuntimeError("ELF が読み込まれていない（elf_load）")
            address = self._symbols.resolve(location)
            out = {"address": address, "where": str(self._symbols.describe(address))}
            if not location.startswith("0x") and not is_line_location(location):
                s = self._symbols.symbol(location)
                out.update(kind=s.kind, size=s.size)
            return out

    def program_ram(self, path: str) -> dict:
        """ELF を RAM に読み込み、そのベクタ表から起動できる状態で止める（Flash には書かない。RAM で動く試験用の
        プログラム向け）。ターゲットはリセットされる。続けて run_until_halt / target_resume で走らせる。"""
        with self._lock:
            self.elf_load(path)
            stop = self.dbg.load_to_ram()
            self._image_written(elf=True)
            return self._stop_dict(stop)

    def breakpoint_set(self, location: str, condition: str | None = None, ignore_count: int = 0,
                       log: list[str] | None = None, message: str | None = None) -> dict:
        """ブレークポイントを置く。location: 関数名・"ファイル:行"・"0x..."。condition: C の式（偽なら止まらない。
        ローカル変数も使える）、ignore_count: 最初の n 回は止まらない、log: 当たるたびに読む式の一覧（止まらずに
        breakpoint_log と記録に残す。message は添える文）。動作のあるブレークポイントは当たるたびにコアを数 ms 止める。"""
        with self._lock:
            address = self.dbg.breakpoint(location if not location.startswith("0x") else int(location, 16))
            self._set_action(address, condition, ignore_count, log, message)
            if self._actions:
                self._watch_stops(self.target.get_state().name != "HALTED")
            return {"address": address, **self._place_of(address)}

    def breakpoint_clear(self, location: str | None = None) -> dict:
        """ブレークポイントを外す（location 省略ですべて。ウォッチポイントも含めて外す）。"""
        with self._lock:
            if location is None:
                self.dbg.clear()
                self._drop_actions()
                self._heap_trace_detached()                       # malloc / free の記録のブレークポイントも外れた
                self._gdb_trace_detached()                        # GDB のトレースポイントも外れた
            else:
                address = self.dbg.breakpoint_address(location if not location.startswith("0x") else int(location, 16))
                self.dbg.remove_breakpoint(address)
                self._drop_actions(address)
            return self.breakpoint_list()

    def breakpoint_list(self) -> dict:
        """置いているブレークポイントとウォッチポイント。"""
        with self._lock:
            return {"breakpoints": [{"address": a, "location": n, **self._place_of(a), **self._breakpoint_kind(a),
                                     **({"action": self._actions[a].describe()} if a in self._actions else {}),
                                     **({"tool": self._break_hooks[a][0]} if a in self._break_hooks else {})}
                                    for a, n in self.dbg.breakpoints.items()],
                    "watchpoints": [{"address": w.address, "size": w.size, "kind": w.kind}
                                    for w in self.dbg.watches.values()]}

    def watchpoint_set(self, location: str, kind: str = "write", size: int | None = None) -> dict:
        """ウォッチポイントを置く（変数名か "0x..."）。kind: write / read / access。size は変数名なら自動。"""
        with self._lock:
            w = self.dbg.watchpoint(location if not location.startswith("0x") else int(location, 16), size, kind)
            return {"address": w.address, "size": w.size, "kind": w.kind}

    def watchpoint_clear(self, location: str) -> dict:
        """ウォッチポイントを外す。"""
        with self._lock:
            self.dbg.remove_watchpoint(location if not location.startswith("0x") else int(location, 16))
            return self.breakpoint_list()

    def run_until_halt(self, timeout_s: float = 5.0) -> dict:
        """実行を再開し、止まるまで（ブレークポイント・ウォッチポイント等）か timeout_s まで待つ。時間切れなら止める。
        返す reason: breakpoint / watchpoint / step / user / timeout 等、where: 止まった関数名 + オフセットとファイル:行。
        セミホスティング（BKPT 0xAB）は処理して実行を続け、SYS_EXIT なら reason = "exit" と exit_code を返す。"""
        return self._run_until_halt(timeout_s)

    def variable_read(self, name: str, index: int | None = None) -> dict:
        """変数を名前で読む（1 / 2 / 4 / 8 バイトは整数、それ以外は 16 進文字列）。index は 4 バイト要素の配列の添字。"""
        with self._lock:
            value = self.dbg.read_variable(name, index)
            return {"name": name, "value": value if isinstance(value, int) else value.hex()}

    def variable_write(self, name: str, value: int, index: int | None = None) -> None:
        """変数を名前で書く（1 / 2 / 4 / 8 バイトの整数、または 4 バイト要素の配列の index 番目）。"""
        with self._lock:
            self.dbg.write_variable(name, value, index)

    def backtrace(self, variables: bool = False, max_frames: int = 64, max_elements: int = 64) -> list[dict]:
        """呼び出し履歴（ELF に CFI があれば CFI で巻き戻し、例外のフレームも越える。無ければいまの位置と戻り先）。
        各段は index・pc・where・note（"exception" = 例外で中断された位置）・cfa・function・offset・file・line。
        variables なら各段の引数とローカル変数（arguments・locals）も。停止中に呼ぶ。"""
        with self._lock:
            return self._backtrace(variables, max_frames, max_elements)

    @staticmethod
    def _stop_dict(stop: Stop) -> dict:
        return {"reason": stop.reason, "pc": stop.pc, **Probe._place(stop.where), "elapsed_s": stop.elapsed_s}

    # --- SWO --------------------------------------------------------------------------

    def swo_start(self, baud: int = 2_000_000, mode: str = "uart") -> None:
        """SWO の受信を始める。mode: "uart"（NRZ。TPIU の SPPR = 2）/ "manchester"（SPPR = 1。ケイパビリティの
        swo_manchester がある構成だけ。ビットごとに同期し直すので、送り手との速さのずれに強い）。ターゲット側の
        TPIU（ACPR = TRACECLKIN / baud - 1、SPPR）と ITM はファームウェアかデバッガで設定しておくこと。
        受けられる速さ（sys 100 MHz）: UART は 25 Mbaud まで（速さが一致すること。divisor = 100 MHz / baud が
        小さいほどずれに弱い）、Manchester は 16.7〜20 Mbit/s までが実用。"""
        if mode not in SWO_MODES:
            raise ValueError(f"SWO の方式は {'/'.join(SWO_MODES)}: {mode!r}")
        with self._lock:
            self.target                                    # 未接続ならここで失敗する
            if mode == "manchester" and not getattr(self.session.device.capabilities(), "swo_manchester", False):
                raise RuntimeError("この構成（ゲートウェア）は SWO の Manchester 方式を受けられない（swo_manchester なし）")
            # 止めてから設定する: pyOCD の swo_start は設定の失敗を黙って捨てる。受信中に速さを変えると、Mode を
            # 受信中に受けない版のゲートウェア（2026-10-18 より前）では前の速さのまま受けていた
            self._debug.probe.swo_stop()
            if mode == "uart":
                self._debug.probe.swo_start(baud)
            else:
                swo_start_mode(self._debug.probe, baud, mode)
            self._swo_mode = mode
            self._swo_reset()                              # 解読（swo_decode / swo_text）は受信の最初から
            self._swo_baud = baud
            self._swo_on = True
            with self._observe:
                self._logs["swo"] = ByteLog()              # 前の受信の残りは読ませない

    def swo_read(self, timeout_s: float = 0.5) -> str:
        """timeout_s の間に届いた SWO のバイト列（ITM パケットのまま、16 進文字列）。背景で読んでいる（TCP の口・
        ファイルへ流している）なら、前回の読みからの分も含む。"""
        with self._lock:
            self.target                                    # 未接続ならここで失敗する
        return self._collect("swo", timeout_s).hex()

    def swo_stop(self) -> None:
        """SWO の受信を止める。"""
        with self._lock:
            self._swo_on = False
            self._debug.probe.swo_stop()

    # --- ロジックアナライザ ----------------------------------------------------------

    def la_arm(self, rate_hz: float | None = None, pretrigger: int = 1024, mask: int = 0, value: int = 0,
               rise: int = 0, fall: int = 0, filter_channels: int = 0, filter_ns: float = 0.0,
               pulse: dict | None = None) -> dict:
        """捕捉を仕掛ける（すぐ戻る）。条件が何もなければ即トリガ。
        filter_channels（外の信号の ch = 拡張ヘッダのピンと専用の入力のビット）は filter_ns より短い変化を捨てる
        （グリッチフィルタ。フィルタした ch は filter.delay_ns 遅れて記録される）。pulse はパルス幅のトリガ {"channel", "level"（既定 1 = High の脈）,
        "min_ns", "max_ns"（省略で上限なし）, "timeout"（true なら level が min_ns を超えて続いた時点で成立）}。
        パルス幅はパターン（mask・rise・fall）と両方満たしたときに成立。ケイパビリティ la_filter / la_pulse が要る。"""
        with self._lock:
            self._la_idle()
            self._la = LogicAnalyzer(self.session.device)
            self._la_period = self._la.start(rate_hz=rate_hz, pretrigger=pretrigger, mask=mask, value=value,
                                             rise=rise, fall=fall, filter_channels=filter_channels,
                                             filter_ns=filter_ns, pulse=pulse)
            return {"sample_period_ns": self._la_period * 1e9, "depth": self._la.depth, **self._la.front_info}

    def la_collect(self, timeout_s: float = 5.0, vcd_path: str | None = None) -> dict:
        """完了を待って読み出し、チャンネルごとの要約（トリガ = 0 ns）を返す。vcd_path があれば VCD も書く。"""
        with self._lock:
            if self._la is None:
                raise RuntimeError("la_arm を先に呼ぶ")
            la, self._la = self._la, None
            la.wait(timeout_s)
            cap = la.read(self._la_period)
            names = channel_names(self.session.device.capabilities())
            self._keep_capture(Transitions.from_capture(cap), "la.capture", names)
            if vcd_path:
                write_vcd(cap, Path(vcd_path), names)
            return {"trigger_index": cap.trigger, "trigger_time": cap.trigger_time,
                    "sample_period_ns": cap.sample_period_s * 1e9, "samples": len(cap.samples),
                    "channels": _channel_summary(cap), "vcd": vcd_path}

    def _keep_capture(self, tr: Transitions, source: str, names: dict[int, str] | None = None) -> None:
        """最後の捕捉を画面の波形（la_view）のために持つ。変化点の表はここでは作らない（画面を使わない操作を遅くしない）。
        names は ch の既定の名前（捕捉した機器の申告 LA_LAYOUT か、読み込んだファイルの名前。書き出しに使う）。"""
        self._capture_id += 1
        self._capture, self._wave = tr, None
        if self._recorder is not None:
            add_capture(self._recorder, tr)                       # 記録中なら共通の時間軸の記録にも入れる
        self._capture_info = {"id": self._capture_id, "length": tr.length, "sample_period_ns": tr.sample_period_s * 1e9,
                              "trigger_index": tr.trigger, "channels": tr.channels, "source": source}
        self._capture_names = dict(names or {})

    def _capture_wave(self) -> tuple[Waveform, dict]:
        """最後の捕捉の変化点の表と情報（画面の波形・タイル用。表は最初に求められたときに作る）。"""
        with self._lock:
            if self._capture is None:
                raise RuntimeError("捕捉が無い（la_capture / la_deep_capture を先に）")
            tr, info, wave = self._capture, self._capture_info, self._wave
        if wave is None:
            wave = Waveform(tr)                                       # 長い捕捉では時間がかかるので、ロックの外で作る
            with self._lock:
                if self._capture is tr:
                    self._wave = wave
        return wave, info

    def la_view(self, start: int = 0, end: int | None = None, width: int = 1000) -> dict:
        """最後の捕捉を画面の幅に縮めた波形。levels[ch][i] は画素 i（サンプル [start + i·(end−start)/width, …)）が
        '0' / '1' / 'x'（区間内で変化）。"""
        wave, info = self._capture_wave()
        end = info["length"] if end is None else end
        levels = wave.view(start, end, width)
        return {"capture": info, "start": max(0, start), "end": min(info["length"], end), "width": width, "levels": levels}

    def la_capture(self, timeout_s: float = 5.0, vcd_path: str | None = None, **trigger) -> dict:
        armed = self.la_arm(**trigger)
        return {**self.la_collect(timeout_s, vcd_path), **{k: armed[k] for k in ("filter", "pulse") if k in armed}}

    # --- クロストリガ ----------------------------------------------------------------

    def xtrig_route(self, to_la: list[str] | None = None, to_inject: list[str] | None = None) -> dict:
        """源（"la_trigger" / "dap_match" / "host" / "sequence" = 多段トリガ）を動作につなぐ。to_la: ロジアナをトリガする源、
        to_inject: 割り込み書き込み（既定はターゲット停止）をする源。指定しない動作は切る。"""
        def flags(names):
            return Source(sum(Source[n.upper()] for n in names or []))
        with self._lock:
            if to_inject and not getattr(self.session.device.capabilities(), "inject", True):
                raise RuntimeError("この構成に故障注入（割り込み書き込み）が無い: probe_design_switch(protocol=\"swd\") で"
                                   "標準の構成に")
            CrossTrigger(self.session.device).route(to_la=flags(to_la), to_inject=flags(to_inject))
            return {"to_la": to_la or [], "to_inject": to_inject or []}

    def xtrig_set_inject(self, address: int = 0xE000EDF0, value: int = 0xA05F0003, ap: int = 0) -> None:
        """割り込み書き込みの番地と値（既定は DHCSR ← C_HALT | C_DEBUGEN = ターゲット停止）。
        ホストの DAP コマンドの境界で行い、ホストの SELECT・CSW・TAR は元に戻す。"""
        with self._lock:
            CrossTrigger(self.session.device).set_inject(address, value, ap)

    def xtrig_set_match(self, address: int, address_mask: int = 0xFFFFFFFF, data: int = 0, data_mask: int = 0,
                        on_write: bool = True, on_read: bool = False) -> None:
        """番地一致（源 dap_match）の条件: デバッガ（ホスト）がこの番地にアクセスしたら成立。data_mask = 0 なら値を見ない。"""
        with self._lock:
            CrossTrigger(self.session.device).set_match(address, address_mask=address_mask, data=data,
                                                        data_mask=data_mask, on_write=on_write, on_read=on_read)

    def xtrig_set_sequence(self, stages: list[dict], pattern_a: dict | None = None,
                           pattern_b: dict | None = None, arm: bool = True) -> dict:
        """多段トリガ（源 "sequence"）。stages は 1〜4 個の {"when": ["pattern_a" / "pattern_b" / "dap_match" / "host"],
        "count": 回数（既定 1）, "within_us": 段に入ってからの制限時間（省略で無制限、段 0 では無視）}。
        pattern_a / pattern_b は {"mask", "value", "rise", "fall"}（ロジアナのチャンネルのビット）。
        例: 「ch3 の立ち上がりの後 10 µs 以内に ch5 の立ち下がり」= stages=[{"when": ["pattern_a"]},
        {"when": ["pattern_b"], "within_us": 10}], pattern_a={"rise": 8}, pattern_b={"fall": 32}。
        xtrig_route の to_la / to_inject に "sequence" を入れてつなぐ。単発（再び使うときは arm）。"""
        names = {"pattern_a": Event.PATTERN_A, "pattern_b": Event.PATTERN_B, "dap_match": Event.DAP_MATCH,
                 "host": Event.HOST}
        seq_stages = []
        for st in stages:
            when = Event(0)
            for name in st["when"]:
                when |= names[name]
            within = st.get("within_us")
            seq_stages.append(Stage(when, int(st.get("count", 1)), None if within is None else within * 1e-6))
        with self._lock:
            seq = TriggerSequencer(self.session.device)
            seq.configure(seq_stages, Pattern(**(pattern_a or {})), Pattern(**(pattern_b or {})))
            if arm:
                seq.arm()
            stage, fired, _ = seq.status()
            return {"stages": len(seq_stages), "armed": arm, "stage": stage, "fired": fired}

    def xtrig_sequence_status(self) -> dict:
        """多段トリガの状態（今の段、発火したか、発火した時刻 = タイムベースのカウント）。"""
        with self._lock:
            stage, fired, time_ = TriggerSequencer(self.session.device).status()
            return {"stage": stage, "fired": fired, "time": time_}

    def xtrig_fire(self) -> None:
        """源 host を 1 回発火する。"""
        with self._lock:
            CrossTrigger(self.session.device).fire()

    def xtrig_status(self) -> dict:
        """割り込み書き込みの状態（結果・回数・時刻）と番地一致の回数・時刻（タイムベースのカウント）。"""
        with self._lock:
            xt = CrossTrigger(self.session.device)
            count, time_ = xt.match_status()
            return {"inject": vars(xt.inject_status()), "match": {"count": count, "time": time_}}

    def la_deep_capture(self, rate_hz: float | None = None, mask: int = 0, value: int = 0, rise: int = 0,
                        fall: int = 0, rle: bool = True, before_beats: int = 4096, after_beats: int = 4096,
                        timeout_s: float = 10.0, vcd_path: str | None = None, filter_channels: int = 0,
                        filter_ns: float = 0.0, pulse: dict | None = None) -> dict:
        """深いバッファ（外部メモリ 128 MB）で捕捉して要約を返す。トリガ条件（グリッチフィルタ・パルス幅を含む）は
        la_capture と同じ。rle = True は変化点圧縮（遅い信号なら数秒〜数十秒分）、False は RAW（100 MS/s で最大 0.67 秒）。
        読むのはトリガの前 before_beats 拍・後 after_beats 拍（1 拍 = RAW 8 サンプル / RLE 4 変化点）。
        ターゲット操作と同時に使うときは la_deep_arm → 操作 → la_deep_collect。"""
        armed = self.la_deep_arm(rate_hz, mask, value, rise, fall, rle, after_beats, filter_channels, filter_ns, pulse)
        result = self.la_deep_collect(timeout_s, before_beats, vcd_path)
        return {**result, **{k: armed[k] for k in ("filter", "pulse") if k in armed}}

    def la_deep_arm(self, rate_hz: float | None = None, mask: int = 0, value: int = 0, rise: int = 0, fall: int = 0,
                    rle: bool = True, after_beats: int = 4096, filter_channels: int = 0, filter_ns: float = 0.0,
                    pulse: dict | None = None) -> dict:
        """深いバッファの捕捉を仕掛ける（すぐ戻る）。filter_channels・filter_ns・pulse は la_arm と同じ。"""
        with self._lock:
            self._la_idle()
            self._deep = DeepAnalyzer(self.session.device)
            if self._collector is not None:
                self._collector.pause_events(True)                # 深いバッファの捕捉中はイベント記録を読めない
            period = self._deep.start(rate_hz=rate_hz, mask=mask, value=value, rise=rise, fall=fall, rle=rle,
                                      post_beats=after_beats, filter_channels=filter_channels, filter_ns=filter_ns,
                                      pulse=pulse)
            return {"sample_period_ns": period * 1e9, "capacity_beats": self._deep.beats, **self._deep.front_info}

    def la_deep_collect(self, timeout_s: float = 10.0, before_beats: int = 4096, vcd_path: str | None = None) -> dict:
        """深いバッファの完了を待って、トリガ前 before_beats 拍から後ろを読み、要約を返す。"""
        with self._lock:
            if getattr(self, "_deep", None) is None:
                raise RuntimeError("la_deep_arm を先に呼ぶ")
            deep, self._deep = self._deep, None
            try:
                deep.wait(timeout_s)
                tr = deep.read_transitions(before_beats=before_beats)    # 長い記録も展開しない
            finally:
                if self._collector is not None:
                    self._collector.pause_events(False)
            names = channel_names(self.session.device.capabilities())
            self._keep_capture(tr, "la.deep.capture", names)
            if vcd_path:
                write_vcd(tr, Path(vcd_path), names)
            return {"trigger_index": tr.trigger, "trigger_time": tr.trigger_time,
                    "sample_period_ns": tr.sample_period_s * 1e9, "samples": tr.length,
                    "duration_s": tr.length * tr.sample_period_s,
                    "channels": _transition_summary(tr), "vcd": vcd_path}

    def la_stream(self, vcd_path: str, duration_s: float = 1.0, rate_hz: float | None = None,
                  rle: bool = True) -> dict:
        """ロジアナを止めずに duration_s 秒流し続け、VCD に書く（トリガなし。記録長は保存先で決まる）。
        RAW は 12.5 MS/s（25 MB/s）程度まで、rle = True（変化点圧縮）は遅い信号ならそれ以上。
        overrun = True はホストが遅れて途中で止まったこと（それまでの分は正しい）。"""
        with self._lock:
            self._la_idle()
            s = DeepAnalyzer(self.session.device).stream_to_vcd(Path(vcd_path), duration_s, rate_hz=rate_hz, rle=rle)
            return {"samples": s.samples, "duration_s": s.duration_s, "sample_period_ns": s.sample_period_s * 1e9,
                    "elapsed_s": s.elapsed_s, "mb_per_s": s.mb_per_s, "overrun": s.overrun, "vcd": vcd_path}

    # --- 共通の時間軸の記録（F3） ----------------------------------------------------------

    @staticmethod
    def _recordings_dir() -> Path:
        from .api.daemon import fprobe_home
        return fprobe_home() / "recordings"

    def recording_start(self, name: str = "", events: bool = True, segment_s: float | None = None,
                        segment_bytes: int | None = None, max_bytes: int | None = None,
                        decimate_after_s: float | None = None, decimate_streams: list[str] | None = None) -> dict:
        """共通の時間軸の記録を始める。記録中は、ロジアナの捕捉・DAP / UART / SWO / 電圧のイベント・時計の対応付け・
        印・（デーモン経由なら）すべての操作とターゲットの状態の変化が、1 つの時刻の上に保存される。
        長期の記録（日単位）: segment_s（分割の時間）・segment_bytes・max_bytes（容量の上限。古い分割から消す）・
        decimate_after_s（閉じてからこの秒数で decimate_streams を間引く）のどれかを指定すると分割した記録になる。"""
        with self._lock:
            if self._recorder is not None:
                raise RuntimeError("記録中（recording_stop を先に）")
            device = self.session.device
            caps = device.capabilities()
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            safe = re.sub(r"[^\w\-]+", "_", name).strip("_")
            directory = self._recordings_dir() / (f"{stamp}-{safe}" if safe else stamp)
            self._recorder = self._make_recorder(
                directory, caps.timebase_hz,
                {"name": name, "board": caps.board, "design": caps.design,
                 "elf": str(self._symbols.path) if self._symbols else None},
                segment_s, segment_bytes, max_bytes, decimate_after_s, decimate_streams)
            self._collector = Collector(device, self._recorder, lock=self._lock)
            if not events:
                self._collector.events = None                     # イベント記録を取り込まない
            self._collector.start()
            self._view = Recording.open(directory)
            return {"recording": directory.name, "path": str(directory)}

    def recording_stop(self) -> dict:
        """記録を止める（保存した記録は recording_list / recording_open で読める）。"""
        collector, recorder = self._collector, self._recorder
        if recorder is None:
            raise RuntimeError("記録していない")
        self._stop_la_record()                                    # 流し続けの残りを入れてから閉じる
        collector.stop()                                          # ロックの外で（集める側がロックを取るため）
        with self._lock:
            recorder.close()
            self._recorder = self._collector = None
            return {"recording": recorder.directory.name, "events": self._view.count() if self._view else 0}

    def recording_status(self) -> dict:
        """記録中か、記録の名前・件数・時刻の範囲・ホストの出来事の時刻の不確かさ。"""
        rec, view = self._recorder, self._view
        out = {"recording": rec is not None, "name": view.directory.name if view else None}
        if view is not None:
            if rec is not None:
                rec.flush()
            out.update(events=view.count(), span=view.span(), timebase_hz=view.meta["timebase_hz"])
        if rec is not None:
            try:
                out["clock_uncertainty_ticks"] = rec.clock.to_ticks(time.monotonic())[1]
            except RuntimeError:
                out["clock_uncertainty_ticks"] = None
        if view is not None:
            out["segmented"] = view.meta.get("layout") == "segmented"
        if self._la_record is not None:
            out["la_stream"] = self._la_record.status()
        return out

    def recording_list(self) -> dict:
        """保存した記録の一覧（新しい順）。"""
        root = self._recordings_dir()
        items = []
        for d in sorted(root.glob("*/meta.json"), reverse=True):
            meta = json.loads(d.read_text(encoding="utf-8"))
            items.append({"name": d.parent.name, "title": meta.get("name"), "board": meta.get("board")})
        return {"recordings": items}

    def recording_open(self, name: str) -> dict:
        """保存した記録を開く（以後の recording_events 等の対象）。name は recording_list の name。"""
        directory = self._recordings_dir() / name
        if directory.parent != self._recordings_dir() or not (directory / "meta.json").is_file():
            raise KeyError(f"記録 {name!r} が無い")
        self._view = Recording.open(directory)
        return {"name": name, "events": self._view.count(), "span": self._view.span()}

    def _reading(self) -> Recording:
        if self._view is None:
            raise RuntimeError("読む記録が無い（recording_start か recording_open を先に）")
        if self._recorder is not None:
            self._recorder.flush()
        return self._view

    def recording_events(self, t0: int | None = None, t1: int | None = None, streams: list[str] | None = None,
                         kinds: list[str] | None = None, limit: int = 1000) -> dict:
        """時刻（タイムベース）の範囲の出来事を時刻順に。streams の例: waveform 以外の dap / uart / swo / rtt / aux /
        host / target / annotation / sample / clock。"""
        return {"events": [asdict(e) for e in self._reading().events(t0, t1, streams, kinds, limit)]}

    def recording_find(self, query: dict) -> dict:
        """条件に一致する出来事の一覧。query: stream / kind / t0 / t1 / a / b（値か {"eq"|"lt"|"ge"…: 値}）/
        body（{欄: 値か {"regex": …}}）。"""
        found = self._reading().find(query)
        return {"count": len(found), "events": [asdict(e) for e in found[:1000]]}

    def recording_intervals(self, start: dict, end: dict) -> dict:
        """開始の条件から次の終了の条件までの時間（タイムベース）の一覧と統計（最小・最大・平均・標準偏差）。"""
        stats = self._reading().intervals(start, end)
        stats["intervals"] = stats["intervals"][:1000]
        return stats

    def recording_density(self, t0: int, t1: int, width: int = 1000, streams: list[str] | None = None) -> dict:
        """画素ごとの出来事の件数（縮図・タイムラインの要約）。長期の記録で間引いた・消した部分にかかれば coverage。"""
        view = self._reading()
        out = {"counts": view.density(t0, t1, width, streams)}
        coverage = view.coverage(t0, t1)
        if not coverage["complete"]:
            out["coverage"] = coverage
        return out

    def recording_waveform(self, t0: int, t1: int, width: int = 1000) -> dict:
        """記録の波形を画素ごとに '0' / '1' / 'x'（変化）/ '-'（波形が無い）。"""
        return {"levels": self._reading().waveform_view(t0, t1, width)}

    def recording_mark(self, text: str, t: int | None = None) -> dict:
        """印とメモを残す（調査の経緯）。t（タイムベース）を省くと記録中の記録のいまの時刻、指定するとその時刻
        （記録中の記録か、開いた記録。時間軸で選んだ時刻に後から印を置く）。"""
        rec = self._recorder
        if rec is not None:
            if t is None:
                return {"t": rec.add_host("annotation", "mark", time.monotonic(), body={"text": text})}
            rec.add("annotation", "mark", t, body={"text": text})
            return {"t": t}
        if t is None:
            raise RuntimeError("記録していない（recording_start を先に。開いた記録には t（タイムベース）を指定して印を足す）")
        self._reading().add("annotation", "mark", t, body={"text": text})
        return {"t": t}

    def recording_export(self, format: str, path: str, t0: int | None = None, t1: int | None = None,
                         streams: list[str] | None = None, waveform: bool = True,
                         channels: list[int] | None = None) -> dict:
        """記録を書き出す。format: vcd（波形）/ csv / json（出来事）/ perfetto（Chrome JSON のトレースイベント。
        Perfetto UI・chrome://tracing で開く）。t0 / t1 / streams は csv・json・perfetto の範囲と種類、waveform・channels は
        perfetto の波形（channels は vcd も）。"""
        r = self._reading()
        if format == "perfetto":
            return write_trace(r, Path(path), t0=t0, t1=t1, streams=streams, waveform=waveform, channels=channels,
                               names=self._counter_names())
        if format == "vcd":
            if t0 is not None or t1 is not None or streams is not None:
                raise ValueError("vcd は範囲・streams で絞れない（波形の全体。channels だけ）")
            r.export_vcd(Path(path), channels)
        else:
            {"csv": r.export_csv, "json": r.export_json}[format](Path(path), t0=t0, t1=t1, streams=streams)
        return {"path": path}

    def record_host_event(self, stream: str, kind: str, body: dict) -> None:
        """デーモンが、操作の記録（activity）やターゲットの状態の変化を記録中の記録へ入れる口。"""
        rec = self._recorder
        if rec is not None:
            try:
                rec.add_host(stream, kind, time.monotonic(), body=body)
            except RuntimeError:
                pass                                              # 時計の対応付けの前（始めた直後）

    # --- DAP のイベント記録 ------------------------------------------------------------

    def events_start(self) -> dict:
        """DAP 転送のイベント記録を始める（外部メモリのリング。古いものから上書き）。"""
        with self._lock:
            self._la_idle()
            log = EventLog(self.session.device)
            log.start()
            return {"capacity": log.capacity}

    def events_stop(self) -> dict:
        """イベント記録を止める（記録は残り、events_read で読める）。"""
        with self._lock:
            self._la_idle()
            log = EventLog(self.session.device)
            log.stop()
            written, lost = log.counts()
            return {"written": written, "lost": lost}

    def events_read(self, last: int = 200, raw: bool = False, since: int | None = None) -> dict:
        """イベントを古い順に返す。since が無ければ最新の last 件。since（前回の結果の next）があれば、その番号から
        last 件まで（続けて読めば取りこぼさない。読む前に上書きされた数は skipped）。first = 返した最初の番号、
        next = 次に since に渡す番号（番号は記録を始めてから書いた数で数える）。raw = False ならアクセス（ポストされた
        読みを対応付けた後）、True なら線上の転送そのもの。time_us は記録開始とは無関係なタイムベースの時刻（µs）。"""
        with self._lock:
            self._la_idle()
            log = EventLog(self.session.device)
            written, lost = log.counts()
            extra = {}
            if since is None:
                events = log.read(last, end=written)
                first = written - len(events)
            else:
                since = since if 0 <= since <= written else 0      # 範囲の外 = 記録が始め直された: 最初から
                first = max(since, written - log.capacity)
                extra["skipped"] = first - since
                n = min(last, written - first)
                events = log.read(n, end=first + n) if n > 0 else []
            hz = self.session.device.capabilities().timebase_hz
            if raw:
                items = [{"time_us": e.time * 1e6 / hz, "kind": e.kind, "ap": e.ap, "read": e.read,
                          "register": e.register, "ack": e.ack, "parity_error": e.parity_error,
                          "address": f"0x{e.address:08X}", "data": f"0x{e.data:08X}"} for e in events]
            else:
                items = [{"time_us": a.time * 1e6 / hz, "kind": a.kind, "address": f"0x{a.address:08X}",
                          "value": f"0x{a.value:08X}", "ack": a.ack} for a in resolve(events)]
            return {"written": written, "lost": lost, "first": first, "next": first + len(events), **extra,
                    "items": items}

    # --- UART（USB では CDC-ACM、フレームのリンクでは ch 3 = UART0） -----------------

    def _uart_link(self, baud: int | None) -> FrameLink | None:
        """フレームのリンクで開いているならそのリンク（ボーレートを LINE_CODING で合わせる）。USB なら None。"""
        link = self.session.link
        if link is not None:
            want = baud or link.uart_baud or 115200
            if link.uart_baud != want:
                link.set_uart_baud(want)
        return link

    def _uart_port(self, baud: int | None) -> Serial:
        if self._uart is None:
            serial_number = self._serial
            ports = [p.device for p in list_ports.comports() if p.vid == g.USB_VID and p.pid == g.USB_PID
                     and (serial_number is None or p.serial_number == serial_number)]
            if len(ports) != 1:
                raise RuntimeError(f"fprobe の COM ポートが特定できない: {ports}")
            self._uart = Serial(ports[0], baud or 115200, timeout=0)
        elif baud and self._uart.baudrate != baud:
            self._uart.baudrate = baud                     # ボーレートはライン設定でプローブに伝わる
        return self._uart

    def uart_write(self, data: str, baud: int | None = None) -> int:
        """ターゲットの UART に送る（latin-1）。baud を指定するとボーレートも変える。"""
        with self._lock:
            if link := self._uart_link(baud):
                return link.uart_write(data.encode("latin-1"))
            port = self._uart_port(baud)
            n = port.write(data.encode("latin-1"))
            port.flush()
            return n

    def _uart_fetch(self, baud: int | None = None) -> None:
        """UART の届いた分を読んで貯める（_lock の中で）。"""
        link = self._uart_link(baud)
        data = link.uart_read(4096) if link else self._uart_port(baud).read(4096)
        if data:
            self._received("uart", bytes(data), time.monotonic())

    def uart_read(self, timeout_s: float = 0.5, baud: int | None = None) -> str:
        """timeout_s の間に届いたものを返す（latin-1 で文字列化）。背景で読んでいる（TCP の口・ファイルへ流している）
        なら、前回の読みからの分も含む。"""
        if baud is not None:
            with self._lock:
                self._uart_fetch(baud)                     # ボーレートを先に合わせる
        return self._collect("uart", timeout_s).decode("latin-1")

    def uart_configure(self, baud: int) -> dict:
        """ターゲットの UART のボーレートを変える（送らない・読まない。端末の画面の設定）。変えた後の baud を返す。"""
        with self._lock:
            if link := self._uart_link(baud):
                return {"baud": link.uart_baud}
            return {"baud": self._uart_port(baud).baudrate}

    def uart_flow(self, auto: bool = False, rts_channel: int | None = None, cts_channel: int | None = None) -> dict:
        """UART のハードウェアのフロー制御（U52）。rts_channel / cts_channel: RTS を出す / CTS を読む端子（ロジアナの
        ch。候補はケイパビリティ uart_flow = 結果の pins。Primer は 4 予備・1 TDI。選んだ端子の普段の機能の出力は止める。
        None で使わない）。auto: 自動の RTS/CTS（CTS が出ている間だけ送り、受信の下流が満杯なら RTS を下げる。CTS を
        取らなければ CTS は出ているとみなす）。auto = False なら RTS は COM ポートの RTS どおり。線はアクティブ Low。
        設定と制御線の状態を返す。"""
        with self._lock:
            flow = UartFlow(self.session.device)
            flow.configure(auto, rts_channel, cts_channel)
            return self._uart_flow_state(flow)

    def uart_lines(self) -> dict:
        """UART の制御線の状態（U52）: cts（CTS が出ている）・rts（RTS を出している）・host_rts（COM ポートの RTS）・
        host_break（ブレークを送っている）と、フロー制御の設定（uart_flow）。"""
        with self._lock:
            return self._uart_flow_state(UartFlow(self.session.device))

    @staticmethod
    def _uart_flow_state(flow: UartFlow) -> dict:
        lines = flow.lines()
        return {**flow.config(), "pins": list(flow.pins), "cts": lines.cts, "rts": lines.rts,
                "host_rts": lines.host_rts, "host_break": lines.host_break}

    def _uart_baud_now(self) -> int | None:
        """いまの UART のボーレート（まだ開いていなければ None）。機器には触らない。"""
        link = getattr(self._session, "link", None)
        if link is not None:
            return getattr(link, "uart_baud", None)
        return self._uart.baudrate if self._uart is not None else None


def build_server(probe: Probe) -> MCPServer:
    server = MCPServer("fprobe", instructions=INSTRUCTIONS)
    for name in ["probe_info", "csr_read", "csr_write", "target_voltage", "elf_load", "symbol_info", "program_ram",
                 "breakpoint_set", "breakpoint_clear", "breakpoint_list", "watchpoint_set", "watchpoint_clear",
                 "run_until_halt", "variable_read", "variable_write", "backtrace",
                 "target_connect", "target_disconnect", "target_status", "target_halt", "target_resume",
                 "target_step", "target_reset", "read_registers", "write_register", "read_memory",
                 "write_memory", "flash", "la_arm", "la_collect", "xtrig_route", "xtrig_set_inject",
                 "xtrig_set_match", "xtrig_set_sequence", "xtrig_sequence_status", "xtrig_fire", "xtrig_status", "swo_start", "swo_read", "swo_stop",
                 "la_deep_capture", "la_deep_arm", "la_deep_collect", "la_stream", "events_start", "events_stop", "events_read",
                 "uart_write", "uart_read"]:
        server.add_tool(getattr(probe, name))
    # la_capture は **trigger を明示した版を出す（MCP のスキーマは引数から作られる）
    server.add_tool(_la_capture(probe))
    return server


def _la_capture(probe: Probe):
    def la_capture(rate_hz: float | None = None, pretrigger: int = 1024, mask: int = 0, value: int = 0,
                   rise: int = 0, fall: int = 0, timeout_s: float = 5.0, vcd_path: str | None = None,
                   filter_channels: int = 0, filter_ns: float = 0.0, pulse: dict | None = None) -> dict:
        """捕捉を仕掛けて完了まで待つ（la_arm + la_collect）。トリガ = (サンプル & mask) == (value & mask)
        かつ rise / fall のビットのチャンネルのどれかがその向きに変化（かつ pulse のパルス幅。la_arm を参照）。"""
        return probe.la_capture(timeout_s, vcd_path, rate_hz=rate_hz, pretrigger=pretrigger, mask=mask,
                                value=value, rise=rise, fall=fall, filter_channels=filter_channels,
                                filter_ns=filter_ns, pulse=pulse)
    return la_capture


import functools
import inspect as _inspect
import operator
import typing
import socket as _socket
import subprocess as _subprocess
import sys as _sys

from .api.client import DaemonClient
from .api.daemon import DEFAULT_PORT, device_of
from .api import NotConnected
from .api.spec import Method, Spec, load_spec

_PY_TYPES = {"integer": int, "number": float, "string": str, "boolean": bool}


def _union(types: list):
    """型の並びの合併（重なりは 1 つに。順は保つ）。"""
    return functools.reduce(operator.or_, dict.fromkeys(types))


def _annotation(schema: dict):
    """JSON Schema → Python の型注釈（MCP の SDK が道具の入力の形を作るのに使う）。型の書かれていないものは Any。"""
    if "anyOf" in schema:                               # どれかの形（整数か "semihosting" 等）
        inner = [s for s in schema["anyOf"] if s.get("type") != "null"]
        if not inner:
            return type(None)
        base = _union([_annotation(s) for s in inner])
        return base | None if len(inner) < len(schema["anyOf"]) else base
    if "enum" in schema:                                # 値の型（[8, 16, 32] は整数）
        return _union([type(v) for v in schema["enum"]])
    kind = schema.get("type")
    if isinstance(kind, list):                          # {"type": ["integer", "null"]} の形
        inner = [k for k in kind if k != "null"]
        base = _union([_annotation({**schema, "type": k}) for k in inner]) if inner else type(None)
        return base | None if "null" in kind and inner else base
    if kind == "array":
        return list[_annotation(schema.get("items", {}))]
    if kind == "object":
        return dict[str, typing.Any]
    return _PY_TYPES.get(kind, typing.Any)


NOTICE_TOPICS = ("elf.changed",)                      # AI に知らせる通知（次の道具の結果の notices に添える）
NOTICE_LIMIT = 20


class Notices:
    """デーモンの通知のうち AI に知らせるもの。MCP には押し出しが無いので、次に返す道具の結果（辞書）に
    notices = [{"topic", "message", ...}] として添える（添えたら消す。溜めるのは最新 NOTICE_LIMIT 件）。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._pending: list[dict] = []

    def add(self, topic: str, params: dict) -> None:
        with self._lock:
            self._pending.append({"topic": topic, **params})
            del self._pending[:-NOTICE_LIMIT]

    def attach(self, result):
        if not isinstance(result, dict):
            return result                               # 辞書でない結果の形は変えない（次の辞書の結果に添える）
        with self._lock:
            pending, self._pending = self._pending, []
        return {**result, "notices": pending} if pending else result


def tool_function(client, method: Method, notices: Notices | None = None):
    """仕様のメソッドを、型付きの引数を持つ関数（MCP の道具）にする。呼ぶとデーモンへ転送する。
    "x-mcp": false の引数は出さない（AI に許さない設定。例: セミホスティングのファイルの root）。
    notices を渡すと、溜まった通知を結果に添える。"""
    props = {name: schema for name, schema in method.params.get("properties", {}).items()
             if schema.get("x-mcp", True)}
    required = set(method.params.get("required", []))
    parameters = [
        _inspect.Parameter(name, _inspect.Parameter.KEYWORD_ONLY, annotation=_annotation(schema),
                           default=_inspect.Parameter.empty if name in required else schema.get("default"))
        for name, schema in props.items()]

    def tool(**kwargs):
        result = client.call(method.name, **kwargs)
        return result if notices is None else notices.attach(result)

    tool.__signature__ = _inspect.Signature(parameters)
    tool.__annotations__ = {p.name: p.annotation for p in parameters}
    tool.__name__ = method.name.replace(".", "_")
    tool.__doc__ = method.summary
    return tool


def build_client_server(client, spec: Spec, notices: Notices | None = None) -> MCPServer:
    server = MCPServer("fprobe", instructions=INSTRUCTIONS)
    for method in spec.methods.values():
        if method.name.startswith("session.") or method.binary:      # バイナリの応答は画面用（AI は la.view 等）
            continue
        server.add_tool(tool_function(client, method, notices), name=method.name.replace(".", "_"),
                        description=method.summary)
    return server


def ensure_daemon(args) -> str:
    """デーモンにつなぐ URL。動いていなければ子プロセスで起動し、待ち受けるまで待つ。起動の出力は ~/.fprobe/daemon.log。"""
    from .api.daemon import fprobe_home
    url = f"ws://127.0.0.1:{args.listen}/api"
    if _listening(args.listen):
        return url
    command = [_sys.executable, "-m", "fprobe.api.daemon", "--listen", str(args.listen)]
    for flag, value in (("--serial", args.serial), ("--port", args.port), ("--eth", args.eth)):
        if value:
            command += [flag, value]
    if args.port:
        command += ["--baud", str(args.baud)]
    if getattr(args, "bitstreams", None):
        command += ["--bitstreams", args.bitstreams]
    fprobe_home().mkdir(parents=True, exist_ok=True)
    log = open(fprobe_home() / "daemon.log", "a", encoding="utf-8")
    _subprocess.Popen(command, stdout=log, stderr=_subprocess.STDOUT,
                      creationflags=getattr(_subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    deadline = time.monotonic() + 20
    while not _listening(args.listen):
        if time.monotonic() > deadline:
            raise RuntimeError(f"fprobe-daemon が起動しない（{log.name} を見る）")
        time.sleep(0.2)
    return url


class ReconnectingClient:
    """デーモンへの接続（切れたら次の呼び出しでつなぎ直す）。切れた呼び出しはやり直さない（二重に実行しない）。"""

    def __init__(self, connect):
        self._connect = connect
        self._client = None
        self._lock = threading.Lock()

    def call(self, method: str, **params):
        with self._lock:
            if self._client is None:
                self._client = self._connect()
            client = self._client
        try:
            return client.call(method, **params)
        except ConnectionError as e:
            with self._lock:
                if self._client is client:
                    self._client = None
            client.close()
            raise ConnectionError(f"fprobe-daemon との接続が切れた（{e}）。次の呼び出しでつなぎ直す") from e

    def close(self) -> None:
        with self._lock:
            if self._client is not None:
                self._client.close()
                self._client = None


def requested_device(args) -> dict:
    return device_of(args.serial, args.port, args.eth)


def check_device(requested: dict, running: dict) -> None:
    """既に動いているデーモンが、頼まれた機器を握っているか（違えば黙って別の機器を操作しないよう誤りにする）。"""
    if requested != running:
        raise RuntimeError(f"動いている fprobe-daemon は別の機器を握っている（{running}、頼まれたのは {requested}）。"
                           "そのデーモンを止めるか、別のポート（--listen）で起動する")


def _listening(port: int) -> bool:
    with _socket.socket() as s:
        s.settimeout(0.2)
        return s.connect_ex(("127.0.0.1", port)) == 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--serial", help="fprobe の USB シリアル番号（複数台あるとき）")
    parser.add_argument("--name", help="個体名（fprobe name set で付けた名前）。--serial / --port / --eth の代わり")
    parser.add_argument("--port", help="SerialLink（UART のホストリンク）の COM ポート。指定すると USB ではなくこれで開く")
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUD, help="SerialLink のボーレート")
    parser.add_argument("--eth", help="EthLink（UDP のホストリンク）のボードの IP アドレス。指定すると USB ではなくこれで開く")
    parser.add_argument("--listen", type=int, default=DEFAULT_PORT, help="fprobe-daemon のポート")
    parser.add_argument("--bitstreams", help="起動するデーモンに渡す: Primer の構成のビットストリームの置き場所"
                                             "（<dir>/<design>/top.fs）")
    parser.add_argument("--token", help="LAN に開いた（--lan の）fprobe-daemon のトークン")
    args = parser.parse_args()
    try:
        probe_names.apply_to_args(args)                  # 名前 → --serial / --port / --eth（デーモンにもそれを渡す）
    except probe_names.ProbeNameError as e:
        parser.error(str(e))

    notices = Notices()

    def connect() -> DaemonClient:
        client = DaemonClient(ensure_daemon(args), "mcp", "ai", token=args.token)
        try:
            check_device(requested_device(args), client.hello["device"])
            client.subscribe(list(NOTICE_TOPICS), notices.add)
        except BaseException:
            client.close()
            raise
        return client

    client = ReconnectingClient(connect)
    client.call("session.status")                        # 起動時に一度つないで、機器の取り違えを早く知らせる
    try:
        build_client_server(client, load_spec(), notices).run("stdio")
    finally:
        client.close()


if __name__ == "__main__":
    main()
