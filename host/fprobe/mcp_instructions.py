"""MCP の案内（INSTRUCTIONS）。AI が最初に読む文章。道具の名前・引数は API の仕様（host/api/fprobe_api.json）が正で、ここは使い方の流れだけ。"""

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
  `fpga_obs_open`（標本型の極小の FPGA は TCK ≤ 対象の clk / 8 の half_period・words 1・sync_period 0。鎖に他の装置が
  あれば chain の ir_len・ir_before・ir_after・dr_before・dr_after。始めの ID の応答を待つ上限は id_scans で渡す。FPGA の
  観測の機能が無い構成（Primer 等）では使えない: `probe_info` の features で確かめる）→ `events_start` →
  `fpga_obs_signals_set`（信号の名前）。値は `fpga_obs_read`、条件の成立回数は `fpga_obs_count`（last を渡す）。
  両側の今は `system_status`（MCU の番地の値と FPGA の各組の最新の値。番地は
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
