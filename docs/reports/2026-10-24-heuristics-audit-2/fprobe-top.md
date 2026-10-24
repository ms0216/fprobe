# ヒューリスティック監査 2 回目: host/fprobe 直下（2026-10-24）

## 範囲と方法

- 範囲: `host/fprobe/*.py`（サブフォルダは含まない）132 ファイル。除外: `host/fprobe/plugins`・テスト・`tests/fake_*`。
- 方法: 各ファイルを先頭から末尾まで読んだ（grep での拾い読みはしていない）。1 回目の監査（docs/reports/2026-10-23-heuristics-audit/）は読んでいない。
- 基準: CLAUDE.md「fprobe 本体にヒューリスティックを入れない」。経験則の閾値・仮定に頼る推定・それらによる判定や並べ替え、
  名前の慣習による判定、暗黙の既定の境界値、打ち切りの未申告を対象にした。画面は考えない層とした。
- 分類: **H** = ヒューリスティック（原則に反する）/ **?** = 判断がつかない（理由を付ける）。コードは変えていない。

## H（ヒューリスティック）

| # | 場所 | コード | なぜ |
|---|---|---|---|
| H1 | debug.py:466-468 | `vectors = min(s.address for s in self.symbols.segments)` | ベクタ表を「最も低い番地の区画」と仮定する（VTOR や ELF の印を見ない）。callgraph.vector_table の方針と逆。 |
| H2 | debug.py:400-412 `_simple_backtrace` | CFI が無いとき LR を呼び出し元とする | 仮定（LR が戻り先のまま）による推定。Frame に推定である印（note）が付かない。 |
| H3 | probe_profile.py:242-261 `_vector_names` | 最初の区画の先頭をベクタ表として IRQ 名を付ける | 配置の仮定で名前（判定）を付ける。外れても使う側は気づけない。 |
| H4 | heap.py:43, 326-332 | `NANO_BREAK_NAMES = ("__sbrk_heap_end","heap_end","heap_end.0","heap_end.1")` | 変数名の慣習で program break を決める。 |
| H5 | heap.py:44, 214-222 | `HEAP_REGION_TABLES = ("xHeapRegions","xHeapRegion")` | アプリ側の変数名の慣習でヒープ領域の表を探す。 |
| H6 | liveness.py:28 ＋ probe_safety.py:89-103 `_tick_address` | `TICK_VARIABLES = ("xTickCount","curr_tick","_tx_timer_system_clock","OSTickCtr","uwTick")` | 名前の慣習で最初に見つかった変数をティックとし、それが止まったら tick_stopped（fatal）と判定する。 |
| H7 | probe_swo.py:37, 218-238 | `_TRACE_PIN_REGISTERS`（SVD のレジスタ名で TRACE_IOEN 等を探す）、`trace_pin=True` 既定 | レジスタ名の慣習で書き込み先を決め、既定で書く。 |
| H8 | probe_etm.py:143-146 | `max(streams, key=lambda i: len(streams[i]))` | trace_id 未指定で「最も多い ID」を選ぶ暗黙の判断。 |
| H9 | recording_long.py:518-524 | 間引いた stream の件数を段 0 の bin から幅で按分 | 一様分布の仮定による推定。density は inexact を返さない。 |
| H10 | symbols.py:280-298 `after_prologue` | prologue_end が無いと「次の行の始まり」 | GDB 流の経験則を関数名ブレークポイントの既定にしている。 |
| H11 | run.py:135-138 | defmt-test: 次の試験が始まったら前を passed | 終了の印を見ずに合否を推定する。 |
| H12 | probe_stack.py:29, 205 | `fill` 既定 `0xCD` | 塗りの記録が無いと IAR の慣習の値で使用量を出す（notes はある）。境界値が暗黙の既定。 |

## ?（判断がつかない）

| 場所 | 一言 | 理由 |
|---|---|---|
| arm_debug.py:234 / arm_a_target.py:94 / monitor.py:308 / riscv_target.py:95-100 / probe_snapshot.py:79-81 | 停止理由の既定値（ビットが無いとき "user" 等） | レジスタの事実の名付けとも、推定とも取れる。 |
| arm_debug.py:214-224 | 停止理由のビットの優先順 | 同時成立時の順は仕様か経験則か決めきれない。 |
| stepping.py:191-194 `_is_call` | LR の変化で BL/BLX とみなす | ステップの制御であり測定結果ではないが、レジスタの変化からの推定。 |
| stepping.py:270-275 `_returned` | CFI が無いと「関数が変わった = 戻った」 | 末尾呼び出しで誤る推定。GDB 互換の制御。 |
| aux_mcu.py:42-47 | 補助の ADC の換算の既定 | 校正値か経験則か、出所が読み取れない。 |
| callgraph.py:154, 306-316 | 呼び出しの検出・表の解釈 | 命令の意味による事実か、慣習による推定かの境界。 |
| correlate.py:1, 27/374, 525-540 | auto の候補の作り方・並び | 並びは preceded の順で事実だが、候補の選び方に既定の範囲がある。 |
| diagnose.py:104, 108-109, 160-162, 196, 259 | 診断の判定文 | 診断は判断そのものだが、利用者の要求の機能。 |
| disasm.py:27/205, 368-369 | データ/コードの区別 | 印（$t/$d）が無いときの扱い。 |
| fault.py:154-164 | 故障原因の文の選び方 | ビットの事実の名付けか解釈か。 |
| flash.py:52-55, 173-186 | 書き込みの区画・アルゴリズムの選択 | 既定の選択が暗黙。 |
| fpga_obs.py:116/245, 240-243 | stuck の判定 | 答えない回数の境界。 |
| heap.py:142-151, 361, 363-365, 463/565, 464/479 | 割り当て器の構造の推定 | 型情報か名前の慣習か混在。 |
| inject.py:284 | 既定の書き込み | 既定値の妥当性。 |
| memview.py:197 | 表示の既定 | 画面寄りの層か本体か。 |
| symbols.py:206, 307 | シンボルの選び方 | 同じ番地の複数シンボルの選択。 |
| packs.py:62 | パックの選択 | 名前の照合の規則。 |
| probe_cycles.py:134-136 | 周期の換算 | 既定のクロックの扱い。 |
| probe_elf_watch.py:126 | ELF の変化の判定 | mtime による判定。 |
| probe_formats.py:183-195, 288, 659-660 | 書式の推定 | 型の無い値の書式の既定。 |
| probe_gdb.py:323-337 | GDB への停止理由の対応 | プロトコルの対応か推定か。 |
| probe_observe.py:661 | 観測の既定 | 既定値の妥当性。 |
| probe_rtos_analysis.py:123-126 | RTOS の状態の解釈 | 構造体の読み方。 |
| recording.py:71 / recording_stream.py:34 | 既定の上限 | 打ち切りの申告の有無。 |
| run.py:42, 60-151, 337-338, 496-498 | 試験の出力の解釈 | 試験フレームワークの出力の規則に依る。 |
| stackusage.py:110-111, 251-256 | 間接呼び出しの扱い | 欠けとして示すか推定か。 |
| timeline_rows.py:~170, ~210 | 段の tone 規則 | 画面寄りの色分けの規則。 |
| timesync.py:17 | 時刻合わせの既定 | 不確かさの扱い。 |
| elfsize.py:346-348 | 区画の分類 | 区画名による分類（名前の慣習）。 |
| caps.py:191-194 | ケイパビリティの既定 | 旧い機器の申告が無いときの既定。 |
| timeline_fpga.py:29-34 | FPGA の段の既定 | 既定の組・名前。 |
| mcp_server.py:1464 `recording_find` | `found[:1000]` | count は返すが切った上限を返さない。 |
| mcp_server.py:1469 `recording_intervals` | `stats["intervals"][:1000]` | 切ったことと上限を返さない。 |
| mcp_server.py:1866, 1917 | `fpga_obs_read(last=2000)`・`system_status(fpga_last=2000)` | 窓の大きさが暗黙の既定（fpga_obs_count は既定なしなのと不揃い）。 |
| mcp_server.py:1963, 1975 | UART の既定 115200 | 設定の既定であり判定ではない。 |
| mcp_server.py:2086-2089 `_cortex_m` | RISC-V でも Cortex-A でもない名前なら Cortex-M | 部品名による消去法の判定。 |

## 読んだファイル（132、すべて先頭から末尾まで読んだ）

__init__.py, __main__.py, arm11_debug.py, arm_a_target.py, arm_debug.py, armv7a_debug.py, aux_flash.py, aux_mcu.py,
bitstreams.py, callgraph.py, calling.py, caps.py, code_at.py, collector.py, connect_scripts.py, control.py, coresight_id.py,
correlate.py, debug.py, device.py, diagnose.py, disasm.py, elfsize.py, eth_link.py, events.py, fault.py, flash.py, flash_bp.py,
fpga_obs.py, fpga_signals.py, frame_link.py, generated.py, gpio.py, halt_risk.py, heap.py, hooks.py, inject.py, itm.py,
jtag_chain.py, la.py, la_measure.py, liveness.py, mcp_server.py, memory.py, memview.py, merge.py, monitor.py, multidrop.py,
obs_condition.py, packs.py, probe_analysis.py, probe_breakpoints.py, probe_connect.py, probe_cycles.py, probe_decode.py,
probe_design.py, probe_elf_files.py, probe_elf_watch.py, probe_etm.py, probe_execution.py, probe_flash.py, probe_flash_bp.py,
probe_formats.py, probe_gdb.py, probe_gdb_trace.py, probe_gpio.py, probe_heap.py, probe_hooks.py, probe_inspect.py,
probe_linux.py, probe_longrec.py, probe_lowlevel.py, probe_monitor.py, probe_multicore.py, probe_names.py, probe_observe.py,
probe_profile.py, probe_profile_export.py, probe_rtos.py, probe_rtos_analysis.py, probe_rules.py, probe_safety.py,
probe_snapshot.py, probe_stack.py, probe_streams.py, probe_swo.py, probe_terminal.py, probe_testpoint.py, probe_timeline.py,
probe_ui_state.py, probe_user.py, probe_watch.py, probe_window.py, probe_xtrig_out.py, pyocd_targets.py, raw_dap.py,
recording.py, recording_long.py, recording_sql.py, recording_stream.py, recording_summary.py, riscv_dm.py, riscv_target.py,
rtt.py, rules.py, run.py, sampler.py, semihosting.py, serial_link.py, session.py, sigrok.py, snapshot.py, spi.py,
stackusage.py, stepping.py, stm32_option_bytes.py, streams.py, svd.py, swo_trace.py, symbols.py, thumb_step.py, timeline.py,
timeline_fpga.py, timeline_rows.py, timesync.py, transport.py, uart_flow.py, usertrace.py, watch_buffer.py, watch_trigger.py,
wiring.py, xtrig.py
