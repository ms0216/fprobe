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
import os
import re
import threading
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from serial import Serial
from serial.tools import list_ports

from . import fault
from . import generated as g
from . import probe_names
from .api import NoData
from .aux_mcu import Aux
from .collector import Collector, add_capture
from .events import INJECT_KINDS, EventLog, resolve
from .fpga_obs import FpgaObs, compile_match, samples as obs_samples
from .correlate import ConditionError, ConditionWatcher, correlate, durations, transactions
from .fpga_signals import FpgaSignals
from .export import write_trace
from .la import DeepAnalyzer, LogicAnalyzer, Transitions, Waveform, channel_names, external_pattern, write_vcd
from .debug import Debugger, Stop
from .frame_link import FrameLink
from .obs_condition import count_condition
from .probe_analysis import AnalysisTools
from .voltage import VoltageHistory
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
from .probe_linux import LinuxTools
from .probe_longrec import LongRecordingTools
from .probe_lowlevel import LowLevelTools
from .probe_monitor import MonitorModeTools
from .probe_multicore import MulticoreTools
from .probe_observe import ObserveTools
from .probe_observe import ByteLog, ObserveTools
from .probe_profile import ProfileTools
from .probe_profile_export import ProfileExportTools
from .probe_rtos import RtosTools
from .probe_recording_rtos import RecordingRtosTools
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

TRIGGER_PERIOD_S = 0.5                 # recording_trigger: 記録を調べる周期
UART_DEFAULT_BAUD = 115_200            # UART を頼まれた速さ無しで開くときの速さ（使った速さは結果の baud に出す）
REOPEN_PERIOD_S = 1.0                  # fpga_obs_open の auto_reopen: 止まっていないか見る周期
FIND_LIMIT = 1000                      # recording_find・recording_intervals が返す一覧の上限の既定（総数と切ったかも返す）

INSTRUCTIONS = """fprobe（FPGA デバッグプローブ）を操作する。
- まず `probe_info` でケイパビリティ（ロジアナのチャンネル数・深さ等）を確認する。
- ターゲットの操作は `target_connect` の後。target は pyOCD のターゲット名（例: stm32f401xe, cortex_m）。
  "auto" なら部品を判別して選ぶ。つながらないときは未接続で `target_diagnose`（電圧 → SWD / JTAG → DP → AP →
  部品 → 保護を順に調べ、段ごとに読めた事実。試す速さは frequencies_hz で渡す）。
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
  止まった理由は `target_status` の halt_reason（request / breakpoint / bkpt_instruction = ファームウェア自身の BKPT で
  panic・assert 等 / watchpoint / vector_catch / external）。ファームウェアが固まって普通のリセットで起動し直さない
  RP2040 は `target_reset(kind="rescue")`（救済の DP でチップ全体（USB 等も）をリセットし、つなぎ直して起動）。
  故障（HardFault 等）で止まったら `target_fault` で原因・故障した行・その時のレジスタ（run_until_halt にも要約が付く）。
  故障の入口で止めるには `target_vector_catch`（例 ["hard_fault", "bus_fault"]。読むだけは `target_vector_catch_get`）。
  リセットの種類は `target_reset` の kind（system / core / hardware = nRESET の線）。
- 止めると壊れうるターゲット: 止める・ブレークポイント・ステップ・`debug_call` の前に `debug_halt_risk`（`debug_halt_risk_set`
  で渡した名前・正規表現に合うシンボルが ELF にあるか。本体は名前の表を持たず、事実だけで判定はしない）。どの名前を
  探すかは使う側が決める（慣習の表はプラグイン fprobe.plugins.heuristics の halt_risk_conventions。経験則）。合えば
  人に知らせ、止めずに調べる道具かモニタ方式を先に。走っているのにロックアップしている・`target_liveness_set` で渡した
  関数の中を回り続けている・ティックが変わっていないと `target_status` に fatal が付く（止めずに見張る）。
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
  読んだ値（evaluate・変数・watch・戻り値等）はどれも kind（値の種類。DWARF の型で決まる）を持つ object: int・float・
  bool（value）/ bytes（hex）/ enum（name・value）/ pointer（address・to）/ struct（members）/ array（length・items。
  items が length より少なければ max_elements で切った）/ unavailable（reason）。
- 周辺レジスタ: `target_peripheral_read`（"RCC.CR" か "GPIOA"。フィールドと列挙の名前に解読。SVD は先に
  `target_svd_load`（SVD のファイル名か path）。周辺の全部を読むとき、読まないものは skip）、`target_peripheral_write`（fields で名前の値）。メモリは `target_memory_view`
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
  `log_read`（記録中なら stream "log"）。文字の中の 0x の数のうち実行区画に入る値の場所は `log_annotate`。トレースは `trace_start`（systemview・ctf・
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
  解析: `rtos_timeline_load`（負荷の推移）/ `rtos_timeline_jobs`（出来事で区切ったジョブと、区間の事実）/
  `rtos_timeline_interval`（記録の 2 点の間の横取りを除いた時間）/ `rtos_timeline_memory`（スタック・ヒープの推移）/
  `rtos_timeline_profile`（スレッド・割り込み・アイドル）/ `rtos_timeline_inversions`（優先度の逆転の区間）。
  itm でファームのフック（fprobe_trace.h）がカーネルの出来事も出すと: `rtos_timeline_events`（API 呼び出し・待ち・
  ready）/ `rtos_timeline_flow`（Communication Flow）/ `rtos_timeline_blocking`（ブロック時間）/ `rtos_timeline_queues`
  （キュー使用率）/ `rtos_timeline_overhead`（フックの負荷）。jobs は応答時間、inversions は正確な区間になる。
- ヒープ: 止めて `heap_scan`（FreeRTOS heap_4/5・newlib・newlib-nano。使用量・断片化・壊れ = issues、compare で前回からの
  増減）。リークは `heap_trace_start`（malloc / free の入口にブレークポイント。止めずに記録）→ 走らせる →
  `heap_trace_report`（live_by_caller = 呼び出し元ごとの生きている確保と、時点ごとの数）/ `heap_trace_events` → `heap_trace_stop`。
- メモリ・スタック: `debug_elf_size`（領域ごとの使用率・大きいシンボル。領域は linker_script / map_file か接続中のメモリマップ）、
  `debug_stack_static`（静的な最大。.su と呼び出しグラフ。再帰・関数ポインタは上限不明 = indirect で候補を渡す）。
  ベアメタルの MSP は止めて `target_stack_paint` → 走らせる → `target_stack_usage`（使った最大・あふれ）。
- 性能解析: `profile_start`（method itm は core_clock_hz が要る。pcsr は SWO 不要）→ `profile_stop` → `profile_result`
  （by function / line / instruction。サンプル数と割合）/ `profile_exceptions`（割り込みの時間）/ `profile_coverage`
  （近似）。記録の 2 点間の時間のばらつき・長い順は `timing_measure`（条件は `recording_find` と同じ）。
  区間（t0_s / t1_s、pcsr は記録の時間軸の t0 / t1）に限る集計・関数のグループ（by file / prefix / groups）は
  `profile_aggregate`、CPU 使用率の推移は `profile_timeline`、gprof / KCacheGrind / lcov / gcov / dot へは `profile_export`。
  テストポイントは `testpoint_aggregate`（区間）/ `testpoint_export`（callgrind・lcov の C0 / C1 等）。
- 記録の時刻のコードの位置: `recording_code_at`（t = タイムベース）で、その時刻の直前・直後の PC のサンプル（記録中に
  `profile_start` の method pcsr で DWT_PCSR を読んでおく）の行と t との差・その間の幅、t の時点で記録された状態と前後の
  止まった出来事（止まった PC は正確）、動いていたタスク。行は推定しない（サンプルの間に何を実行したかは記録に無い）。
  材料の無いものは missing の理由。
- 記録の集計・検索は `recording_query`（読み取り専用の SQL。表 events(t, t_end, stream, kind, a, b, body, unc)。時刻 t は
  タイムベース（recording_status の timebase_hz、多くは 100 MHz）。body は json_extract で。a・b の意味は stream ごと:
  sample = a 番地・b 値（32 ビット）、fpga の kind value = a 組・b 生の値（信号は fpga_obs_signals_get の lsb・width で
  (b >> lsb) & ((1 << width) - 1)）、dap = a 番地・b データ、uart = b バイト。fpga の kind hidden = スナップショット型で
  標本の間に変わって見えなかった変化の数（a）、gap = 欠落、events_error（annotation）= 読み出しの失敗で欠けた所）。
  時刻の単位: system_status の time_us・fpga_obs_read の time_us は µs、記録・ui_state の view / cursors はタイムベース、
  recording_correlate の delay_s と window は秒。人に見せる時間軸は
  `recording_export`（format perfetto → ui.perfetto.dev で開く。t0 / t1・streams で絞る）。調査の仮説・結論は
  `recording_mark`（t を指定すると、開いた記録のその時刻にも置ける。人の画面の Event Log に並ぶ）。
- MCU と FPGA が連携するシステム（システム解析）: 対象の FPGA に観測の回路（target_ip/fprobe_obs）を入れ、
  `fpga_obs_open`（標本型の極小の FPGA は TCK ≤ 対象の clk / 8 の half_period・words 1・sync_period 0）→ `events_start` →
  `fpga_obs_signals_set`（信号の名前）。両側の今は `system_status`（MCU の番地の値と FPGA の各組の最新の値。番地は
  1 ワードずつ順に読むので、走っている MCU では同じ瞬間の値ではない: 複数ワードの組を揃えて見るなら記録して
  時刻で突き合わせる）。人の画面の
  MCU のメモリの表と FPGA の条件は `ui_state_get` の system（`ui_state_set` の system で人の画面に足せる）。時刻で
  突き合わせるには `recording_start` → `watch_set`（MCU のカウンタ等の番地、names で名前。記録の前から動いている watch の名前も記録に入る）→
  `recording_la_stream_start`（ピン）→ 数秒 → 止める → `recording_correlate`（effect 例 "errors changed"、causes 例
  "restart changed and state=BUSY"・"in0 rose"。省くと記録の変数から自動で候補を作る。no_values = 範囲の中に値が 1 件も
  無い変数（止まった watch・記録していないピン等。0 回は「原因でない」ではない）。reads = MCU のメモリの読みの事実
  （回数・間隔の分位・いちばん長い空白の時刻。結果の時刻は読みの間隔だけ遅れて見える）。gaps = データの欠け（reason:
  reads_missing MCU の読みの空白（gap_s を明示したときだけ。それより長い空白）・read_failed 読めなかった語・pins_missing ピンの無い区間・fpga_lost FPGA の欠落・
  events_error 取り込みの失敗・recording_dropped 長期の記録の間引き。その間の結果・候補は数えられていない）。同じ時刻に成立する自動の候補は
  1 つにまとめ same に。fpga_hidden = スナップショットの間で見えなかった FPGA の変化の数）。返すのは測った事実だけで、
  判定・経験則の順位づけはしない: preceded（結果の直前に候補があった割合）と coverage（記録のうち候補の後 window 以内に
  当たる時間の割合）を並べて、どう読むかは自分で決める（どちらも 1 に近い候補は、いつも起きているだけかもしれない）。
  上位の候補は例の時刻（examples）で実際の波形・値を見て確かめる。
  数字を読む前に欠けを確かめる: 記録中は `recording_status` の watch_running・events_errors、解析の結果の gaps・warnings・
  judged・excluded（欠けの間の結果は数えていない。読みの空白の before / after で動いた量は分かる）。
  めったに起きない不具合は、記録中に `recording_trigger_set`（条件が成り立つたびに印、action stop なら post_s 後に記録を
  止める）で前後つきで残す。区間（リクエスト等）ごとの集計は `recording_transactions`（開始の条件で区間に切り、開始時の値ごとに結果の割合・
  条件の回数。by_at "end" なら区間の終わりの値で分ける: ファームウェアが要求の最後に書く値等）。watch は `watch_status`
  の running で動いているか（watch_stop の後も一覧は残る）。値が続いた時間の分布は `recording_durations`（例: FPGA の state が BUSY の長さと、ファームウェアの待ち時間を比べる）。
  例の時刻は `ui_state_set`（view と cursors の reference = 候補・selection = 結果）で人の画面に見せる。
  成り立った瞬間の MCU の中を見るには `fpga_obs_trigger_set`（FPGA の条件でハードのクロストリガ。halt で MCU を止める。遅延は
  SWD 4 MHz で約 64 µs）→ `target_status` / `debug_backtrace` 等 → `fpga_obs_trigger_clear`。成り立った瞬間の前後のピンは
  `fpga_obs_trigger_set(halt=false, la=true)` → `la_deep_capture(mask=1, value=1, fall=1, rle=false)`（波形では成り立たない
  条件 = クロストリガだけで捕捉。内蔵の捕捉がある構成は `la_capture`）→ `fpga_obs_trigger_clear`。観測が stuck
  （対象の FPGA を書き直した等）なら `fpga_obs_status` の config のまま `fpga_obs_open` し直す（FPGA を作り直しながら
  見続けるなら auto_reopen=true で自動に）。
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
- 通信の解読: 捕捉の後 `la_decode`（protocol と settings。通信の設定（枠の値・線の割り当て）は既定を持たないので
  すべて渡す。つながない線は null。例 uart は {"channel": 5, "baud": 115200, "data_bits": 8, "parity": "none",
  "stop_bits": 1, "invert": false, "lsb_first": true}、swd は {"swclk": 0, "swdio": 3, "drive": 8, "turnaround": 1,
  "orundetect": false}。足りなければ誤りが要る設定の名前を挙げる）。errors_only で誤りだけ、件数が多ければ
  offset / limit。解読器と設定（required・nullable）は `la_decoders` で一覧（CAN/CAN FD・1-Wire・JTAG・I2S・
  パラレル・Manchester 等）。積み重ね（spiflash・eeprom24・modbus・jtag_arm）は下の設定を下の名前のキーに（例 modbus
  は {"uart": {…uart の設定…}}、spiflash は {"spi": {…}, "address_bytes": 3}）。足りない解読器は Python で書いて
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


FIRST_EDGES = 16                       # la_collect の要約で時刻を並べる先頭の変化の数（切ったかは結果に出す）


def _channel_summary(cap) -> dict:
    """{channels: {ch: {initial, edges, first_edges_ns（トリガ = 0 ns からの先頭 FIRST_EDGES 個）, first_edges_truncated}},
    first_edges_limit, constant_low（常に 0 で省いたチャンネル）}。"""
    period_ns = cap.sample_period_s * 1e9
    out, low = {}, []
    for ch in range(cap.channels):
        bits = cap.channel(ch)
        edges = [i for i in range(1, len(bits)) if bits[i] != bits[i - 1]]
        if not edges and not bits[0]:
            low.append(ch)                                 # 常に 0 のチャンネルは省く（どれを省いたかは返す）
            continue
        out[ch] = {
            "initial": bits[0],
            "edges": len(edges),
            "first_edges_ns": [round((i - cap.trigger) * period_ns) for i in edges[:FIRST_EDGES]],
            "first_edges_truncated": len(edges) > FIRST_EDGES,
        }
    return {"channels": out, "first_edges_limit": FIRST_EDGES, "constant_low": low}


class Probe(InspectTools, ObserveTools, WatchTools, RtosTools, ProfileTools, SwoTools, CyclesTools, WindowTools,
            BreakpointActions, AnalysisTools, GdbTools, GdbTraceTools, FlashTools, TestpointTools, SnapshotTools, ExecutionTools,
            RtosAnalysisTools, RulesTools, HookTools, FlashBreakpointTools, DecodeTools, UserTools,
            LongRecordingTools, HeapTools, LowLevelTools, ConnectTools, ElfWatchTools, StreamTools, FormatTools,
            ProfileExportTools, StackTools, TimelineTools, ElfFileTools, TerminalTools, UiStateTools, DesignTools,
            MonitorModeTools, XtrigOutputTools, GpioTools, SafetyTools, MulticoreTools, EtmTools, LinuxTools,
            RecordingRtosTools):

    def __init__(self, serial: str | None = None, port: str | None = None, baud: int = DEFAULT_BAUD,
                 eth: str | None = None, bitstreams: str | None = None):
        self._serial = serial
        self._port, self._baud = port, baud               # port を指定すると SerialLink で開く
        self._eth = eth                                   # IP アドレスを指定すると EthLink で開く
        self._lock = threading.RLock()
        self._voltage_history = VoltageHistory()
        self._session: Session | None = None
        self._debug = None
        self._symbols: Symbols | None = None                # elf_load / program_ram / flash（ELF）で読み込む
        self._dbg: Debugger | None = None
        self._capture_id = 0                                    # 最後の捕捉（画面の波形 la_view 用）
        self._capture: Transitions | None = None
        self._capture_lock = threading.Lock()             # 捕捉・情報・変化点の表をそろえて読み書きする（読みのスレッド）
        self._capture_info: dict | None = None
        self._capture_names: dict[int, str] = {}                # 最後の捕捉の ch の既定の名前（書き出し用）
        self._wave: Waveform | None = None                     # 変化点の表は画面が最初に求めたときに作る
        self._recorder: Recorder | None = None                 # 記録中の共通の時間軸の記録（F3）
        self._collector: Collector | None = None
        self._view: Recording | None = None                    # 読む対象の記録（記録中のもの、または開いたもの）
        self._view_lock = threading.Lock()                     # _view の差し替えと前の記録を閉じるのを 1 つに
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
        self._init_linux()

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
        self._close_linux()                               # Linux のターゲットは機器と無関係（USB の開き直しでは閉じない）
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
        """ターゲットの電源電圧（補助マイコン Pico の ADC、V）。live = window_s 秒待つ間に計測値の数が増えた
        （samples_before → samples）。over_range が True なら測れる上限を超えている（volts は None。配線の分圧を確認）。"""
        with self._lock:
            reading = Aux(self.session.device).read().as_dict()
        self._voltage_history.add(reading)
        return reading

    def probe_voltage_history(self, since: float | None = None) -> dict:
        """デーモンが読んだ電圧（だれの probe_voltage でも）の履歴: since（UNIX 秒）より後の読み {time, volts,
        over_range, live} と統計（fprobe.voltage.VoltageHistory.read）。機器には触れない。"""
        return self._voltage_history.read(since)

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
        asked = {"target": target, "frequency_hz": frequency_hz, "connect_mode": connect_mode, "jtag_chain": jtag_chain,
                 "script": script, "core": core}
        switches = []
        auto = None
        if target == "auto":
            target, auto = self._resolve_auto_target(frequency_hz, switches)
            frequency_hz = auto["frequency_hz"]               # 判別で DP が応答した速さ（頼まれた速さ）
            if jtag_chain is None:
                jtag_chain = auto["jtag_chain"]
        if riscv_part(target) is None and arm_a_part(target) is None:
            target, _, core = resolve_target(target, core)
        switch = self._ensure_design(protocol_for_target(target, jtag_chain))
        if switch is not None:
            switches.append(switch)
        status = self._connect_target(target, frequency_hz, connect_mode, jtag_chain, script, core)
        if status.get("state") != "HALTED":
            with self._lock:
                self._clear_halt_reason()          # 前のツール・前の停止が残した DFSR で、次の停止の理由を誤らない
        self._last_connect = asked                 # 同じ設定でつなぎ直す（対象のリセットで SWD がはずれた後。system_status）
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
        """コアの状態（RUNNING / HALTED 等）と、停止中なら PC と止まった理由 halt_reason（request = 停止要求・ステップ・
        クロストリガ / breakpoint / bkpt_instruction = ファームウェア自身の BKPT（panic・assert 等）/ watchpoint /
        vector_catch / external。fprobe.fault.halt_reason）と、DFSR の立っていたビット全部 halt_bits（HALTED / BKPT /
        DWTTRAP / VCATCH / EXTERNAL。複数立っていても halt_reason は 1 語）。つないでいる先（target・frequency_hz・wire）も。
        走っているのに致命的エラーで回り続けていれば fatal（fprobe.probe_safety）。複数ドロップの部品（RP2040）なら
        core（接続中のコア）と cores（各コアの状態。fprobe.probe_multicore）。"""
        with self._lock:
            state = self.target.get_state().name
            status = {"state": state, **(self._link or {})}
            if state == "HALTED":
                pc = self.target.read_core_register("pc")
                status["pc"] = pc
                status.update(self._place_of(pc))
                status.update(self._halt_reason(pc))
            if self._monitor is not None:
                status["debug_mode"] = "monitor"              # 止まっているのは DebugMon の中（優先度の高い割り込みは動く）
            fatal = self._fatal_state(state)
            if fatal is not None:
                status["fatal"] = fatal
            return self._multicore_status(status)

    def _caps(self):
        """ケイパビリティ（最後に読んだもの。無ければ読む）。capabilities() は呼ぶたびに機器から読み直す（UDP の
        リンクで何往復もする）ので、毎秒の読み（system_status・fpga_obs_read 等）はこちらで。構成の切り替え・
        つなぎ直しでは capabilities() が読み直して last_capabilities も替わる。"""
        device = self.session.device
        cached = getattr(device, "last_capabilities", None)
        if cached is not None:
            return cached
        with self._lock:                                      # 読み直すときは機器を使う（ほかのスレッドと混ぜない）
            return device.capabilities()

    def _halt_reason(self, pc: int) -> dict:
        """DFSR（と BKPT なら PC の命令）から止まった理由（_lock の中で）: halt_reason（1 語）と、DFSR の立っていた
        ビット全部の halt_bits。Cortex-M 以外・読めなければ空。"""
        if not _cortex_m(getattr(self, "_link", None)):
            return {}
        try:
            dfsr = self.target.read32(fault.DFSR)
            instruction = self.target.read16(pc & ~1) if dfsr & 0x02 else None
        except Exception:                                   # noqa: BLE001  理由は状態の添え物。読めなくても状態は返す
            return {}
        dbg = self._dbg                                     # 置いたブレークポイント（無ければまだ置いていない）
        reason = fault.halt_reason(dfsr, instruction, breakpoint=dbg is not None and pc in dbg.breakpoints)
        return {**({"halt_reason": reason} if reason is not None else {}), "halt_bits": fault.dfsr_bits(dfsr)}

    def _clear_halt_reason(self) -> None:
        """DFSR をクリアする（W1C。pyOCD は自分の再開・ステップでだけクリアする）。Cortex-M 以外は何もしない。"""
        if not _cortex_m(getattr(self, "_link", None)):
            return
        try:
            self.target.write32(fault.DFSR, 0x1F)
        except Exception:                                   # noqa: BLE001  理由の表示のための片付け。失敗しても操作は続ける
            pass

    def target_halt(self, core: int | None = None) -> dict:
        """コアを停止する。core: 複数ドロップの部品（RP2040）で接続していないコアを止める（その状態を返す）。"""
        with self._lock:
            if core is not None:
                other = self._other_core(core, "halt")
                if other is not None:
                    return other
            if self.target.get_state().name != "HALTED":
                self._clear_halt_reason()                   # 前の停止の理由（リセットでは消えない）を混ぜない
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
        周辺も）/ core（VECTRESET。コアだけ、ARMv7-M だけ）/ hardware（nRESET の線）/ emulated / rescue（RP2040 の
        救済の DP でチップ全体。つなぎ直して起動させる。halt は使わない。fprobe.multidrop）。"""
        with self._lock:
            if kind == "rescue":
                self._rescue()
                self._watch_stops(True)
                return {**self.target_status(), "kind": kind}
            self._clear_halt_reason()                       # DFSR はリセットで消えない（電源投入だけ）
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
        """読み込んだ ELF の行番号の情報が参照するソース（名前・場所・この PC にあるか）。同じ名前の別のファイルは
        files には最初のものだけで、same_name に名前 → 場所の一覧（"ファイル:行" はディレクトリを添えて選ぶ）。"""
        with self._lock:
            if self._symbols is None:
                return {"files": [], "same_name": {}}
            relocated = self._symbols.relocated()
            return {"files": [{"name": name, "path": str(path), "exists": not is_network_path(path) and path.is_file(),
                               **({"built_path": str(relocated[name]["built"])} if name in relocated else {})}
                              for name, path in sorted(self._symbols.sources().items())],
                    "same_name": {name: [str(p) for p in paths]
                                  for name, paths in sorted(self._symbols.source_conflicts().items())}}

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
        """場所（関数名・変数名・"ファイル:行"・"0x..."）の番地と、その番地の関数名・ファイル:行（where の文字と、
        欄 function・offset・file・line。分からない欄は None）。"""
        with self._lock:
            if self._symbols is None:
                raise RuntimeError("ELF が読み込まれていない（elf_load）")
            address = self._symbols.resolve(location)
            place = self._symbols.describe(address)
            out = {"address": address, "where": str(place), "function": place.function, "offset": place.offset,
                   "file": place.file, "line": place.line}
            if not location.startswith("0x") and not is_line_location(location):
                s = self._symbols.symbol(location)
                out.update(kind=s.kind, size=s.size)
            return out

    def program_ram(self, path: str, vector_table: int | None = None) -> dict:
        """ELF を RAM に読み込み、起動できる状態で止める（Flash には書かない。RAM で動く試験用のプログラム向け）。
        vector_table（ベクタ表の番地。使う側が明示する）を渡せばその表の初期の MSP・リセットの処理から（VTOR も
        設定）、渡さなければ ELF の入口（e_entry）から（SP・VTOR は変えない。ベクタ表の場所を推定しない）。
        ターゲットはリセットされる。続けて run_until_halt / target_resume で走らせる。結果の start = {from:
        vector_table / entry, entry, vector_table}。"""
        with self._lock:
            self.elf_load(path)
            stop, start = self.dbg.load_to_ram(vector_table)
            self._image_written(elf=True)
            return {**self._stop_dict(stop), "start": start}

    def breakpoint_set(self, location: str, condition: str | None = None, ignore_count: int = 0,
                       log: list[str] | None = None, message: str | None = None) -> dict:
        """ブレークポイントを置く。location: 関数名・"ファイル:行"・"0x..."。condition: C の式（偽なら止まらない。
        ローカル変数も使える）、ignore_count: 最初の n 回は止まらない、log: 当たるたびに読む式の一覧（止まらずに
        breakpoint_log と記録に残す。message は添える文）。動作のあるブレークポイントは当たるたびにコアを数 ms 止める。"""
        with self._lock:
            address = self.dbg.breakpoint(location if not location.startswith("0x") else int(location, 16))
            self._disabled.pop(address, None)                       # 無効にしてあったものは置き直しで有効に
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
                if address not in self._disabled:
                    self.dbg.remove_breakpoint(address)
                self._drop_actions(address)
            return self.breakpoint_list()

    def breakpoint_list(self) -> dict:
        """置いているブレークポイント（enabled = false は breakpoint_enable で無効にしたもの。機器には置いていない）と
        ウォッチポイント。"""
        with self._lock:
            placed = [{"address": a, "location": n, "enabled": True, **self._place_of(a), **self._breakpoint_kind(a),
                       **({"action": self._actions[a].describe()} if a in self._actions else {}),
                       **({"tool": self._break_hooks[a][0]} if a in self._break_hooks else {})}
                      for a, n in self.dbg.breakpoints.items()]
            disabled = [{"address": a, "location": n, "enabled": False, **self._place_of(a),
                         **({"action": action.describe()} if action is not None else {})}
                        for a, (n, action) in self._disabled.items()]
            return {"breakpoints": sorted(placed + disabled, key=lambda b: b["address"]),
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

    def backtrace(self, variables: bool = False, max_frames: int = 64, max_elements: int = 64,
                  assume_leaf: bool = False) -> list[dict]:
        """呼び出し履歴（ELF に CFI があれば CFI で巻き戻し、例外のフレームも越える。無ければいまの位置と戻り先）。
        各段は index・pc・where・note（"exception" = 例外で中断された位置）・cfa・function・offset・file・line。
        CFI で巻き戻したときは最後の段に stop（その先へ巻き戻さなかった理由と、上限で切ったなら使った max_frames）。
        assume_leaf なら CFI の無い最内の関数を葉と仮定して LR から呼び出し元を出す（その段の note は lr_without_cfi）。
        variables なら各段の引数とローカル変数（arguments・locals）も。停止中に呼ぶ。"""
        with self._lock:
            return self._backtrace(variables, max_frames, max_elements, assume_leaf)

    @staticmethod
    def _stop_dict(stop: Stop) -> dict:
        out = {"reason": stop.reason, "pc": stop.pc, **Probe._place(stop.where), "elapsed_s": stop.elapsed_s}
        if stop.halt_bits is not None:
            out["halt_bits"] = stop.halt_bits
        return out

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
               pulse: dict | None = None, external_only: bool = False) -> dict:
        """捕捉を仕掛ける（すぐ戻る）。条件が何もなければ即トリガ。external_only = true ならクロストリガ（xtrig_route の
        to_la・fpga_obs_trigger_set の la）でだけ捕捉する（パターンとは一緒に指定できない）。
        filter_channels（外の信号の ch = 拡張ヘッダのピンと専用の入力のビット）は filter_ns より短い変化を捨てる
        （グリッチフィルタ。フィルタした ch は filter.delay_ns 遅れて記録される）。pulse はパルス幅のトリガ {"channel", "level"（既定 1 = High の脈）,
        "min_ns", "max_ns"（省略で上限なし）, "timeout"（true なら level が min_ns を超えて続いた時点で成立）}。
        パルス幅はパターン（mask・rise・fall）と両方満たしたときに成立。ケイパビリティ la_filter / la_pulse が要る。"""
        with self._lock:
            self._la_idle()
            self._la = LogicAnalyzer(self.session.device)
            self._la_period = self._la.start(rate_hz=rate_hz, pretrigger=pretrigger, mask=mask, value=value,
                                             rise=rise, fall=fall, filter_channels=filter_channels,
                                             filter_ns=filter_ns, pulse=pulse, external_only=external_only)
            return {"sample_period_ns": self._la_period * 1e9, "depth": self._la.depth, "pretrigger": pretrigger,
                    **self._la.front_info}

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
                    "trigger_time_span": self.session.device.capabilities().la_phase_span,
                    "sample_period_ns": cap.sample_period_s * 1e9, "samples": len(cap.samples),
                    **_channel_summary(cap), "vcd": vcd_path}

    def _keep_capture(self, tr: Transitions, source: str, names: dict[int, str] | None = None) -> None:
        """最後の捕捉を画面の波形（la_view）のために持つ。変化点の表はここでは作らない（画面を使わない操作を遅くしない）。
        names は ch の既定の名前（捕捉した機器の申告 LA_LAYOUT か、読み込んだファイルの名前。書き出しに使う）。"""
        if self._recorder is not None:
            add_capture(self._recorder, tr)                       # 記録中なら共通の時間軸の記録にも入れる
        with self._capture_lock:                                  # 捕捉・情報・表をそろえて替える（_capture_wave）
            self._capture_id += 1
            self._capture, self._wave = tr, None
            self._capture_info = {"id": self._capture_id, "length": tr.length,
                                  "sample_period_ns": tr.sample_period_s * 1e9, "trigger_index": tr.trigger,
                                  "channels": tr.channels, "source": source}
            self._capture_names = dict(names or {})

    def _capture_wave(self) -> tuple[Waveform, dict]:
        """最後の捕捉の変化点の表と情報（画面の波形・タイル用。表は最初に求められたときに作る）。読みのスレッドから
        呼ばれるので、機器の操作のロック（_lock。la_collect 等が長く持つ）ではなく捕捉の小さなロックで読む。"""
        with self._capture_lock:
            if self._capture is None:
                raise NoData("捕捉が無い（la_capture / la_deep_capture を先に）")
            tr, info, wave = self._capture, self._capture_info, self._wave
        if wave is None:
            wave = Waveform(tr)                                       # 長い捕捉では時間がかかるので、ロックの外で作る
            with self._capture_lock:
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

    def la_trigger_status(self) -> dict:
        """ロジアナのトリガの条件と状態（la ブロックのレジスタの読み戻し。la_arm・la_deep_arm が書いたもの）:
        mask・value・rise・fall、external_only（パターンが決して成り立たない形 = クロストリガでだけ捕捉。la_arm の
        external_only が書く形そのもの）、rate_hz・pretrigger、filter（ケイパビリティ la_filter）・pulse（la_pulse）、
        internal（内蔵バッファの捕捉の状態: capturing・triggered・done・trigger_time）・deep（深いバッファの同じもの。
        外部メモリがあれば）。trigger_time はトリガした後だけ（それまで None）。トリガの回数を数えるレジスタは無い。"""
        with self._lock:
            dev = self.session.device
            caps = dev.capabilities()
            if not getattr(caps, "la_channels", 0):
                raise RuntimeError("このデバイスにはロジックアナライザが無い")
            rate, pretrigger, mask, value, rise, fall = dev.read(g.LA_RATE, 6)
            out: dict = {"mask": mask, "value": value, "rise": rise, "fall": fall,
                         "external_only": (mask, value, rise, fall) == external_pattern(True, 0, 0, 0, 0, None)
                         and not (getattr(caps, "la_pulse", False) and dev.read32(g.LA_PULSE) & 3),
                         "rate_hz": caps.la_sample_hz / ((rate & 0xFFFF) + 1), "pretrigger": pretrigger}
            clock = caps.sys_clock_hz
            if getattr(caps, "la_filter", False):
                word = dev.read32(g.LA_FILTER)
                cycles = word >> g.LA_FILTER_CYCLES[0] & (1 << g.LA_FILTER_CYCLES[1]) - 1
                out["filter"] = {"channels": word >> g.LA_FILTER_CHANNELS[0] & (1 << g.LA_FILTER_CHANNELS[1]) - 1,
                                 "cycles": cycles, "min_width_ns": cycles / clock * 1e9 if cycles > 1 else None}
            if getattr(caps, "la_pulse", False):
                word, low, high = dev.read(g.LA_PULSE, 3)
                mode = ("none", "width", "timeout", "none")[word >> g.LA_PULSE_MODE[0] & 3]
                out["pulse"] = {"mode": mode} if mode == "none" else {
                    "mode": mode, "level": word >> g.LA_PULSE_LEVEL[0] & 1,
                    "channel": word >> g.LA_PULSE_CHANNEL[0] & (1 << g.LA_PULSE_CHANNEL[1]) - 1,
                    "min_ns": low / clock * 1e9, "max_ns": None if high == 0xFFFF_FFFF else high / clock * 1e9}

            def state(status_addr, fields, time_addr) -> dict:
                status = dev.read32(status_addr)
                lo, hi = dev.read(time_addr, 2)
                bits = {k: bool(status >> f[0] & 1) for k, f in fields.items()}
                return {**bits, "trigger_time": hi << 32 | lo if bits["triggered"] else None}
            if getattr(caps, "la_depth", 0):
                out["internal"] = state(g.LA_STATUS, {"capturing": g.LA_STATUS_CAPTURING, "triggered": g.LA_STATUS_TRIGGERED,
                                                      "done": g.LA_STATUS_DONE}, g.LA_TRIGGER_TIME_LO)
            if getattr(caps, "mem_bytes", 0):
                out["deep"] = state(g.LA_DEEP_STATUS, {"capturing": g.LA_DEEP_STATUS_CAPTURING,
                                                       "triggered": g.LA_DEEP_STATUS_TRIGGERED,
                                                       "done": g.LA_DEEP_STATUS_DONE}, g.LA_DEEP_TRIGGER_TIME_LO)
            return out

    def la_capture(self, timeout_s: float = 5.0, vcd_path: str | None = None, **trigger) -> dict:
        armed = self.la_arm(**trigger)
        return {**self.la_collect(timeout_s, vcd_path), **{k: armed[k] for k in ("filter", "pulse", "pretrigger")
                                                            if k in armed}}

    # --- クロストリガ ----------------------------------------------------------------

    def xtrig_route(self, to_la: list[str] | None = None, to_inject: list[str] | None = None) -> dict:
        """源（"la_trigger" / "dap_match" / "host" / "sequence" = 多段トリガ / "obs_match" = FPGA の観測の値の一致（fpga_obs_trigger_set） / "sampler_match" = サンプラの値の一致（sampler_trigger_set））を動作につなぐ。to_la: ロジアナをトリガする源、
        to_inject: 割り込み書き込み（既定はターゲット停止）をする源。指定しない動作は切る。"""
        def flags(names):
            return Source(sum(Source[n.upper()] for n in names or []))
        with self._lock:
            caps = self.session.device.capabilities()
            for name in {*(to_la or []), *(to_inject or [])} & {"obs_match", "sampler_match"}:
                if not getattr(caps, name, False):
                    raise RuntimeError(f"この構成には源 {name} が無い（ケイパビリティ {name}）")
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
        """多段トリガの状態（レジスタの事実）: 今の段、発火したか、発火した時刻（タイムベースのカウント）、仕掛けているか
        （armed）、設定した段の数（stages）と、段ごとの状態 stage_states（done = 済んだ・current = 待っている段・
        waiting = まだ・idle = 仕掛けていない。発火したら全部 done）。config は段とピンの条件のレジスタの読み戻し
        （xtrig_set_sequence の stages・pattern_a・pattern_b と同じ形。within_us は刻みに丸めた値、制限なしは None）。"""
        with self._lock:
            seq = TriggerSequencer(self.session.device)
            stage, fired, time_ = seq.status()
            armed, stages = seq.control()
            configured, a, b = seq.config(stages)
        states = ["done" if fired else "idle" if not armed else
                  "done" if k < stage else "current" if k == stage else "waiting" for k in range(stages)]
        names = {Event.PATTERN_A: "pattern_a", Event.PATTERN_B: "pattern_b", Event.DAP_MATCH: "dap_match",
                 Event.HOST: "host"}
        config = {"stages": [{"when": [n for e, n in names.items() if st.when & e], "count": st.count,
                              "within_us": None if st.within_s is None else st.within_s * 1e6} for st in configured],
                  "pattern_a": vars(a), "pattern_b": vars(b)}
        return {"stage": stage, "fired": fired, "time": time_, "armed": armed, "stages": stages,
                "stage_states": states, "config": config}

    def xtrig_fire(self) -> None:
        """源 host を 1 回発火する。"""
        with self._lock:
            CrossTrigger(self.session.device).fire()

    def xtrig_status(self) -> dict:
        """割り込み書き込みの状態（結果・回数・時刻）と中身（ap・address・value。レジスタの読み戻し）、番地一致の回数・
        時刻（タイムベースのカウント）と条件（address・address_mask・data・data_mask・on_write・on_read。読み戻し）、いまの経路 route
        （レジスタから読んだ to_la・to_inject の源の名前。xtrig_route 以外の道具（fpga_obs.trigger_set・sampler.trigger_set・
        ルール）が書いたビットも含む）。"""
        with self._lock:
            xt = CrossTrigger(self.session.device)
            count, time_ = xt.match_status()
            match = xt.match_config()
            ap, address, value = xt.inject_config()
            route = self.session.device.read32(g.XTRIG_ROUTE)

            def names(field) -> list[str]:
                bits = route >> field[0] & (1 << field[1]) - 1
                return [s.name.lower() for s in Source if bits & int(s)]
            return {"inject": {**vars(xt.inject_status()), "ap": ap, "address": address, "value": value},
                    "match": {"count": count, "time": time_, **match},
                    "route": {"to_la": names(g.XTRIG_ROUTE_TO_LA), "to_inject": names(g.XTRIG_ROUTE_TO_INJECT)}}

    def la_deep_capture(self, rate_hz: float | None = None, mask: int = 0, value: int = 0, rise: int = 0,
                        fall: int = 0, rle: bool = True, before_beats: int = 4096, after_beats: int = 4096,
                        timeout_s: float = 10.0, vcd_path: str | None = None, filter_channels: int = 0,
                        filter_ns: float = 0.0, pulse: dict | None = None, external_only: bool = False) -> dict:
        """深いバッファ（外部メモリ 128 MB）で捕捉して要約を返す。トリガ条件（グリッチフィルタ・パルス幅を含む）は
        la_capture と同じ。rle = True は変化点圧縮（遅い信号なら数秒〜数十秒分）、False は RAW（100 MS/s で最大 0.67 秒）。
        読むのはトリガの前 before_beats 拍・後 after_beats 拍（1 拍 = RAW 8 サンプル / RLE 4 変化点）。
        ターゲット操作と同時に使うときは la_deep_arm → 操作 → la_deep_collect。"""
        armed = self.la_deep_arm(rate_hz, mask, value, rise, fall, rle, after_beats, filter_channels, filter_ns, pulse,
                                 external_only)
        result = self.la_deep_collect(timeout_s, before_beats, vcd_path)
        return {**result, **{k: armed[k] for k in ("filter", "pulse", "after_beats") if k in armed}}

    def la_deep_arm(self, rate_hz: float | None = None, mask: int = 0, value: int = 0, rise: int = 0, fall: int = 0,
                    rle: bool = True, after_beats: int = 4096, filter_channels: int = 0, filter_ns: float = 0.0,
                    pulse: dict | None = None, external_only: bool = False) -> dict:
        """深いバッファの捕捉を仕掛ける（すぐ戻る）。filter_channels・filter_ns・pulse・external_only は la_arm と同じ。"""
        with self._lock:
            self._la_idle()
            self._deep = DeepAnalyzer(self.session.device)
            if self._collector is not None:
                self._collector.pause_events(True)                # 深いバッファの捕捉中はイベント記録を読めない
            period = self._deep.start(rate_hz=rate_hz, mask=mask, value=value, rise=rise, fall=fall, rle=rle,
                                      post_beats=after_beats, filter_channels=filter_channels, filter_ns=filter_ns,
                                      pulse=pulse, external_only=external_only)
            return {"sample_period_ns": period * 1e9, "capacity_beats": self._deep.beats, "after_beats": after_beats,
                    **self._deep.front_info}

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
                    "trigger_time_span": self.session.device.capabilities().la_phase_span,
                    "sample_period_ns": tr.sample_period_s * 1e9, "samples": tr.length,
                    "duration_s": tr.length * tr.sample_period_s, "before_beats": before_beats,
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
                 "elf": str(self._symbols.path) if self._symbols else None,
                 "fpga.signals": self.ui_state_get()["fpga_signals"], "pins": _channel_list(caps)},
                segment_s, segment_bytes, max_bytes, decimate_after_s, decimate_streams)
            try:
                self._collector = Collector(device, self._recorder, lock=self._lock)
                for k, v in getattr(self, "_obs_delays", {}).items():    # FPGA の観測を先に開いていれば同じ遅れで
                    setattr(self._collector.obs, k, v)
                if not events:
                    self._collector.events = None                     # イベント記録を取り込まない
                self._collector.start()
                self._set_view(Recording.open(directory))
            except BaseException:                                     # 途中で失敗: 記録中の状態を残さない
                collector, recorder = self._collector, self._recorder
                self._collector = self._recorder = None
                if collector is not None and collector._thread is not None:
                    collector._stop.set()
                recorder.close()
                raise
            # 前から動いている watch の名前も記録の変数の名前に（watch_set を記録の後に呼び直さなくても）
            if getattr(self, "_watch_running", False):            # 止めた watch の名前は写さない（値が入らない）
                self._name_watches({w.address: w.name for w in (getattr(self, "_watched", None) or [])
                                    if getattr(w, "address", None) is not None and w.name})
            return {"recording": directory.name, "path": str(directory)}

    def _name_watches(self, names: dict[int, str]) -> None:
        """記録中の記録の変数の名前（sample.names）に足す（_lock の中で）。読む記録が記録中の記録のときだけ、その情報も
        合わせる（記録中に別の記録を開いていれば、その古い記録の情報は書き換えない）。"""
        recorder = self._recorder
        if recorder is None or not names:
            return
        named = dict((getattr(recorder, "_meta", None) or {}).get("sample.names") or {})
        named.update({f"0x{a:08X}": n for a, n in names.items()})
        recorder.update_meta(**{"sample.names": named})
        view = self._view
        if view is not None and getattr(view, "directory", None) == recorder.directory and hasattr(view, "update_meta"):
            view.update_meta(**{"sample.names": named})

    def recording_stop(self) -> dict:
        """記録を止める（保存した記録は recording_list / recording_open で読める）。"""
        collector, recorder = self._collector, self._recorder
        if recorder is None:
            raise RuntimeError("記録していない")
        self._stop_la_record()                                    # 流し続けの残りを入れてから閉じる
        self._linux_before_recording_stop()                       # Linux の出来事の、組を待っている分を入れる
        collector.stop()                                          # ロックの外で（集める側がロックを取るため）
        with self._lock:
            recorder.close()
            self._recorder = self._collector = None
            view = self._view                                  # 記録中に別の記録を開いていれば、その件数ではない
            same = view is not None and view.directory == recorder.directory
            return {"recording": recorder.directory.name, "events": view.count() if same else None}

    def recording_status(self) -> dict:
        """記録中か、記録の名前・件数・時刻の範囲・ホストの出来事の時刻の不確かさ。記録中は watch_running（MCU のメモリの
        読みが動いているか。false なら記録にメモリが入らない）。"""
        rec, view = self._recorder, self._view
        out = {"recording": rec is not None, "name": view.directory.name if view else None}
        if view is not None:
            if rec is not None:
                rec.flush()
            out.update(events=view.count(), span=view.span(), timebase_hz=view.meta["timebase_hz"])
        if rec is not None:
            # MCU のメモリの読み（watch）が動いているか: 止まると記録に入らない（watch_set で再開。それまでは欠け）
            out["watch_running"] = bool(getattr(self, "_watch_running", False))
            try:
                out["clock_uncertainty_ticks"] = rec.clock.to_ticks(time.monotonic())[1]
            except RuntimeError:
                out["clock_uncertainty_ticks"] = None
        if view is not None:
            out["segmented"] = view.meta.get("layout") == "segmented"
        collector = self._collector                     # イベントの読み出しの失敗（取り込みは続く。欠落は lost）
        if collector is not None and collector.errors:
            out["events_errors"] = {"count": collector.errors, "last": collector.last_error, "lost": collector.lost}
        la_record = self._la_record                    # 別のスレッド（条件で止める等）が None にするので一度だけ読む
        if la_record is not None:
            out["la_stream"] = la_record.status()
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
        view = Recording.open(directory)
        self._set_view(view)
        return {"name": name, "events": view.count(), "span": view.span()}

    def _set_view(self, view: Recording) -> None:
        """読む記録を替え、前の記録を閉じる（開いたままだと Windows でフォルダを消せない。参照の循環で GC は遅い）。
        ponytail: 別のスレッドで読んでいる最中の前の記録の問い合わせは 1 回だけ失敗する（画面は次の要求で読み直す）。"""
        with self._view_lock:
            old, self._view = self._view, view
            if old is not None and old is not view:
                old.close()

    def _reading(self) -> Recording:
        view, recorder = self._view, self._recorder     # 別のスレッドが替える・None にするので一度だけ読む
        if view is None:
            raise NoData("読む記録が無い（recording_start か recording_open を先に）")
        if recorder is not None:
            recorder.flush()
        return view

    def recording_events(self, t0: int | None = None, t1: int | None = None, streams: list[str] | None = None,
                         kinds: list[str] | None = None, limit: int = 1000) -> dict:
        """時刻（タイムベース）の範囲の出来事を時刻順に。streams の例: waveform 以外の dap / uart / swo / rtt / aux /
        host / target / annotation / sample（a 番地・b 値・name 記録の変数の名前）/ fpga（kind value の a 組・b 生の値。fields に記録の信号の
        定義で名前ごとの値。fpga の value_untimed・gap_untimed・group_untimed は、時刻合わせの印が来ないまま鎖が終わって時刻の決まらない記録（t は受けた時 = 起きた時刻の上限））/ clock。
        先頭 limit 件（total = 範囲の総数・truncated = 切ったか・limit = 使った上限）。"""
        view = self._reading()
        signals = FpgaSignals.from_dict(view.meta.get("fpga.signals"))
        names = {int(a, 16): n for a, n in (view.meta.get("sample.names") or {}).items()}
        out = []
        for e in view.events(t0, t1, streams, kinds, limit):
            item = asdict(e)
            if e.stream == "sample" and e.a in names:
                item["name"] = names[e.a]
            if e.stream == "fpga" and e.kind == "value":
                item["fields"] = signals.fields(e.a, e.b)
            out.append(item)
        total = view.count_events(t0, t1, streams, kinds)
        return {"events": out, "total": total, "truncated": total > len(out), "limit": limit}

    def recording_correlate(self, effect: str, causes: list[str] | None = None, window_s: float | None = None,
                            t0: int | None = None, t1: int | None = None, examples: int = 5, limit: int = 20,
                            gap_s: float | None = None, max_coverage: float | None = None) -> dict:
        """結果の直前に多い出来事: 結果の条件 effect（例 "誤り changed"）の各時刻の直前（window_s 秒の中）に、causes の各条件
        （例 "restart changed and state=BUSY"・"in0 rose"）が来ていた割合（preceded）・記録のうち候補の後 window_s 以内に
        当たる時間の割合（coverage）・後に結果が来た候補の割合（followed）・遅れ・例の時刻。測った事実だけを返す（判定・
        経験則の順位づけをしない）。並びは preceded の大きい順、同じなら条件の文字の順。変数は MCU のメモリの名前
        （watch_set の names）・番地、FPGA の信号の名前、ピンの名前・ch<N>（同じ名前は mcu. / fpga. / pin. で区別）。
        項: <変数> changed / rose / fell、<変数>=<値>（列挙の名前も）を and でつなぐ。sampling = 変数の読みの間隔（秒。
        遅れの分解能）、fpga_hidden = 標本型で標本の間に見えなかった変化の数。causes を省くと記録の変数から候補を作って
        調べ（auto。信号の変化・列挙の値に入った瞬間・変化 かつ 列挙 = 値の組・ピンの rose / fell・ほかのメモリの変化）、
        上位 limit 個を返す（切る前の数 causes_total・切ったか truncated・使った limit。max_coverage を渡せば coverage が
        それを超える候補を除く）。gap_s を渡せば、それより長い
        MCU のメモリの読みの空白を欠けにする（既定は欠けにしない。読みの事実は reads）。gaps = 使った変数の
        データの欠け（時刻・reason。warnings にも合計）。judged = 先行率の分母（窓が候補の変数の欠けにかかる結果と、結果の変数の欠けの直後の読みで見えた結果は除く）。
        fpga_hidden = 標本型の FPGA の観測で、標本の間に変わって見えなかった変化の数（信号を問わない合計。多ければ
        FPGA の 1 回きりの変化を見落としうる。記録型なら 0）。sampling の null = 決まった読みの間隔が無い変数
        （FPGA の信号は変化を記録する）。effect.delta = 結果の MCU のメモリの範囲の最初と最後の値の差（count と違えば、1 回の
        読みで何回分も動いている）。"""
        return correlate(self._reading(), effect, causes, window_s, t0, t1, examples, limit, gap_s, max_coverage)

    def recording_trigger_set(self, condition: str, action: str = "mark", post_s: float = 1.0,
                              lag_s: float = 2.0) -> dict:
        """記録中に、条件（recording_correlate と同じ書き方。MCU のメモリ・FPGA の信号・ピンをまたげる）が成り立つたびに
        記録に印を付ける（stream annotation の mark）。action = stop なら、最初に成り立った時刻から post_s 秒が記録に
        入ったら記録を止める（めったに起きない不具合を前後つきで残す）。判定は PC で、今から lag_s 秒前までを調べる
        （流れ・取り込みが記録に届くのを待つ）。調べ終えた時刻より前に後から届いた出来事は調べず、その数を late に返す。
        記録の始めから調べる。様子は recording_trigger_status、やめるのは recording_trigger_clear。"""
        if action not in ("mark", "stop"):
            raise ValueError("action は mark / stop")
        if not post_s >= 0:
            raise ValueError("post_s は 0 以上")
        if not lag_s >= 0:
            raise ValueError("lag_s は 0 以上")
        with self._lock:
            if self._recorder is None:
                raise RuntimeError("記録していない（recording_start を先に）")
            watcher = ConditionWatcher(condition)                      # 条件の形はここで確かめる
            self._clear_trigger()
            # 記録中の記録を自分で開いて読む（画面・AI が別の記録を開いて読む記録 _view を替えても、見張りは替わらない）
            self._trigger = {"condition": watcher.condition.text, "action": action, "post_s": post_s, "lag_s": lag_s,
                             "watcher": watcher, "view": Recording.open(self._recorder.directory), "checked": 0, "hits": 0, "first_t": None, "last_t": None,
                             "state": "armed", "error": None, "late": 0, "_checked_count": 0}
            self._add_job("recording.trigger", TRIGGER_PERIOD_S, self._trigger_poll)
            return self.recording_trigger_status()

    def recording_trigger_status(self) -> dict:
        """条件・action・lag_s・回数 hits・最初と最後に成り立った時刻（タイムベース）・調べ終えた時刻 checked・
        調べ終えた後に届いて調べなかった出来事の数 late・状態（off / armed = まだ / fired = 成り立った / stopped = 記録を
        止めた / error）。"""
        trig = getattr(self, "_trigger", None)
        if trig is None:
            return {"state": "off"}
        return {k: v for k, v in trig.items() if k not in ("watcher", "view") and not k.startswith("_")}

    def recording_trigger_clear(self) -> dict:
        """条件の見張りをやめる。"""
        with self._lock:
            self._clear_trigger()
            return {"state": "off"}

    def _clear_trigger(self) -> None:
        self._remove_job("recording.trigger")
        trig = getattr(self, "_trigger", None)
        if trig is not None:
            trig["view"].close()
        self._trigger = None

    def _trigger_poll(self, now: int | None = None) -> None:
        """recording_trigger の周期の仕事（_lock の中で）: 前に調べた所から now（既定: 今 − lag_s）までを調べる。"""
        trig = getattr(self, "_trigger", None)
        if trig is None or trig["state"] in ("stopped", "error"):
            return
        if self._recorder is None:                                      # 記録が止まった: 見張りも終わる
            self._remove_job("recording.trigger")
            trig["view"].close()
            trig["state"] = "stopped" if trig["state"] == "fired" else "off"
            return
        hz = self._recorder.timebase_hz
        if now is None:
            now = self.session.device.time() - int(trig["lag_s"] * hz)
        try:
            self._recorder.flush()
            view = trig["view"]
            if trig["checked"]:                                         # 調べ終えた所より前に後から届いた出来事
                trig["late"] += view.count_events(t1=trig["checked"]) - trig["_checked_count"]
            hits = trig["watcher"].scan(view, trig["checked"], now)
        except Exception as e:                                          # 条件の変数が記録に無い等
            trig.update(state="error", error=f"{type(e).__name__}: {e}")
            self._remove_job("recording.trigger")
            trig["view"].close()
            return
        trig["checked"] = max(trig["checked"], now)
        for t in hits:
            self._recorder.add("annotation", "mark", t, body={"text": f"条件が成り立った: {trig['condition']}"})
        self._recorder.flush()                                          # 自分の印も数えた後で、届いた分の数を覚える
        trig["_checked_count"] = trig["view"].count_events(t1=trig["checked"])
        if hits:
            trig["hits"] += len(hits)
            trig["first_t"] = trig["first_t"] if trig["first_t"] is not None else hits[0]
            trig["last_t"] = hits[-1]
            trig["state"] = "fired"
        if trig["action"] == "stop" and trig["first_t"] is not None and now >= trig["first_t"] + trig["post_s"] * hz:
            trig["state"] = "stopped"
            self._remove_job("recording.trigger")
            trig["view"].close()
            # 止めるのはロックの外で（ロジアナの流れの受け取りがロックを取る。recording_la_stream_stop）
            threading.Thread(target=self._trigger_stop, args=(trig["condition"],), name="fprobe-trigger-stop",
                             daemon=True).start()

    def _trigger_stop(self, condition: str) -> None:
        """条件で記録を止め、誰が・なぜ止めたかを通知 recording で知らせる（デーモンを通らない止め方なので）。"""
        result = self.recording_stop()
        publish = getattr(self, "_publish", None)
        if publish is not None:
            publish("recording", {"action": "stop", "name": result.get("recording"), "reason": f"条件: {condition}",
                                  "client": {"id": None, "name": "trigger", "role": "script"}})

    def recording_durations(self, variable: str, t0: int | None = None, t1: int | None = None, limit: int = 20,
                            gap_s: float | None = None) -> dict:
        """変数（recording_correlate と同じ指し方）の値ごとの回数・続いた時間（両端の見えている区間。秒の最小・中央値・
        90 % 点・最大）・その値だった時間の割合。続いた時間の合計の大きい順に limit 個（値の総数 values_total・切ったか
        truncated・使った limit）。例: FPGA の state が BUSY のまま続く時間を MCU の待ち時間と比べる。データの欠け
        にかかる回は数えず excluded（欠けは gaps）。"""
        return durations(self._reading(), variable, t0, t1, limit, gap_s)

    def recording_transactions(self, start: str, by: str | None = None, counts: list[str] | None = None,
                               effect: str | None = None, t0: int | None = None, t1: int | None = None,
                               rows: int = 20, by_at: str = "start", effect_lag_s: float = 0.0,
                               gap_s: float | None = None) -> dict:
        """記録を区間（リクエスト等の 1 回の取引）に切って数える（条件・変数は recording_correlate と同じ書き方）。区間 =
        start（瞬間の項のある条件）が成り立ってから次に成り立つまで。区間ごとに by の開始時の値・counts の各条件の回数・
        effect が起きたか（by_at "end" なら by は区間の終わりの値。例 start "requests changed"・by "last_b"・by_at "end" で
        要求ごとの B）。groups = by の値ごとの区間の数・effect の割合（effect_rate）・counts の平均、rows = 最初の rows
        区間（入れうる区間の数 rows_total・切ったか rows_truncated・使った rows は rows_limit）。例: start "state changed and state=BUSY"、by "left_hi"、counts ["restart changed and state=BUSY"]、
        effect "errors changed" → どの入力の要求が再試行され、再試行された要求が誤るか。データの欠けにかかる区間は groups・rows に
        入れず excluded（変数ごとの内訳は excluded_by、欠けは gaps）。length_s = 区間の長さ、effect_offset_s = 区間の始め
        から結果までの時間（秒の分位。結果が始めの直後に集まるなら、前の区間の結果が次に入っている可能性: 区間の切り方を
        見直すか、effect_lag_s でその秒数だけ前の時刻の区間に割り当てる）。effect があれば by_effect = 結果が起きた区間（with）と起きなかった区間（without）の
        数・counts の平均（失敗した回と普段の回の違い）。"""
        return transactions(self._reading(), start, by, counts, effect, t0, t1, rows, by_at, effect_lag_s, gap_s)

    def recording_find(self, query: dict, limit: int = FIND_LIMIT) -> dict:
        """条件に一致する出来事の一覧。query: stream / kind / t0 / t1 / a / b（値か {"eq"|"lt"|"ge"…: 値}）/
        body（{欄: 値か {"regex": …}}）。events は時刻の順に先頭 limit 件（count = 総数、truncated = 切ったか、
        limit = 使った上限）。"""
        if limit < 0:
            raise ValueError("limit は 0 以上")
        found = self._reading().find(query)
        return {"count": len(found), "events": [asdict(e) for e in found[:limit]], "truncated": len(found) > limit,
                "limit": limit}

    def recording_intervals(self, start: dict, end: dict, limit: int = FIND_LIMIT) -> dict:
        """開始の条件から次の終了の条件までの時間（タイムベース）の一覧と統計（最小・最大・平均・標準偏差。統計は
        全部の組から）。intervals は先頭 limit 組（count = 総数、intervals_truncated = 切ったか、limit = 使った上限）。"""
        if limit < 0:
            raise ValueError("limit は 0 以上")
        stats = self._reading().intervals(start, end)
        stats["intervals_truncated"] = len(stats["intervals"]) > limit
        stats["intervals"] = stats["intervals"][:limit]
        stats["limit"] = limit
        return stats

    def recording_density(self, t0: int, t1: int, width: int = 1000, streams: list[str] | None = None) -> dict:
        """画素ごとの出来事の件数（縮図・タイムラインの要約）。長期の記録で間引いた・消した部分にかかれば coverage と、
        端がそこにかかって数えられない画素の inexact（[{pixel, max}]。counts はその画素では残っている出来事だけの下限、
        max は上限。消した分割なら None。按分で推定しない）。"""
        view = self._reading()
        bounds = view.density_bounds(t0, t1, width, streams)
        out = {"counts": bounds["counts"]}
        if bounds["inexact"]:
            out["inexact"] = bounds["inexact"]
        coverage = view.coverage(t0, t1)
        if not coverage["complete"]:
            out["coverage"] = coverage
        return out

    def recording_waveform(self, t0: int, t1: int, width: int = 1000) -> dict:
        """記録の波形を画素ごとに '0' / '1' / 'x'（変化）/ '-'（波形が無い）。"""
        return {"levels": self._reading().waveform_view(t0, t1, width)}

    def recording_mark(self, text: str, t: int | None = None, caller: dict | None = None) -> dict:
        """印とメモを残す（調査の経緯）。t（タイムベース）を省くと記録中の記録のいまの時刻、指定するとその時刻
        （記録中の記録か、開いた記録。時間軸で選んだ時刻に後から印を置く）。caller（デーモンが渡す呼び出し元
        {id, name, role}。引数の仕様には出ない）は置いた者として印の本文の client に残す（デーモンを通さなければ無し）。"""
        body: dict = {"text": text, **({"client": dict(caller)} if caller is not None else {})}
        rec = self._recorder
        if rec is not None:
            if t is None:
                return {"t": rec.add_host("annotation", "mark", time.monotonic(), body=body)}
            rec.add("annotation", "mark", t, body=body)
            return {"t": t}
        if t is None:
            raise RuntimeError("記録していない（recording_start を先に。開いた記録には t（タイムベース）を指定して印を足す）")
        self._reading().add("annotation", "mark", t, body=body)
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
        読みを対応付けた後。クロストリガの割り込み書き込みは kind "inject" と、停止の書き込みまでの転送数 transfers（旧い回路は
        null））、True なら線上の転送そのもの。time_us は記録開始とは無関係なタイムベースの時刻（µs）。"""
        with self._lock:
            written, lost, first, events, extra = self._read_events(last, since)
            hz = self.session.device.capabilities().timebase_hz
            if raw:
                items = [{"time_us": e.time * 1e6 / hz, "kind": e.kind, "ap": e.ap, "read": e.read,
                          "register": e.register, "ack": e.ack, "parity_error": e.parity_error,
                          "address": f"0x{e.address:08X}", "data": f"0x{e.data:08X}",
                          **({"transfers": e.transfers} if e.kind in INJECT_KINDS else {})} for e in events]
            else:
                items = [{"time_us": a.time * 1e6 / hz, "kind": a.kind, "address": f"0x{a.address:08X}",
                          "value": f"0x{a.value:08X}", "ack": a.ack,
                          **({"transfers": a.transfers} if a.kind == "inject" else {})} for a in resolve(events)]
            return {"written": written, "lost": lost, "first": first, "next": first + len(events), **extra,
                    "items": items}

    def _read_events(self, last: int, since: int | None) -> tuple[int, int, int, list, dict]:
        """イベントの記録の窓（events_read の説明）: (written, lost, first, events, extra)。ロックの中で呼ぶ。"""
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
        return written, lost, first, events, extra

    # --- 対象の FPGA の観測（FEATURES の FPGA_OBS がある構成だけ。値はイベント 8・9 で記録に入る） -------------

    def fpga_obs_open(self, chain: dict | None = None, user_ir: int = 2, half_period: int = 10, words: int = 64,
                      sync_period: int = 100, auto_reopen: bool = False, id_scans: int = 0) -> dict:
        """対象の FPGA の JTAG で観測の回路 v2（target_ip/fprobe_obs）を読み始め、その申告（id・version・width・groups・
        fifo_depth・clock_hz・sync_delay・sample_delay・lost_bits）を返す。chain = 鎖での対象の位置 {ir_len（既定 4）,
        ir_before, ir_after, dr_before, dr_after}、user_ir = 対象の USER の命令、half_period = TCK の半周期（sys の周期数。
        obs_soft_core の 1 クロック版は TCK ≤ 対象の clk / 8）、words = 1 スキャンの記録の語数、sync_period = 印（時刻
        合わせ）の間隔（スキャンの数）、id_scans = 始めに ID の応答を待つスキャンの上限（0 = 上限なし。上限まで来なければ
        止まり、status の stuck・id_timeout）。値を記録に残すには events_start も要る。auto_reopen = 観測の回路が答えなくなって
        止まったら（stuck。対象の FPGA を書き直した・電源を入れ直した等）、1 秒ごとに同じ設定で開き直す（記録中なら印を
        残す。回数は fpga_obs_status の reopened）。止まった回路の線をまた動かすので、利用者が選んだときだけ。"""
        config = {"chain": chain, "user_ir": user_ir, "half_period": half_period, "words": words,
                  "sync_period": sync_period, "id_scans": id_scans}
        with self._lock:
            info = self._open_obs(config)
            self._fpga_obs_opened.update(auto_reopen=auto_reopen, reopened=0)
            if auto_reopen:
                self._add_job("fpga_obs.reopen", REOPEN_PERIOD_S, self._fpga_obs_reopen)
            else:
                self._remove_job("fpga_obs.reopen")
            return info

    def _fpga_obs_reopen(self) -> None:
        """auto_reopen の周期の仕事（_lock の中で）: 止まっていたら同じ設定で開き直す。まだ答えなければ次の周期で。"""
        opened = getattr(self, "_fpga_obs_opened", None)
        if not opened or not self._obs().status().get("stuck"):
            return
        try:
            self._open_obs(opened["config"])
        except Exception:                                          # まだ答えない（書き込みの途中等）
            return
        self._fpga_obs_opened["reopened"] = opened.get("reopened", 0) + 1     # _open_obs が覚え直した方に
        if self._recorder is not None:
            self._recorder.add("annotation", "mark", self.session.device.time(),
                               body={"text": "FPGA の観測を開き直した（観測の回路が答えなくなっていた）"})

    def _open_obs(self, config: dict) -> dict:
        """観測を開き、設定と申告を覚え、遅れの申告を取り込みに渡す（_lock の中で）。"""
        with self._lock:
            self._fpga_latest = {}                           # 別の回路・設定かもしれない: 前の最後の値は捨てる
            self._fpga_obs = FpgaObs(self.session.device)
            info = self._fpga_obs.open(config["chain"], config["user_ir"], config["half_period"], config["words"],
                                       config["sync_period"], id_scans=config.get("id_scans", 0))
            # 開いた設定と申告を覚える（別の窓・開き直した画面が見る。対象を書き直した後に同じ設定で開き直す）
            previous = getattr(self, "_fpga_obs_opened", None) or {}
            self._fpga_obs_opened = {**previous, "config": config, "declared": info}
            hz = self.session.device.capabilities().timebase_hz
            self._obs_delays = {"sync_delay": info["sync_delay"], "sample_delay": info["sample_delay"],
                                "nominal_ratio": hz / info["clock_hz"] if info["clock_hz"] else None}
            collector = getattr(self, "_collector", None)
            if collector is not None:                              # 記録中: 取り込みの復号器も同じ遅れで
                for k, v in self._obs_delays.items():
                    setattr(collector.obs, k, v)
            return info

    def _obs(self) -> FpgaObs:
        """開いた観測（open で読んだ型・遅れの申告を持つ）。開いていなければ新しく（状態を見る・閉じるため）。"""
        obs = getattr(self, "_fpga_obs", None)
        if obs is None or obs.dev is not self.session.device:
            obs = self._fpga_obs = FpgaObs(self.session.device)
        return obs

    def fpga_obs_close(self) -> dict:
        """観測をやめる（対象の TAP を Test-Logic-Reset に戻し、線を手放す。auto_reopen も止める）。"""
        with self._lock:
            self._remove_job("fpga_obs.reopen")
            if getattr(self, "_fpga_obs_opened", None):
                self._fpga_obs_opened["auto_reopen"] = False
            self._obs().close()
            self._fpga_latest = {}
            return {}

    def fpga_obs_status(self) -> dict:
        """観測の状態（running・id_ok・stuck = 観測の回路が答えないので止めた・cmd_pending・overflow_seen・
        cmd_dropped・scans・id・mode = stream（記録型）/ snapshot（標本型））と、最後に開いた設定 config（fpga_obs_open の
        引数。開いていなければ null）・申告 declared（fpga_obs_open の戻り）・auto_reopen・reopened（自動で開き直した回数）。"""
        with self._lock:
            opened = getattr(self, "_fpga_obs_opened", None) or {"config": None, "declared": None, "auto_reopen": False,
                                                                  "reopened": 0}
            return {**self._obs().status(), "mode": self._obs().mode, **opened}

    def fpga_obs_trigger_set(self, condition: str, halt: bool = True, la: bool = False, group: int = 0) -> dict:
        """FPGA の条件（システム解析と同じ書き方。例 "restart changed and state=BUSY"・"state=BUSY"（入った瞬間）・
        "flag rose"。瞬間の項は 1 つまで）を fprobe の観測のエンジンで比べ、成り立ったらハードのクロストリガ
        （源 obs_match）: halt = 割り込み書き込み（既定は MCU の停止）、la = ロジアナのトリガ（クロストリガだけで
        捕捉するには la_arm / la_deep_arm の external_only=true（深いバッファは rle=False が速い。RLE は信号の変化が少ないとトリガ後の拍が埋まるのに十数秒かかる））。遅れは観測のスキャン 1 回分
        （スナップショット型・TCK 2.5 MHz で約 15 µs）+ SWD の書き込み（ホストの CSW・TAR の控えがあれば 4 転送 = 約 190
        SWCLK、無ければ読んで戻す転送が足される。2026-10-22 の Shrike-Lite の実測: SWD 4 MHz で 101 µs、1 MHz で
        206〜362 µs）。信号の名前は組 group の定義（fpga_obs_signals_get）から。
        docs/superpowers/specs/2026-10-21-system-analysis-design.md §4.1"""
        with self._lock:
            caps = self.session.device.capabilities()
            if not getattr(caps, "obs_match", False):
                raise RuntimeError("この構成にはハードの FPGA の条件のトリガが無い（ケイパビリティ obs_match）")
            self._need_inject(caps, halt)
            defs = self.fpga_obs_signals_get()
            if not halt and not la:
                raise ValueError("halt か la の少なくとも一方が要る（どちらも無ければ成立しても何も起きない）")
            mask, value, change = compile_match(condition, FpgaSignals.from_dict(defs["recording"] or defs["signals"]),
                                                group)
            self._obs_trigger = None                                     # 書く途中で失敗しても古い記憶を残さない
            dev = self.session.device
            dev.write32(g.OBS_MATCH_CHANGE, 0)                          # 変える間は止める（途中の組で発火しない）
            dev.write32(g.OBS_MATCH, mask | value << 16)
            dev.write32(g.OBS_MATCH_CHANGE, change | 1 << g.OBS_MATCH_CHANGE_ENABLE[0])
            self._obs_route(halt, la)
            self._obs_trigger = {"condition": condition, "halt": halt, "la": la}
            _save_obs_trigger({**self._obs_trigger, "mask": mask, "value": value, "change": change})
            return {"mask": mask, "value": value, "change": change, **self._obs_trigger}

    def fpga_obs_trigger_status(self) -> dict:
        """ハードの FPGA の条件のトリガ: 条件・halt・la・成り立った回数 count（無ければ condition = null）。
        条件の文字はデーモンにしか無いので、デーモンの再起動の前に仕掛けたもの（FPGA には残って MCU を止め続ける）は
        レジスタから orphan = true（mask・value・change・halt・la・count）で返す。fpga_obs_trigger_clear で外せる。"""
        with self._lock:
            trig = getattr(self, "_obs_trigger", None)
            armed = self._obs_armed()
            if trig is not None and armed is None:                   # FPGA を書き直した・経路を外された: もう無い
                self._obs_trigger = trig = None
            if trig is not None:
                return {**trig, "count": self.session.device.read32(g.OBS_MATCH_COUNT)}
            if armed is None:
                return {"condition": None}
            dev = self.session.device
            match = dev.read32(g.OBS_MATCH)
            regs = {"mask": match & 0xFFFF, "value": match >> 16 & 0xFFFF, "change": dev.read32(g.OBS_MATCH_CHANGE) & 0xFFFF}
            saved = _load_obs_trigger()                        # デーモンの再起動の前に仕掛けた条件の文字
            if saved and all(saved.get(k) == v for k, v in regs.items()) and saved.get("halt") == armed["halt"]                     and saved.get("la") == armed["la"]:
                self._obs_trigger = {"condition": saved["condition"], "halt": armed["halt"], "la": armed["la"]}
                return {**self._obs_trigger, "restored": True, "count": dev.read32(g.OBS_MATCH_COUNT)}
            return {"condition": None, "orphan": True, **regs, **armed, "count": dev.read32(g.OBS_MATCH_COUNT)}

    def fpga_obs_trigger_clear(self) -> dict:
        """ハードの FPGA の条件のトリガをやめる（クロストリガの経路の obs_match を外す。ほかの源の経路は触らない）。
        デーモンの再起動の前に仕掛けた条件（orphan）も外す。"""
        with self._lock:
            if getattr(self, "_obs_trigger", None) is not None or self._obs_armed() is not None:
                self.session.device.write32(g.OBS_MATCH_CHANGE, 0)
                self._obs_route(False, False)
            self._obs_trigger = None
            _save_obs_trigger(None)
            return {"condition": None}

    def _obs_armed(self) -> dict | None:
        """FPGA の条件のトリガがレジスタの上で生きていれば {halt, la}（_lock の中で）。無い・この構成に無ければ None。"""
        dev = self.session.device
        if not getattr(self._caps(), "obs_match", False):
            return None
        if not dev.read32(g.OBS_MATCH_CHANGE) >> g.OBS_MATCH_CHANGE_ENABLE[0] & 1:
            return None
        route, bit = dev.read32(g.XTRIG_ROUTE), int(Source.OBS_MATCH)
        halt = bool(route >> g.XTRIG_ROUTE_TO_INJECT[0] & bit)
        la = bool(route >> g.XTRIG_ROUTE_TO_LA[0] & bit)
        return {"halt": halt, "la": la} if halt or la else None

    def _obs_route(self, halt: bool, la: bool) -> None:
        """クロストリガの経路の obs_match のビットだけを変える（_lock の中で）。"""
        self._route_bit(Source.OBS_MATCH, halt, la)

    @staticmethod
    def _need_inject(caps, halt: bool) -> None:
        """halt（割り込み書き込みの経路）を使うなら、故障注入の回路が要る（無い構成では経路を立てても何も起きない）。"""
        if halt and not getattr(caps, "inject", True):
            raise RuntimeError("この構成に故障注入（割り込み書き込み。ケイパビリティ inject）が無いので halt はできない"
                               "（la だけなら使える）")

    def _route_bit(self, source: Source, halt: bool, la: bool) -> None:
        """クロストリガの経路の source のビットだけを変える（_lock の中で）。"""
        dev = self.session.device
        route = dev.read32(g.XTRIG_ROUTE)
        bit = int(source)
        for field, on in ((g.XTRIG_ROUTE_TO_INJECT, halt), (g.XTRIG_ROUTE_TO_LA, la)):
            route = route | bit << field[0] if on else route & ~(bit << field[0])
        dev.write32(g.XTRIG_ROUTE, route)

    # --- サンプラの値の一致のトリガ（R21。docs/superpowers/specs/2026-10-23-sampler-match-trigger-design.md）---------

    def sampler_trigger_set(self, address: int, mask: int, value: int, halt: bool, la: bool) -> dict:
        """DAP サンプラが読んだ address の語が (値 xor value) and mask = 0 に入った瞬間（前の標本が不一致・今の標本が一致）に
        ハードのクロストリガ（源 sampler_match）: halt = 割り込み書き込み（既定は MCU の停止）、la = ロジアナのトリガ。
        例: MCU が止まった = address 0xE000EDF0（DHCSR）・mask = value = 0x20000（S_HALT）。サンプラに address を読ませるのは
        別の道具（watch 等。この API はサンプラの持ち主にならない）。読んでいなければ sampler_trigger_status の seen が 0 のまま。
        有効にした後の最初の標本では発火しない（その前を見ていない）。"""
        if not halt and not la:
            raise ValueError("halt か la の少なくとも一方が要る（どちらも無ければ成立しても何も起きない）")
        for name, v in (("address", address), ("mask", mask), ("value", value)):
            if not 0 <= v <= 0xFFFFFFFF:
                raise ValueError(f"{name} は 32 ビット（{v:#x}）")
        with self._lock:
            if not getattr(self._caps(), "sampler_match", False):
                raise RuntimeError("この構成にはサンプラの値の一致のトリガが無い（ケイパビリティ sampler_match）")
            self._need_inject(self._caps(), halt)
            dev = self.session.device
            dev.write32(g.SAMPLER_MATCH_CONTROL, 0)                    # 変える間は止める
            dev.write32(g.SAMPLER_MATCH_ADDR, address)
            dev.write32(g.SAMPLER_MATCH_MASK, mask)
            dev.write32(g.SAMPLER_MATCH_VALUE, value)
            dev.write32(g.SAMPLER_MATCH_CONTROL, 1 << g.SAMPLER_MATCH_CONTROL_ENABLE[0])
            self._route_bit(Source.SAMPLER_MATCH, halt, la)
            return {"address": address, "mask": mask, "value": value, "halt": halt, "la": la}

    def sampler_trigger_status(self) -> dict:
        """サンプラの値の一致のトリガのレジスタの事実: enabled・address・mask・value・halt・la（経路）・matched（最後の標本が
        一致）・seen_any・seen（有効にした後に比べた標本の数。0 ならサンプラがその番地を読んでいない）・count（発火の回数）・
        time（最後に発火した標本を受けた時刻、タイムベースの下位 48 ビット = イベントの時刻と同じ幅）・prev_time（その直前の不一致の標本の時刻。条件に入ったのは
        prev_time と time の間のどこか）。"""
        with self._lock:
            if not getattr(self._caps(), "sampler_match", False):
                raise RuntimeError("この構成にはサンプラの値の一致のトリガが無い（ケイパビリティ sampler_match）")
            dev = self.session.device
            # 1 回のまとめ読み（数・時刻を同じ時点に近づける。ハードにラッチは無い）
            address, mask, value, ctrl, seen, count, t_lo, t_hi, p_lo, p_hi = dev.read(g.SAMPLER_MATCH_ADDR, 10)
            route, bit = dev.read32(g.XTRIG_ROUTE), int(Source.SAMPLER_MATCH)
            return {"enabled": bool(ctrl >> g.SAMPLER_MATCH_CONTROL_ENABLE[0] & 1),
                    "address": address, "mask": mask, "value": value,
                    "halt": bool(route >> g.XTRIG_ROUTE_TO_INJECT[0] & bit), "la": bool(route >> g.XTRIG_ROUTE_TO_LA[0] & bit),
                    "matched": bool(ctrl >> g.SAMPLER_MATCH_CONTROL_MATCHED[0] & 1),
                    "seen_any": bool(ctrl >> g.SAMPLER_MATCH_CONTROL_SEEN_ANY[0] & 1),
                    "seen": seen, "count": count, "time": t_hi << 32 | t_lo, "prev_time": p_hi << 32 | p_lo}

    def sampler_trigger_clear(self) -> dict:
        """サンプラの値の一致のトリガをやめる（比べるのを止め、経路の sampler_match を外す。ほかの源の経路は触らない）。"""
        with self._lock:
            if getattr(self._caps(), "sampler_match", False):
                self.session.device.write32(g.SAMPLER_MATCH_CONTROL, 0)
                self._route_bit(Source.SAMPLER_MATCH, False, False)
            return {"enabled": False}

    def fpga_obs_register_read(self, addr: int) -> dict:
        """観測の回路のレジスタ（16 ビット）を読む（0・1 ID の下位・上位、2 VERSION、3 GROUPS、4 FIFO_DEPTH、
        5・6 CLOCK_HZ の下位・上位、7 SYNC_DELAY、8 SAMPLE_DELAY、9 LOST_BITS、14 CONTROL、15 SELECT）。標本型には無い。"""
        with self._lock:
            return {"value": self._obs().register_read(addr)}

    def fpga_obs_register_write(self, addr: int, value: int) -> dict:
        """観測の回路のレジスタに書く（14 CONTROL の bit0 = 記録する、15 SELECT = 観測する 16 ビットの組。値は 0〜255）。"""
        with self._lock:
            self._obs().register_write(addr, value)
            return {}

    def fpga_obs_select(self, group: int) -> dict:
        """観測する 16 ビットの組を選ぶ（0〜groups − 1）。"""
        with self._lock:
            self._obs().select(group)
            return {}

    def fpga_obs_start(self) -> dict:
        """観測の回路が値の変化を記録し始める（始め直すと絶対の時刻と今の値から）。"""
        with self._lock:
            self._obs().start()
            return {}

    def fpga_obs_stop(self) -> dict:
        """観測の回路の記録を止める（読み出しは続く）。"""
        with self._lock:
            self._obs().stop()
            return {}

    def fpga_obs_signals_get(self) -> dict:
        """FPGA の観測の組に付ける信号の定義: signals = 画面の状態（記録していないときの既定）、recording = 開いている
        記録の定義（無ければ null）。"""
        meta = getattr(self._view, "meta", None) or {}
        return {"signals": self.ui_state_get()["fpga_signals"], "recording": meta.get("fpga.signals")}

    def fpga_obs_signals_set(self, signals: dict) -> dict:
        """信号の定義（{"groups": {"<組>": [{"name", "lsb", "width", "format": hex/dec/bin/enum, "enum": {"<値>": "<名前>"}}]}}）
        を置く。画面の状態（別の窓・AI と共有）と、記録中・開いている記録の情報に（後から変えても過去の記録の段の名前が
        変わる。記録の生の値は変わらない）。名前の重複・ビットの重なり・16 ビットの外は断る。"""
        d = FpgaSignals.from_dict(signals).to_dict()
        self.ui_state_set(fpga_signals=d)
        wrote = False
        with self._lock:
            if self._recorder is not None:
                self._recorder.update_meta(**{"fpga.signals": d})
                wrote = True
            view = self._view
            if view is not None and hasattr(view, "update_meta"):
                view.update_meta(**{"fpga.signals": d})
                wrote = True
        return {"signals": d, "recording": wrote}

    def fpga_obs_read(self, last: int = 2000, since: int | None = None) -> dict:
        """イベントの記録から観測した値を fprobe の時刻で返す（窓は events_read と同じ。続けて読むなら since）。
        items: time_us（fprobe のタイムベース。印が無く決まらなければ null）・target_cycles（鎖の中の対象の clk の周期。
        溢れ・読み始めで 0 から）・value（null は溢れの切れ目、lost = 失った変化の数）。窓の中の最初の鎖の始め・印より
        前の値は時刻が決まらないので出ない。"""
        hz = self._caps().timebase_hz
        defs = self.fpga_obs_signals_get()
        extra_out: dict = {}
        try:
            signals = FpgaSignals.from_dict(defs["recording"] or defs["signals"])
        except (ValueError, KeyError, TypeError) as e:          # 壊れた定義でも生の値は返す（定義の誤りを添える）
            signals = None
            extra_out["signals_error"] = str(e)

        def item(s) -> dict:
            fields = signals is not None and s.kind == "value"
            return {"time_us": None if s.time_fprobe is None else s.time_fprobe * 1e6 / hz,
                    "time_basis": getattr(s, "time_basis", None),
                    "target_cycles": s.target_cycles, "kind": s.kind, "group": s.group, "value": s.value,
                    "lost": s.lost, **({"fields": signals.fields(s.group, s.value)} if fields else {})}

        la_record, collector = self._la_record, self._collector      # 別のスレッドが替えるので一度だけ読む
        if la_record is not None and la_record.running and collector is not None:
            # ロジアナを記録へ流している間はリングを流れが読む: 記録へ取り込んだ値（共有の復号器が覚えている分）から。
            # 機器に触らないので機器のロックを取らない（取ると流れの受け取りと奪い合い、画面の毎秒の読みで 1 秒待ち、
            # 受け取りも遅れて値が数分古くなった。2026-10-21 実機）
            if since is not None:
                raise RuntimeError("ロジアナを記録へ流している間は since で続けて読めない（last だけ。続きは "
                                   "recording_events の stream fpga）")
            recent = list(collector.obs.recent)[-last:] if last > 0 else []
            return {"source": "recording", "items": [item(s) for s in recent], **extra_out}
        with self._lock:
            written, lost, first, events, extra = self._read_events(last, since)
            items = [item(s) for s in obs_samples(events, **getattr(self, "_obs_delays", {}))]
            return {"written": written, "lost": lost, "first": first, "next": first + len(events), **extra,
                    "items": items, **extra_out}

    def fpga_obs_count(self, conditions: list[str], last: int) -> dict:
        """観測した値の最後の last 件（fpga_obs_read の窓。使う側が明示する。既定の値は無い）で、条件ごとに成り立った
        回数と、数えた範囲の事実（数えた値の数・最初と最後の値の時刻 t0_us / t1_us・その間の秒 span_s・切れ目の数 gaps・
        失った変化の数 lost）を返す（fprobe.obs_condition）。読めない条件は error に理由を返す（ほかの条件は数える）。"""
        items = self.fpga_obs_read(last=last)["items"]
        out = []
        for text in conditions:
            try:
                out.append(count_condition(items, text))
            except ConditionError as e:
                out.append({"condition": text, "error": str(e)})
        return {"last": last, "conditions": out}

    # --- システム（MCU と FPGA を並べて） ----------------------------------------------------------------------

    def system_status(self, addresses: list[int] | None = None, fpga_last: int = 2000) -> dict:
        """MCU と FPGA の両方の今を 1 回で返す（システム解析の画面・AI の見回り用。どちらかが無くても他方は返す）。
        mcu: target_status と、addresses の 32 ビットの語（止めずに読む。例: ファームウェアのメールボックス）・connect
        （最後に成功した target_connect の引数。対象のリセットで SWD がはずれたら同じ引数でつなぎ直す）。
        fpga: 観測の状態（型・スキャンの数・溢れ）と、最近の値（fpga_obs_read の最後の fpga_last 件のうち、各組の最新の
        値と信号。窓から消えても最後に分かった値を返す。time_us で古さがわかる）。
        値を読むにはイベントの記録（events_start）が要る。記録型は窓の中に印が無いと値の時刻が決まらず
        出ないので、窓は広め（既定 2000 件）。"""
        with self._lock:
            device = self.session.device
            out: dict = {"time_us": device.time() * 1e6 / self._caps().timebase_hz}
            try:
                mcu = {"connected": True, **self.target_status()}
                mcu["memory"] = {f"0x{a:08X}": self.target.read32(a) for a in addresses or []}
            except NotConnected:
                mcu = {"connected": False}
            except Exception as e:                           # 読めない番地等: 理由を返す
                mcu = {"connected": True, "error": str(e)}
            mcu["connect"] = getattr(self, "_last_connect", None)
            out["mcu"] = mcu
            if not getattr(self._caps(), "fpga_obs", False):
                out["fpga"] = {"available": False}
                return out
            fpga: dict = {"available": True, **self.fpga_obs_status()}
            try:
                items = self.fpga_obs_read(last=fpga_last)["items"]
                # 最後に分かった値を覚えておく（値が変わらないと窓から消える。MCU を止めた瞬間の FPGA こそ見たい）
                latest = self.__dict__.setdefault("_fpga_latest", {})
                for it in items:
                    if it["kind"] == "value":
                        latest[it["group"]] = it
                defs = self.fpga_obs_signals_get()                # 名前は今の定義で（定義を変えた後も古い名前を出さない）
                signals = FpgaSignals.from_dict(defs["recording"] or defs["signals"])
                fpga["latest"] = [{**latest[g], "fields": signals.fields(g, latest[g]["value"])} for g in sorted(latest)]
                fpga["gaps"] = sum(1 for it in items if it["kind"] == "gap")
                fpga["window"] = {"last": fpga_last, "items": len(items)}     # 見た窓（latest のうち窓の外のものもある）
            except Exception as e:                           # イベントの記録を始めていない等
                fpga["latest_error"] = str(e)
            out["fpga"] = fpga
            return out

    # --- UART（USB では CDC-ACM、フレームのリンクでは ch 3 = UART0） -----------------

    def _uart_link(self, baud: int | None) -> FrameLink | None:
        """フレームのリンクで開いているならそのリンク（ボーレートを LINE_CODING で合わせる。頼まれず、まだ決まって
        いなければ UART_DEFAULT_BAUD。使った速さは結果の baud に出す）。USB なら None。"""
        link = self.session.link
        if link is not None:
            want = baud or link.uart_baud or UART_DEFAULT_BAUD
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
            self._uart = Serial(ports[0], baud or UART_DEFAULT_BAUD, timeout=0)
        elif baud and self._uart.baudrate != baud:
            self._uart.baudrate = baud                     # ボーレートはライン設定でプローブに伝わる
        return self._uart

    def uart_write(self, data: str, baud: int | None = None) -> dict:
        """ターゲットの UART に送る（latin-1）。baud を指定するとボーレートも変える。written（送ったバイト数）と、
        使ったボーレート baud（頼まれず、まだ開いていなければ UART_DEFAULT_BAUD）。"""
        with self._lock:
            if link := self._uart_link(baud):
                return {"written": link.uart_write(data.encode("latin-1")), "baud": link.uart_baud}
            port = self._uart_port(baud)
            n = port.write(data.encode("latin-1"))
            port.flush()
            return {"written": n, "baud": port.baudrate}

    def _uart_fetch(self, baud: int | None = None) -> None:
        """UART の届いた分を読んで貯める（_lock の中で）。"""
        link = self._uart_link(baud)
        data = link.uart_read(4096) if link else self._uart_port(baud).read(4096)
        if data:
            self._received("uart", bytes(data), time.monotonic())

    def uart_read(self, timeout_s: float = 0.5, baud: int | None = None) -> dict:
        """timeout_s の間に届いたもの data（latin-1 で文字列化）と、受けたボーレート baud（頼まれず、まだ開いて
        いなければ UART_DEFAULT_BAUD）。背景で読んでいる（TCP の口・ファイルへ流している）なら、前回の読みからの分も含む。"""
        if baud is not None:
            with self._lock:
                self._uart_fetch(baud)                     # ボーレートを先に合わせる
        data = self._collect("uart", timeout_s).decode("latin-1")
        with self._lock:
            return {"data": data, "baud": self._uart_baud_now()}

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
                 "uart_write", "uart_read", "fpga_obs_open", "fpga_obs_close", "fpga_obs_status",
                 "fpga_obs_register_read", "fpga_obs_register_write", "fpga_obs_select", "fpga_obs_start",
                 "fpga_obs_stop", "fpga_obs_read", "fpga_obs_signals_get", "fpga_obs_signals_set"]:
        server.add_tool(getattr(probe, name))
    # la_capture は **trigger を明示した版を出す（MCP のスキーマは引数から作られる）
    server.add_tool(_la_capture(probe))
    return server


def _obs_trigger_path() -> Path:
    """仕掛けた FPGA の条件のトリガ（条件の文字・halt・la・レジスタの値）。デーモンの再起動の後に条件の文字で見せる。"""
    return Path(os.environ.get("FPROBE_HOME", Path.home() / ".fprobe")) / "obs_trigger.json"


def _save_obs_trigger(trigger: dict | None) -> None:
    path = _obs_trigger_path()
    try:
        if trigger is None:
            path.unlink(missing_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(trigger, ensure_ascii=False), encoding="utf-8")
    except OSError:                                         # 覚えられなくても、トリガそのものは働く
        pass


def _load_obs_trigger() -> dict | None:
    try:
        return json.loads(_obs_trigger_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _cortex_m(link: dict | None) -> bool:
    """接続中のターゲットが Cortex-M か（DFSR 等の M のレジスタを触ってよいか）。Cortex-A・ARM11・RISC-V は違う。"""
    name = (link or {}).get("target") or ""
    return riscv_part(name) is None and arm_a_part(name) is None


def _la_capture(probe: Probe):
    def la_capture(rate_hz: float | None = None, pretrigger: int = 1024, mask: int = 0, value: int = 0,
                   rise: int = 0, fall: int = 0, timeout_s: float = 5.0, vcd_path: str | None = None,
                   filter_channels: int = 0, filter_ns: float = 0.0, pulse: dict | None = None,
                   external_only: bool = False) -> dict:
        """捕捉を仕掛けて完了まで待つ（la_arm + la_collect）。トリガ = (サンプル & mask) == (value & mask)
        かつ rise / fall のビットのチャンネルのどれかがその向きに変化（かつ pulse のパルス幅。la_arm を参照）。
        external_only = true ならクロストリガでだけ捕捉する。"""
        return probe.la_capture(timeout_s, vcd_path, rate_hz=rate_hz, pretrigger=pretrigger, mask=mask,
                                value=value, rise=rise, fall=fall, filter_channels=filter_channels,
                                filter_ns=filter_ns, pulse=pulse, external_only=external_only)
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
    notices = [{"topic", "message", ...}] として添える（添えたら消す。溜めるのは最新 NOTICE_LIMIT 件。古いものを
    捨てたら notices_dropped = 捨てた数・notices_limit も添える）。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._pending: list[dict] = []
        self._dropped = 0

    def add(self, topic: str, params: dict) -> None:
        with self._lock:
            self._pending.append({"topic": topic, **params})
            over = len(self._pending) - NOTICE_LIMIT
            if over > 0:
                del self._pending[:over]
                self._dropped += over

    def attach(self, result):
        if not isinstance(result, dict):
            return result                               # 辞書でない結果の形は変えない（次の辞書の結果に添える）
        with self._lock:
            pending, self._pending = self._pending, []
            dropped, self._dropped = self._dropped, 0
        if not pending:
            return result
        out = {**result, "notices": pending}
        if dropped:
            out.update(notices_dropped=dropped, notices_limit=NOTICE_LIMIT)
        return out


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


def _channel_list(caps) -> list[str]:
    """記録の情報 pins: ピンの既定の名前（ch の順。名前の無い ch は ch<N>）。後から開いた記録でもピンを名前で指せる
    （recording_correlate）。時間軸の段の名前（channel_names。利用者の名前）とは別（画面は既定の名前を訳して見せる）。"""
    names = channel_names(caps)
    count = max(int(getattr(caps, "la_channels", 0) or 0), max(names, default=-1) + 1)
    return [names.get(ch, f"ch{ch}") for ch in range(count)]
