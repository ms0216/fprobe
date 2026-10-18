<!-- 自動生成ファイル。直接編集しないこと（python -m fprobe.api gen で再生成）。生成元: host/api/fprobe_api.json -->
# fprobe API

JSON-RPC 2.0 over WebSocket（`ws://127.0.0.1:7820/api`）。API の版 1。仕様: docs/superpowers/specs/2026-10-17-host-daemon-design.md

## session.hello

種類: `read`。名乗る（最初に必ず呼ぶ）。api_version が合わなければ断られる。

| 引数 | 型 | 必須 |
|---|---|---|
| `name` | `string` | ○ |
| `role` | `"human" | "ai" | "script"` | ○ |
| `api_version` | `number` | ○ |
| `token` | `string | null` |  |

## session.status

種類: `read`。デーモンの状態（API の版、機器、利用者の一覧、一時停止の状態、保留の数、見張りが最後に見たターゲットの状態、GDB サーバの待ち受けのポート gdb.port（開いていなければ null））。

## session.subscribe

種類: `read`。受け取る通知の種類を設定する。

| 引数 | 型 | 必須 |
|---|---|---|
| `topics` | `Array<"activity" | "control" | "target.state" | "uart.data" | "swo.data" | "clients" | "hook" | "elf.changed" | "flash.progress" | "ui.state" | "recording">` | ○ |

## session.activity.list

種類: `read`。このデーモンの起動以降の操作の記録を、番号の小さい順に返す（before より前の、最後の limit 件）。

| 引数 | 型 | 必須 |
|---|---|---|
| `before` | `number` |  |
| `limit` | `number` |  |

## session.control.pause

種類: `read`。一時停止（human だけ）。以後 human 以外の control の要求は保留される。

## session.control.resume

種類: `read`。再開（human だけ）。保留していた要求を受けた順に処理する。

## ui.state.get

種類: `read`。画面の状態（人の画面・別の窓・AI が共有する「いま見ているところ」）: view（時間軸の視点 source・t0・t1。単位は捕捉 = サンプル番号、記録 = タイムベース）、cursors（selection・mouse・reference の 3 種。同じ単位）、rows（開いている段の id: wave:<ch> = ロジアナ、ほかは記録の段の名前。表示の順。null = まだ知らない）、event（選んだ出来事 stream・t・t_end・kind・text・row）、frame（呼び出し履歴で選んだフレーム）、sampling（データ サンプリングの式の一覧 expressions = [{expr, plot, axis}] と周期 rate_hz。取っているかは watch.status）、dashboard（操作パネルの部品の配置 {version: 1, widgets: [{id, kind, expr, label, min, max, unit, digits, value, w, h}]}）、rev（変わるたびに増える）。 pointers は窓・利用者ごとのマウスのカーソル [{key, t, source, name, role, time}]。

## ui.state.set

種類: `read`。画面の状態を変える（渡したものだけ。人の画面がその視点・カーソル・段・出来事・フレームに移る）。cursors は渡したカーソルだけ（null で消す）。sampling はデータ サンプリングの式の一覧と周期（{expressions: [{expr, plot, axis}], rate_hz}。人の画面の一覧がそれになる）。dashboard は操作パネルの部品の配置（{version: 1, widgets: [{id, kind = gauge / number / bar / chart / button / slider / toggle / lamp, expr, label, min, max, unit, digits, value, w, h}]}。64 個・64 KiB まで。人の画面・遠隔の画面の操作パネルがそれになる。足りない・範囲の外の値は画面が直す）。clear に挙げた項目（view / cursors / rows / event / frame / sampling / dashboard）は既定に戻す。origin は変えた窓の印（通知 ui.state にそのまま載る）。結果は状態と changed（変わった項目）。AI が人に場所を見せる（例: 異常の区間に view と cursors を置く）のに使う。 cursors.mouse を渡すと、その窓・利用者のマウスのカーソル（pointers。名前・役割はデーモンが付ける）も変わる（人の画面がほかの窓・AI の指す時刻を時間軸に描く。null で消える）。

| 引数 | 型 | 必須 |
|---|---|---|
| `view` | `Record<string, unknown> | null` |  |
| `cursors` | `Record<string, unknown> | null` |  |
| `rows` | `Array<string> | null` |  |
| `event` | `Record<string, unknown> | null` |  |
| `frame` | `number | null` |  |
| `clear` | `Array<"view" | "cursors" | "rows" | "event" | "frame" | "sampling" | "dashboard"> | null` |  |
| `origin` | `string | null`（MCP の道具には出さない） |  |
| `sampling` | `Record<string, unknown> | null` |  |
| `dashboard` | `Record<string, unknown> | null` |  |

## probe.info

種類: `read`。プローブのケイパビリティ・タイムベースの現在値・ターゲット接続の有無・個体（device = つなぎ方と番地、name = 個体名、version = ビットストリームの版: 入力の要約・ビルドの日時・git）。

## probe.name.set

種類: `control`。今の機器（USB のシリアル番号・COM ポート・IP）に個体名を付ける（~/.fprobe/probes.json）。name を省くと名前を外す。以後 fprobe-daemon / fprobe-mcp を --name で起動でき、`probe_info` が名前を返す。

| 引数 | 型 | 必須 |
|---|---|---|
| `name` | `string | null` |  |

## probe.sampler.status

種類: `read`。DAP サンプラ（変数を止めずに読む FPGA の回路。1 つだけ）をいま使っている道具（機器には触れない）: user（watch = 変数のライブ観測 / rtos = タスクの時間軸 / profile = PC のサンプリング / rtt = RTT の見張り / clock = CPU クロックの実測。空きなら null）、release（止める API。clock は自分で終わるので null）、since（使い始めた時刻、UNIX 秒）。ほかの道具が使っている間に watch.set・rtos.timeline.start（sampled）・profile.start（pcsr）・rtt.start（sampler）を呼ぶと誤り。

## probe.design.get

種類: `read`。Primer の構成（ビットストリーム）: 今のデザイン・線（SWD / JTAG）と機能（サンプラ・故障注入・専用の入力・ロジアナの流し続けにイベントも載せる la_stream_events）の有無・切り替えられる構成（線・ビットストリームの場所と有無）・自動の書き替えの有無。`target_connect` は要る線の構成でなければ自動で書き替える（JTAG は jtag_chain か Cortex-A・ARM11 のターゲット）。

## probe.design.switch

種類: `control`。Primer の構成を書き替える（ターゲット未接続のときだけ）。design（"fprobe" = 標準: SWD・サンプラ・故障注入、"fprobe_jtag" = JTAG と SWD、"fprobe_la" = SWD とロジアナの専用の入力 6 本（J14）・ロジアナの流し続けにイベントも載せる（サンプラ・故障注入なし））か protocol（"swd" / "jtag"）で指定する。今と同じなら何もしない。書き替えるとロジアナ・記録・流れの状態も消える。 書き替えると FPGA の出力の設定は消えるので、出力に使っていた端子は無効（駆動しない）で取り戻す（結果の outputs = {restored, failed}）。

| 引数 | 型 | 必須 |
|---|---|---|
| `design` | `"fprobe" | "fprobe_jtag" | "fprobe_la" | null` |  |
| `protocol` | `"swd" | "jtag" | null` |  |

## probe.csr.read

種類: `read`。CSR を読む（バイトアドレス、32 ビット語 count 個）。レジスタ表は docs/registers.md。

| 引数 | 型 | 必須 |
|---|---|---|
| `address` | `number` | ○ |
| `count` | `number` |  |

## probe.csr.write

種類: `control`。CSR に 32 ビット語の列を書く。

| 引数 | 型 | 必須 |
|---|---|---|
| `address` | `number` | ○ |
| `words` | `Array<number>` | ○ |

## probe.voltage

種類: `read`。ターゲットの電源電圧（補助マイコン Pico の ADC、V）。live が False なら Pico が計測値を送っていない。
over_range が True なら測れる上限を超えている（volts は None。配線の分圧を確認）。

## probe.pins.read

種類: `control`。デバッグコネクタのピンの状態（swclk = SWCLK/TCK、swdio = SWDIO/TMS、nreset）。変えない。fprobe は TDI・TDO・nTRST を返さない（not_reported）。接続中も読める。

## probe.pins.write

種類: `control`。ピンを個別に High（1）/ Low（0）にする（省いたピンは変えない）。ターゲット未接続のときだけ（接続中は断る）。hold_us を与えるとその時間の後で元に戻す（例 nreset=0, hold_us=1000 で 1 ms のリセットの脈）。書いた後の状態を返す。

| 引数 | 型 | 必須 |
|---|---|---|
| `swclk` | `0 | 1 | null` |  |
| `swdio` | `0 | 1 | null` |  |
| `nreset` | `0 | 1 | null` |  |
| `hold_us` | `number` |  |

## target.connect

種類: `control`。ターゲットに接続する。既定は SWD。jtag_chain（JTAG チェーンの IR 長、TDO 側から。例: STM32 は [4, 5]）を
指定すると JTAG。connect_mode: halt / attach / under-reset。target="auto" なら先に部品を判別して pyOCD のターゲット名を選ぶ（結果の auto に判別の内容。JTAG だけのターゲットなら jtag_chain も補う）。
script: 接続・リセット等の手順を上書きする pyOCD のユーザスクリプト（"builtin:<名前>" かファイル。target_script_check で検査できる）。
core: コアごとに DP を持つ複数ドロップの部品（RP2040）のコアの番号。target="rp2040" と組む（pyOCD のターゲット rp2040_core0 / rp2040_core1 を選ぶ）。
RISC-V: target="rp2350_riscv" で RP2350 の Hazard3 に（pyOCD を使わず、APB-AP の向こうの Debug Module を直接）。core はハートの番号、connect_mode は halt / attach。使えるのは状態・停止・再開・ステップ・レジスタ（pc・x0〜x31・ABI の名前・主な CSR）・メモリ（システムバス）・ブレークポイント（トリガ）。リセット・Flash・SWO・RTOS 等は断る。
Cortex-A・ARM11（JTAG で直接。pyOCD を使わない）: target="bcm2836"（Raspberry Pi 2、Cortex-A7 × 4。core はコアの番号）/ "cortex_a"（ARMv7-A、コアは ROM テーブルから）/ "bcm2835"（Pi Zero・1、ARM1176）/ "arm11"。チェーンの装置は IDCODE で探す（jtag_chain で IR 長の列を与えてもよい）。connect_mode は halt / attach。使えるのは状態・停止・再開・ステップ・レジスタ（r0〜r15・sp・lr・pc・cpsr）・メモリ（コアを通す。番地はコアの見る番地 = MMU が有効なら仮想番地。書いた範囲はキャッシュを揃える）・ハードのブレークポイント / ウォッチポイント。止めるのは選んだコアだけ。リセット・ベクタキャッチ・Flash・SWO・RTOS 等は断る。

| 引数 | 型 | 必須 |
|---|---|---|
| `target` | `string` |  |
| `frequency_hz` | `number` |  |
| `connect_mode` | `string` |  |
| `jtag_chain` | `Array<number> | null` |  |
| `script` | `string | null` |  |
| `core` | `number | null` |  |

## target.disconnect

種類: `control`。ターゲットから切断する。

## target.status

種類: `read`。コアの状態（RUNNING / HALTED 等）と、停止中なら PC・場所（where、分かれば function・file・line）。つないでいる先（target・frequency_hz・wire = swd / jtag）も。走っているのに致命的エラーで回り続けていれば fatal = {kind（lockup = ロックアップ / fatal_halt = RTOS・ランタイムの止まって回る関数 / fault_loop = 故障の処理 / stuck = 狭いループでティックが止まった）, function, since（見つけ始めた時刻、UNIX 秒）, evidence（手がかり: clue = lockup / pc_in / pc_loop / tick_stopped）}。材料は止めずに読む DHCSR・DWT_PCSR・ティックの変数。複数ドロップの部品（RP2040）なら core（接続中のコア）と cores = [{core, selected, state, pc・where（止まっていれば）} または {core, selected, error}]。接続していないコアは TARGETSEL で DP を一時的に選び直して DHCSR を読む（DAP サンプラが動いている間は error）。RISC-V なら arch = "riscv"・hart・debug_module（版・プログラムバッファ・システムバス・ハート）。Cortex-A・ARM11 なら arch = "armv7-a" / "armv6"・core・cores・breakpoints・watchpoints・jtag_chain・tap（Cortex-A は debug_base・apb_ap・discovery = rom_table / part_table も）。

## target.core.select

種類: `control`。接続するコアを替える（複数ドロップの部品（RP2040）。同じ速度で、既定は止めずに attach でつなぎ直す）。以後の道具（ブレークポイント・レジスタ・ステップ等）はそのコアに効く。target.status と同じものを返す。

| 引数 | 型 | 必須 |
|---|---|---|
| `core` | `number` | ○ |
| `connect_mode` | `string` |  |

## target.fault

種類: `control`。止まったコアの故障の解析: 原因（CFSR・HFSR の各ビットの意味と手がかり。例 NOCP = FPU が無効）、故障の番地（MMFAR・BFAR）、故障した命令の場所（関数・ファイル:行）とその時のレジスタ。故障のハンドラで止まると debug.run_until_halt の結果にも要約が付く。

## target.halt

種類: `control`。コアを停止する。core: 複数ドロップの部品（RP2040）で接続していないコアを止める（そのコアの状態を返す）。

| 引数 | 型 | 必須 |
|---|---|---|
| `core` | `number | null` |  |

## target.resume

種類: `control`。コアの実行を再開する。core: 複数ドロップの部品（RP2040）で接続していないコアを再開する（そのコアの状態を返す）。

| 引数 | 型 | 必須 |
|---|---|---|
| `core` | `number | null` |  |

## target.step

種類: `control`。1 命令ずつ count 回ステップ実行する。

| 引数 | 型 | 必須 |
|---|---|---|
| `count` | `number` |  |

## target.reset

種類: `control`。ターゲットをリセットする（halt=True ならリセット直後（リセットのベクタキャッチ）で停止）。kind: default（pyOCD の既定）/ system（AIRCR.SYSRESETREQ。周辺も）/ core（AIRCR.VECTRESET。コアだけ、ARMv7-M だけ）/ hardware（nRESET の線）/ emulated（デバッガがレジスタを初期値にする）。コアに無い種類は断る（黙って替えない）。

| 引数 | 型 | 必須 |
|---|---|---|
| `halt` | `boolean` |  |
| `kind` | `"default" | "system" | "core" | "hardware" | "emulated"` |  |

## target.vector_catch

種類: `control`。ベクタキャッチ（例外の入口でコアを止める。DEMCR の VC_*）を catch の集合にする（"all" ですべて、[] で無し）。名前: core_reset / mem_manage / no_coprocessor / check_error / state_error / bus_fault / interrupt_error / hard_fault / secure_fault。結果の enabled はコアから読み直したもの、コアに無いもの（ARMv6-M は hard_fault・core_reset だけ）は unsupported。読むだけなら target.vector_catch.get。止まると debug.run_until_halt・debug.step の reason が vector_catch になり、故障なら target.fault の要約が付く。

| 引数 | 型 | 必須 |
|---|---|---|
| `catch` | `Array<"core_reset" | "mem_manage" | "no_coprocessor" | "check_error" | "state_error" | "bus_fault" | "interrupt_error" | "hard_fault" | "secure_fault" | "all">` | ○ |

## target.vector_catch.get

種類: `control`。いまのベクタキャッチ（DEMCR の VC_*）を読む: enabled（入っているもの）・available（名前の一覧）。状態を変えない。

## target.debug_mode.get

種類: `control`。いまのデバッグの方式（U37。J-Link の Monitor Mode Debugging 相当）。mode: halt（停止方式。ブレークポイント・ステップでコア全体を止める）/ monitor（モニタ方式。デバッグ用の例外 DebugMon の中で止まり、DebugMon より優先度の高い割り込みは止まっている間も動き続ける）。available: ファームにモニタ（host/firmware/monitor）があるか（無ければ reason）。あれば box（箱の番地）・priority（DebugMon の優先度。数が小さいほど高い）・prio_bits（実装された優先度のビット）・keeps_running（止まっている間も動く例外・割り込み: exception・name・priority）・fpu・stack_size・stack_overflow（モニタのスタックが溢れた）、モニタ方式なら state（stopped / running）・stops（止まった回数）も。

## target.debug_mode.set

種類: `control`。デバッグの方式を切り替える（U37）。mode: halt / monitor。止まっていれば同じ PC で止まったまま切り替わる。monitor ではターゲットのファームに組み込んだモニタ（host/firmware/monitor の DebugMon_Handler と fprobe_monitor_init）を通して、target.halt・resume・step、debug.breakpoint.*、debug.step、レジスタ、呼び出し履歴、debug.call、GDB サーバがそのまま使える。止まっている間も DebugMon より優先度の高い割り込みは動く。モニタ方式でできないこと: ベクタキャッチ、止めてのリセット（target.reset の halt・target.program_ram）、Flash の書き込み・消去、Flash の BKPT のブレークポイント（比較器だけ）、DebugMon 以上の優先度で走る処理の中のブレークポイント（HardFault になる）。クロストリガの停止の書き込みは DEMCR.MON_PEND に替わる。priority: DebugMon の優先度（0〜255、実装のビットに丸める）。結果は target.debug_mode.get と同じ。

| 引数 | 型 | 必須 |
|---|---|---|
| `mode` | `"halt" | "monitor"` | ○ |
| `priority` | `number | null` |  |

## target.registers.read

種類: `control`。コアレジスタを読む（省略時 r0..r12, sp, lr, pc, xpsr）。（RISC-V は pc と x1〜x31 の ABI の名前、Cortex-A・ARM11 は xpsr の代わりに cpsr）

| 引数 | 型 | 必須 |
|---|---|---|
| `names` | `Array<string> | null` |  |

## target.registers.write

種類: `control`。コアレジスタに書く（停止中）。

| 引数 | 型 | 必須 |
|---|---|---|
| `name` | `string` | ○ |
| `value` | `number` | ○ |

## target.memory.read

種類: `control`。ターゲットのメモリを読み、16 進文字列（アドレス順のバイト）で返す。

| 引数 | 型 | 必須 |
|---|---|---|
| `address` | `number` | ○ |
| `length` | `number` | ○ |

## target.memory.write

種類: `control`。ターゲットのメモリ（RAM・周辺）に 16 進文字列のバイト列を書く。Flash は `target_flash` を使う。

| 引数 | 型 | 必須 |
|---|---|---|
| `address` | `number` | ○ |
| `data_hex` | `string` | ○ |

## target.dp.read

種類: `control`。ターゲットの DP のレジスタを直接読む。register は名前（DPIDR / CTRL/STAT / SELECT / RDBUFF / TARGETID / DLPIDR / EVENTSTAT / BASEPTR0）か番地（bit 7:4 が DPBANKSEL。例 0x24 = TARGETID）。接続中は pyOCD を通し、未接続なら DP だけを起こして読む（target_diagnose の後はその線と速度、ほかは SWD 1 MHz）。path に経路。

| 引数 | 型 | 必須 |
|---|---|---|
| `register` | `number | string` | ○ |

## target.dp.write

種類: `control`。ターゲットの DP のレジスタに書く（例 ABORT = 0x0 に 0x1E で誤りの印を消す、CTRL/STAT で電源を求める）。経路は target_dp_read と同じ。

| 引数 | 型 | 必須 |
|---|---|---|
| `register` | `number | string` | ○ |
| `value` | `number` | ○ |

## target.ap.read

種類: `control`。ターゲットの AP のレジスタを直接読む。ap は ADIv5 なら APSEL（0〜255）、ADIv6 なら AP の番地。register は AP の中の番地（ADIv5 の MEM-AP: 0x00 CSW / 0x04 TAR / 0x0C DRW / 0xF8 BASE / 0xFC IDR。ADIv6 は 0xD00 足す）。経路は target_dp_read と同じ。

| 引数 | 型 | 必須 |
|---|---|---|
| `ap` | `number` | ○ |
| `register` | `number` | ○ |

## target.ap.write

種類: `control`。ターゲットの AP のレジスタに書く（ap・register は target_ap_read と同じ）。

| 引数 | 型 | 必須 |
|---|---|---|
| `ap` | `number` | ○ |
| `register` | `number` | ○ |
| `value` | `number` | ○ |

## target.speed_test

種類: `control`。ターゲットのメモリの読みの速度（write なら書きも）を測る（接続中）。書きは RAM の番地でコアを止めてから: 試験の模様を書いて読み比べ（verified）、元の中身を書き戻す（restored）。bytes_per_s を返す。

| 引数 | 型 | 必須 |
|---|---|---|
| `address` | `number` | ○ |
| `length` | `number` |  |
| `write` | `boolean` |  |

## target.diagnose

種類: `control`。ターゲットにつながらない理由を順に調べて言葉で返す（未接続のときだけ）: 電圧 → ピン → SWD（JTAG→SWD・休眠からの起こし・複数ドロップ・速度を下げて。どの ACK か）→ JTAG のチェーン → DP（デバッグ電源）→ AP・ROM テーブル → コア（ロックアップ等）→ 部品の判別（pyOCD のターゲット名）→ 保護（STM32 の RDP・nRF の APPROTECT）。verdict・steps・suggested_connect（target_connect の引数）を返す。

| 引数 | 型 | 必須 |
|---|---|---|
| `frequency_hz` | `number` |  |
| `max_aps` | `number` |  |
| `jtag` | `boolean` |  |

## target.script.check

種類: `read`。接続の手順のスクリプト（pyOCD のユーザスクリプト。"builtin:<名前>" かファイル）を実行せずに調べる: 定義された手順（will_connect / did_init_target / will_reset / unlock_device 等）と行、名前・引数の誤り（似た名前の提案）、構文の誤り。target_connect の script に渡す前に。

| 引数 | 型 | 必須 |
|---|---|---|
| `script` | `string` | ○ |

## target.script.list

種類: `read`。内蔵の接続の手順（target_connect の script に "builtin:<名前>"。例 builtin:stm32_debug_low_power = 低消費電力の間もデバッグ、builtin:reset_with_nreset = リセットを nRESET の線で）と、pyOCD の手順の名前・引数の一覧。

## target.option_bytes.read

種類: `control`。STM32 のオプションバイトを名前で読む（F4・L4・G4・G0。RDP と rdp_level、BOR、ウォッチドッグ、リセット、ブート、F4 は nWRP）。部品は DBGMCU の DEV_ID で判別する。変えない。

## target.option_bytes.write

種類: `control`。STM32 のオプションバイトを名前で書く（fields 例 {"nWRP": 255, "IWDG_SW": 1}。書かないビットは保つ）。RDP を Level 1 → 0 に戻す（Flash の全消去）には confirm_mass_erase、Level 0 → 1 には confirm_protect が要る（無ければ何もせず断る）。Level 2 は書かない。コアを止めてから書く。reset: F4 は書いた後でリセットして止める、L4 / G4 / G0 は OBL_LAUNCH で反映して切断する（reconnect_required）。

| 引数 | 型 | 必須 |
|---|---|---|
| `fields` | `Record<string, unknown>` | ○ |
| `confirm_mass_erase` | `boolean` |  |
| `confirm_protect` | `boolean` |  |
| `reset` | `boolean` |  |
| `timeout_s` | `number` |  |

## target.unlock

種類: `control`。保護を全消去で解除する（confirm_mass_erase が無ければ何もせず断る）。接続中: STM32 の RDP を Level 0 に（target_option_bytes_write と同じ手順）。未接続: nRF52 の CTRL-AP の ERASEALL（APPROTECT の解除）。既に解除されていれば何もしない（already_unlocked）。

| 引数 | 型 | 必須 |
|---|---|---|
| `confirm_mass_erase` | `boolean` |  |
| `reset` | `boolean` |  |
| `timeout_s` | `number` |  |

## jtag.scan

種類: `control`。JTAG のチェーンを走査する（未接続のときだけ）: 装置の数・IDCODE（設計者・名前）・IR 長（既知の表か Capture-IR から推定。ir_length_source = table / measured / guessed）。jtag_chain は target_connect にそのまま渡せる。TDO の張り付きも分かる。

| 引数 | 型 | 必須 |
|---|---|---|
| `frequency_hz` | `number` |  |
| `max_devices` | `number` |  |

## jtag.reset

種類: `control`。すべての TAP を Test-Logic-Reset に（TMS = 1 を 6 回）して Run-Test/Idle へ（未接続のときだけ）。

## jtag.shift

種類: `control`。チェーンの device 番目に IR を入れ（ほかの装置は BYPASS）、DR を dr_length ビット シフトする（未接続のときだけ）。chain は IR 長の列（TDO 側から。省略時は最後の jtag_scan）。ir 省略時は IR を変えない。取り出した ir_capture・dr_capture を返す。

| 引数 | 型 | 必須 |
|---|---|---|
| `device` | `number` |  |
| `ir` | `number | null` |  |
| `dr_value` | `number` |  |
| `dr_length` | `number` |  |
| `chain` | `Array<number> | null` |  |

## jtag.clock

種類: `control`。生のビット列を TCK ごとに出す（未接続のときだけ）。tms・tdi は時間順の "0" / "1" の文字列（tdi 省略時は 0）。TDO を同じ形で返す。TAP の状態は呼び手が管理する（ほかの JTAG の道具は Run-Test/Idle を前提にする）。

| 引数 | 型 | 必須 |
|---|---|---|
| `tms` | `string` | ○ |
| `tdi` | `string | null` |  |

## target.svd.load

種類: `control`。周辺レジスタの定義（CMSIS-SVD）を読む。path（ファイル）か device（部品名。同梱・~/.fprobe/svd・pyOCD の内蔵から探す）。どちらも無ければ `target_connect` のターゲット名から探す（周辺の道具は読み込んでいなければ自動でこれを行う）。

| 引数 | 型 | 必須 |
|---|---|---|
| `device` | `string | null` |  |
| `path` | `string | null` |  |

## target.svd.peripherals

種類: `read`。SVD の周辺の一覧（名前・番地・グループ・説明・レジスタの数）。filter は名前・グループの一部。

| 引数 | 型 | 必須 |
|---|---|---|
| `filter` | `string | null` |  |

## target.svd.registers

種類: `read`。周辺のレジスタの定義（番地・幅・アクセス・リセット値・読むと変わるか・フィールド・列挙）。

| 引数 | 型 | 必須 |
|---|---|---|
| `peripheral` | `string` | ○ |

## target.peripheral.read

種類: `control`。周辺レジスタを読んでフィールドに解読する（名前・ビット・値・列挙の名前）。path は RCC.CR（1 つ）か GPIOA（周辺の全部）。読むと状態が変わるもの（SVD の readAction、周辺の一括ではデータ・FIFO の口も）は allow_side_effects が無ければ読まず skipped に入れる。

| 引数 | 型 | 必須 |
|---|---|---|
| `path` | `string` | ○ |
| `allow_side_effects` | `boolean` |  |

## target.peripheral.write

種類: `control`。周辺レジスタに書く。fields（例 {"HSEON": 1, "MODER5": "Output"}。読み・変更・書き込み）か value（全体）。書く前と後の解読した値を返す。

| 引数 | 型 | 必須 |
|---|---|---|
| `path` | `string` | ○ |
| `fields` | `Record<string, unknown> | null` |  |
| `value` | `number | null` |  |
| `allow_side_effects` | `boolean` |  |

## target.memory.view

種類: `control`。メモリの 16 進表示（4096 バイトまで。rows と文字の dump）。unit = 8 / 16 / 32 ビット、width = 1 行のバイト数。compare なら同じ範囲の前回の読みと比べ、変わった所（changes）に印。番地にシンボル・周辺レジスタの名前（names）。

| 引数 | 型 | 必須 |
|---|---|---|
| `address` | `number` | ○ |
| `length` | `number` | ○ |
| `unit` | `8 | 16 | 32` |  |
| `width` | `number` |  |
| `compare` | `boolean` |  |
| `from_elf` | `boolean` |  |

## target.flash

種類: `control`。ELF / HEX / BIN を Flash に書く（BIN は address が要る）。erase: auto / sector（書く範囲のセクタだけ。ほかの中身は保つ）/ chip（全体を消して書く）、smart: 同じ中身のページは書かない、verify: 書いた後に読み比べて違えば誤り、reset: 書いた後にリセットして走らせる（false なら止めたまま）。段ごと（消去・書き込み）の時間と速度、検証の結果を返す。進捗は通知 flash.progress。ELF ならシンボルも読む。 既定は書く前にリセットする（アルゴリズムはリセット直後の状態を前提にする）。preserve で走っているプログラムのまま書く。

| 引数 | 型 | 必須 |
|---|---|---|
| `path` | `string` | ○ |
| `address` | `number | null` |  |
| `erase` | `"auto" | "sector" | "chip"` |  |
| `verify` | `boolean` |  |
| `smart` | `boolean` |  |
| `reset` | `boolean` |  |
| `preserve` | `boolean` |  |

## target.program_ram

種類: `control`。ELF を RAM に読み込み、そのベクタ表から起動できる状態で止める（Flash には書かない。RAM で動く試験用の
プログラム向け）。ターゲットはリセットされる。続けて `debug_run_until_halt` / `target_resume` で走らせる。

| 引数 | 型 | 必須 |
|---|---|---|
| `path` | `string` | ○ |

## debug.elf.load

種類: `control`。ELF のシンボルと行番号を読み込む（以後、場所を関数名・変数名・"ファイル:行" で指定でき、止まった場所が
関数名と行で返る）。`target_flash` で ELF を書いたときは自動で読む。

| 引数 | 型 | 必須 |
|---|---|---|
| `path` | `string` | ○ |

## debug.elf.load_demo

種類: `control`。同梱のデモの ELF（host/firmware/sram_demo/sram_demo.elf。RAM で動く STM32 の小さなプログラム）を読む（画面の Load Demo。機器・ELF が無くても各パネルの働きを見る）。結果は debug.elf.load と同じ。

## debug.elf.upload

種類: `control`。画面で選んだ ELF の中身（data_base64）をデーモンの PC の FPROBE_HOME/uploads/<name> に置いて読む（ブラウザはファイルの場所を渡せないため。遠隔の画面でも使える）。64 MB まで。結果は debug.elf.load と同じ。

| 引数 | 型 | 必須 |
|---|---|---|
| `name` | `string` | ○ |
| `data_base64` | `string` | ○ |

## debug.symbol.info

種類: `read`。場所（関数名・変数名・"ファイル:行"・"0x..."）の番地と、その番地の関数名・ファイル:行。

| 引数 | 型 | 必須 |
|---|---|---|
| `location` | `string` | ○ |

## debug.source.list

種類: `read`。読み込んだ ELF の行番号の情報が参照するソース（name・path・この PC にあるか exists）。

## debug.source.read

種類: `read`。ソースの本文（text）と命令のある行（lines、ブレークポイントを置ける行）。name は `debug_source_list` の名前。
ELF が参照するファイルだけ読める。

| 引数 | 型 | 必須 |
|---|---|---|
| `name` | `string` | ○ |

## debug.breakpoint.set

種類: `control`。ブレークポイントを置く。location: 関数名・"ファイル:行"・"0x..."。condition: C の式（偽なら止まらない。ローカル変数・比較・論理演算も使える）、ignore_count: 最初の n 回は止まらない、log: 当たるたびに読む式の一覧（[] なら式なしで message だけ。止まらずに debug.breakpoint.log と記録の stream "debug" に残す。message は添える文）。動作のあるブレークポイントは当たるたびにコアを数 ms 止める（速い頻度の観測は watch・RTT・テストポイントで）。

| 引数 | 型 | 必須 |
|---|---|---|
| `location` | `string` | ○ |
| `condition` | `string | null` |  |
| `ignore_count` | `number` |  |
| `log` | `Array<string> | null` |  |
| `message` | `string | null` |  |

## debug.breakpoint.clear

種類: `control`。ブレークポイントを外す（location 省略ですべて。ウォッチポイントも含めて外す）。

| 引数 | 型 | 必須 |
|---|---|---|
| `location` | `string | null` |  |

## debug.breakpoint.list

種類: `read`。置いているブレークポイントとウォッチポイント。各ブレークポイントは address・location・場所（where・function・file・line）・kind（hw / flash / sw。接続中）、動作があれば action（condition・ignore_count・log・message・hits・last_error）、道具が置いたものは tool。

## debug.breakpoint.log

種類: `read`。ログ・ブレークポイントの記録（当たった場所・回数・式の値）。seq が since より後のものを古い順に limit 件。next を次の since に渡す。記録中に当たった項は記録のタイムベースの時刻 t を持つ（時間軸のその時刻へ飛べる）。

| 引数 | 型 | 必須 |
|---|---|---|
| `since` | `number` |  |
| `limit` | `number` |  |

## debug.breakpoint.flash

種類: `control`。Flash 上の無制限ブレークポイント（J-Link の Unlimited Flash Breakpoints 相当）の様子と設定。ハードの比較器が尽きたら、Flash の命令を BKPT に書き換えてブレークポイントにする（debug.breakpoint.set・GDB の Z0・行単位のステップがそのまま使う）。書くのは再開の直前だけ、セクタごとにまとめて 1 回。外したものは同じセクタを書くついでに戻し（それまでは当たっても黙って踏み越える）、ブレークポイントの上からの再開・ステップは元の命令を Flash の外で実行する（書き換えない）。切断・終了で元に戻し、異常終了で残ったものは次の接続で戻す（target.connect の flash_breakpoints_recovered）。enabled: 使うか（既定 true）。dormant_limit: 外した BKPT に何回当たったら戻すか。scratch: 元の命令を実行する RAM の番地（8 バイト、止めている間だけ借りる。省略時は Flash の書き込みアルゴリズムの RAM の先頭）。返す: hardware（比較器の番地）、flash（番地・state written / pending・場所）、dormant（外したが Flash に残る BKPT）、stats（commits・sector_writes・emulated・displaced・rewrites・dormant_hits・restored）、journal（書き換えの記録のファイル）。

| 引数 | 型 | 必須 |
|---|---|---|
| `enabled` | `boolean | null` |  |
| `dormant_limit` | `number | null` |  |
| `scratch` | `number | null` |  |

## debug.breakpoint.flash_restore

種類: `control`。いま Flash に書いてある BKPT をすべて元の命令に戻す（Flash を別のツールに渡す前等）。置いたブレークポイントは残り、次の再開で書き直す。返す restored: 戻した数。

## debug.watchpoint.set

種類: `control`。ウォッチポイントを置く（変数名か "0x..."）。kind: write / read / access。size は変数名なら自動。

| 引数 | 型 | 必須 |
|---|---|---|
| `location` | `string` | ○ |
| `kind` | `string` |  |
| `size` | `number | null` |  |

## debug.watchpoint.clear

種類: `control`。ウォッチポイントを外す。

| 引数 | 型 | 必須 |
|---|---|---|
| `location` | `string` | ○ |

## debug.run_until_halt

種類: `control`。実行を再開し、止まるまで（ブレークポイント・ウォッチポイント等）か timeout_s まで待つ。時間切れなら止める。
返す reason: breakpoint / watchpoint / step / user / timeout 等、where: 止まった関数名 + オフセットとファイル:行。

| 引数 | 型 | 必須 |
|---|---|---|
| `timeout_s` | `number` |  |

## debug.step

種類: `control`。ソース行単位のステップ（ELF の行表と CFI）。mode: into（呼び出しに入り、前置きの後で止まる。行の情報の無い関数は踏み越える）/ over（呼び出しは踏み越える）/ out（いまの関数から出る。return_value に戻り値）/ until（over と同じだが後ろへ戻るループでは止まらない。location（関数名・"ファイル:行"・"0x..."）を指定するとそこまで、いまの関数から戻ればそこでも止まる）。mask_interrupts: 1 命令ずつ進める間は割り込みを止める（DHCSR.C_MASKINTS。呼び出しを踏み越えて走る間は割り込みも動く）。False なら割り込みに入っても踏み越える。返す reason: step（終えた）か、途中で止まった理由（breakpoint / watchpoint / vector_catch / fault / timeout / exit）と、止まった関数名・ファイル:行。故障なら fault に要約。

| 引数 | 型 | 必須 |
|---|---|---|
| `mode` | `"into" | "over" | "out" | "until"` |  |
| `location` | `string | null` |  |
| `timeout_s` | `number` |  |
| `mask_interrupts` | `boolean` |  |

## debug.call

種類: `control`。止まったコアで関数を引数付きで実行し、戻り値を得る（AAPCS。DWARF の型で引数を置き、戻り値を解読する）。function: 関数名か "0x..."。arguments の各要素: 数、C の式（文字列。いまのフレームで評価。例 "&sensors[1]"・"MODE_RUN"）、{"string": 文字列}・{"bytes": 16 進}（スタックの下に置いてその番地を渡す。呼んだ後の中身を buffers に返す）。戻り先はリセットの入口に置く踏み台のブレークポイント。呼んだ関数の中のブレークポイントでは止まらない。終われば（timeout_s の時間切れ・故障で打ち切っても）レジスタとスタックの下のメモリを元に戻す（故障の例外に入ったままなら restored = false）。disable_interrupts: 呼ぶ間は PRIMASK = 1。単体テスト・AI の試行に使う。

| 引数 | 型 | 必須 |
|---|---|---|
| `function` | `string` | ○ |
| `arguments` | `Array<number | string | Record<string, unknown> | Record<string, unknown>> | null` |  |
| `timeout_s` | `number` |  |
| `disable_interrupts` | `boolean` |  |

## debug.variable.read

種類: `control`。変数を名前で読む（1 / 2 / 4 / 8 バイトは整数、それ以外は 16 進文字列）。index は 4 バイト要素の配列の添字。

| 引数 | 型 | 必須 |
|---|---|---|
| `name` | `string` | ○ |
| `index` | `number | null` |  |

## debug.variable.write

種類: `control`。変数を名前で書く（1 / 2 / 4 / 8 バイトの整数、または 4 バイト要素の配列の index 番目）。

| 引数 | 型 | 必須 |
|---|---|---|
| `name` | `string` | ○ |
| `value` | `number` | ○ |
| `index` | `number | null` |  |

## debug.backtrace

種類: `control`。呼び出し履歴（ELF に CFI があれば CFI で巻き戻し、例外のフレームも越える）。各段は index・pc・where・note（exception = 例外で中断された位置）・cfa・function・offset・file・line。variables なら各段の引数とローカル変数（arguments・locals、型付きの値）も。停止中に呼ぶ。

| 引数 | 型 | 必須 |
|---|---|---|
| `variables` | `boolean` |  |
| `max_frames` | `number` |  |
| `max_elements` | `number` |  |

## debug.evaluate

種類: `control`。C の式（変数・メンバー・添字・ポインタ・& と *・+ と -）を DWARF の型で評価して読む。frame は `debug_backtrace` の index（0 = いまの関数。その関数の引数・ローカル変数も使える）。live = true なら走らせたまま読む（ライブ ウォッチ。コアを止めず、レジスタを使わずに大域・静的の変数とメモリを読む。ローカル変数は誤り）。結果は expression・type・address（メモリ上なら）・value（構造体・配列は入れ子。配列は max_elements 個まで）。

| 引数 | 型 | 必須 |
|---|---|---|
| `expression` | `string` | ○ |
| `frame` | `number` |  |
| `max_elements` | `number` |  |
| `live` | `boolean` |  |

## debug.expression.check

種類: `read`。C の式を ELF だけで調べる（ターゲットは使わない。止まっていても・走っていても・つないでいなくても同じ）。式の誤り・名前の無い変数・型の誤りは誤り（名前が無いときは、ローカル変数なら止まっているときに読む旨を添える）。通れば expression・type（ポインタをたどる式は読むまで分からないので null）・address（番地の決まる式）・live（走らせたまま読める）・needs_memory（番地がメモリの値で決まる）。

| 引数 | 型 | 必須 |
|---|---|---|
| `expression` | `string` | ○ |

## debug.frame_variables

種類: `control`。フレームの引数とローカル変数（型付きの値）。frame は `debug_backtrace` の index。

| 引数 | 型 | 必須 |
|---|---|---|
| `frame` | `number` |  |
| `max_elements` | `number` |  |

## debug.write_expression

種類: `control`。C の式の指す値（メモリ上の基本型・列挙（名前でも）・ポインタ・ビットフィールド）に書き、読み直した値を返す。整数は型の範囲を確かめる。

| 引数 | 型 | 必須 |
|---|---|---|
| `expression` | `string` | ○ |
| `value` | `number | number | string` | ○ |
| `frame` | `number` |  |

## debug.type_info

種類: `read`。型の名前（struct Sensor・typedef 名等）の大きさ・種類・メンバー（オフセット・ビットフィールド）・列挙子。ELF だけを使う（ターゲット不要）。

| 引数 | 型 | 必須 |
|---|---|---|
| `name` | `string` | ○ |

## debug.disasm.range

種類: `control`。番地から length バイト（4096 まで）を逆アセンブルする（Thumb / Thumb-2。関数名・ファイル:行・分岐先とリテラルの名前付き）。接続中はターゲットのメモリ、未接続か from_elf なら ELF。命令は offset 番目から limit 件（total が全体）。

| 引数 | 型 | 必須 |
|---|---|---|
| `address` | `number` | ○ |
| `length` | `number` | ○ |
| `from_elf` | `boolean` |  |
| `offset` | `number` |  |
| `limit` | `number` |  |

## debug.disasm.function

種類: `control`。関数の全体（関数名か 0x... の関数の中の番地）を逆アセンブルする。

| 引数 | 型 | 必須 |
|---|---|---|
| `location` | `string` | ○ |
| `from_elf` | `boolean` |  |
| `offset` | `number` |  |
| `limit` | `number` |  |

## debug.disasm.around

種類: `control`。番地（省略時は PC）の前 before 命令・その命令・後ろ after 命令（前は関数の先頭から揃える）。pc_index がその命令の位置。

| 引数 | 型 | 必須 |
|---|---|---|
| `address` | `number | null` |  |
| `before` | `number` |  |
| `after` | `number` |  |
| `from_elf` | `boolean` |  |

## debug.disasm.text

種類: `control`。ソースの行を挟んだ逆アセンブルの文字列（AI 向け。PC の命令には => の印）。location なら関数、address と length なら範囲、どちらも無ければ address（省略時は PC）の前後。

| 引数 | 型 | 必須 |
|---|---|---|
| `location` | `string | null` |  |
| `address` | `number | null` |  |
| `length` | `number | null` |  |
| `before` | `number` |  |
| `after` | `number` |  |
| `source` | `boolean` |  |
| `from_elf` | `boolean` |  |

## debug.semihosting.configure

種類: `control`。セミホスティング（BKPT 0xAB）の処理の設定。既定で有効: 止まった理由が BKPT 0xAB なら処理して実行を続け（`debug_run_until_halt` の中と `target_resume` の後）、出力は `rtt_read` の channel semihosting、SYS_EXIT は reason exit と exit_code。ファイル操作は file_root を指定したときだけ、その下に限る（file_root・writable は MCP の道具には出さない）。

| 引数 | 型 | 必須 |
|---|---|---|
| `enabled` | `boolean` |  |
| `file_root` | `string | null`（MCP の道具には出さない） |  |
| `writable` | `boolean`（MCP の道具には出さない） |  |
| `cmdline` | `string` |  |

## debug.semihosting.status

種類: `read`。セミホスティングの設定・処理した要求の数・最後の終了（exit: code・reason）・背景の見張りの有無。

## la.capture

種類: `control`。捕捉を仕掛けて完了まで待つ（`la_arm` + `la_collect`）。トリガ = (サンプル & mask) == (value & mask)
かつ rise / fall のビットのチャンネルのどれかがその向きに変化。
filter_channels（外の信号の ch = 拡張ヘッダのピンと専用の入力のビット。並びは probe_info の capabilities.la_layout）は filter_ns より短い変化を捨てる（グリッチフィルタ。フィルタした ch は結果の filter.delay_ns だけ遅れて記録される）。pulse はパルス幅のトリガ {"channel", "level"（既定 1 = High の脈）, "min_ns", "max_ns"（省略で上限なし）, "timeout"（true なら level が min_ns を超えて続いた時点で成立）}。パルス幅はパターンの条件と両方満たしたときに成立。ケイパビリティ la_filter / la_pulse が要る。

| 引数 | 型 | 必須 |
|---|---|---|
| `rate_hz` | `number | null` |  |
| `pretrigger` | `number` |  |
| `mask` | `number` |  |
| `value` | `number` |  |
| `rise` | `number` |  |
| `fall` | `number` |  |
| `timeout_s` | `number` |  |
| `vcd_path` | `string | null` |  |
| `filter_channels` | `number` |  |
| `filter_ns` | `number` |  |
| `pulse` | `Record<string, unknown> | null` |  |

## la.arm

種類: `control`。捕捉を仕掛ける（すぐ戻る）。条件が何もなければ即トリガ。
filter_channels（外の信号の ch = 拡張ヘッダのピンと専用の入力のビット。並びは probe_info の capabilities.la_layout）は filter_ns より短い変化を捨てる（グリッチフィルタ。フィルタした ch は結果の filter.delay_ns だけ遅れて記録される）。pulse はパルス幅のトリガ {"channel", "level"（既定 1 = High の脈）, "min_ns", "max_ns"（省略で上限なし）, "timeout"（true なら level が min_ns を超えて続いた時点で成立）}。パルス幅はパターンの条件と両方満たしたときに成立。ケイパビリティ la_filter / la_pulse が要る。

| 引数 | 型 | 必須 |
|---|---|---|
| `rate_hz` | `number | null` |  |
| `pretrigger` | `number` |  |
| `mask` | `number` |  |
| `value` | `number` |  |
| `rise` | `number` |  |
| `fall` | `number` |  |
| `filter_channels` | `number` |  |
| `filter_ns` | `number` |  |
| `pulse` | `Record<string, unknown> | null` |  |

## la.collect

種類: `control`。完了を待って読み出し、チャンネルごとの要約（トリガ = 0 ns）を返す。vcd_path があれば VCD も書く。

| 引数 | 型 | 必須 |
|---|---|---|
| `timeout_s` | `number` |  |
| `vcd_path` | `string | null` |  |

## la.deep.capture

種類: `control`。深いバッファ（外部メモリ 128 MB）で捕捉して要約を返す。トリガ条件（グリッチフィルタ・パルス幅を含む）は `la_capture` と同じ。
rle = True は変化点圧縮（遅い信号なら数秒〜数十秒分）、False は RAW（100 MS/s で最大 0.67 秒）。
読むのはトリガの前 before_beats 拍・後 after_beats 拍（1 拍 = RAW 8 サンプル / RLE 4 変化点）。
ターゲット操作と同時に使うときは `la_deep_arm` → 操作 → `la_deep_collect`。

| 引数 | 型 | 必須 |
|---|---|---|
| `rate_hz` | `number | null` |  |
| `mask` | `number` |  |
| `value` | `number` |  |
| `rise` | `number` |  |
| `fall` | `number` |  |
| `rle` | `boolean` |  |
| `before_beats` | `number` |  |
| `after_beats` | `number` |  |
| `timeout_s` | `number` |  |
| `vcd_path` | `string | null` |  |
| `filter_channels` | `number` |  |
| `filter_ns` | `number` |  |
| `pulse` | `Record<string, unknown> | null` |  |

## la.deep.arm

種類: `control`。深いバッファの捕捉を仕掛ける（すぐ戻る）。filter_channels・filter_ns・pulse は `la_arm` と同じ。

| 引数 | 型 | 必須 |
|---|---|---|
| `rate_hz` | `number | null` |  |
| `mask` | `number` |  |
| `value` | `number` |  |
| `rise` | `number` |  |
| `fall` | `number` |  |
| `rle` | `boolean` |  |
| `after_beats` | `number` |  |
| `filter_channels` | `number` |  |
| `filter_ns` | `number` |  |
| `pulse` | `Record<string, unknown> | null` |  |

## la.deep.collect

種類: `control`。深いバッファの完了を待って、トリガ前 before_beats 拍から後ろを読み、要約を返す。

| 引数 | 型 | 必須 |
|---|---|---|
| `timeout_s` | `number` |  |
| `before_beats` | `number` |  |
| `vcd_path` | `string | null` |  |

## la.stream

種類: `control`。ロジアナを止めずに duration_s 秒流し続け、VCD に書く（トリガなし。記録長は保存先で決まる）。
RAW は 12.5 MS/s（25 MB/s）程度まで、rle = True（変化点圧縮）は遅い信号ならそれ以上。
overrun = True はホストが遅れて途中で止まったこと（それまでの分は正しい）。

| 引数 | 型 | 必須 |
|---|---|---|
| `vcd_path` | `string` | ○ |
| `duration_s` | `number` |  |
| `rate_hz` | `number | null` |  |
| `rle` | `boolean` |  |

## la.view

種類: `read`。最後の捕捉（誰が取ったものでも）を width 画素に縮めた波形（画面の表示用）。levels[ch][i] は画素 i の区間が
'0'（ずっと 0）/ '1'（ずっと 1）/ 'x'（区間内で変化）。start・end はサンプル番号（end 省略で最後まで）。

| 引数 | 型 | 必須 |
|---|---|---|
| `start` | `number` |  |
| `end` | `number | null` |  |
| `width` | `number` |  |

## timeline.tiles

種類: `read`。応答はバイナリのフレーム（WebSocket の利用者だけ。MCP の道具には無い）。共通タイムラインのタイル（画面の時間軸が毎フレームの間引きに使う材料。応答は WebSocket のバイナリのフレーム）。source = capture（最後の捕捉、時刻はサンプル番号）/ recording（読む記録、時刻はタイムベース）の範囲 [t0, t1) の、波形の生の変化点（かたまりごとに頭の値と変化点）と出来事の段（stream ごとに t・t_end・種類と、段の規則（fprobe.timeline_rows）で分けた段・ラベル・値・入れ子の深さ・色の意味・飛び先の番地。状態・値の段は t0 の前の最後の出来事も）。生データが max_edges（全チャンネルの和）・max_events（stream ごと）を超える部分は、幅 bin 程度（そろえた区間）の要約（波形の状態、段 × 区間の件数、値の段の最小・最大）。AI は la.view / recording.waveform / recording.density を使う。

| 引数 | 型 | 必須 |
|---|---|---|
| `source` | `"capture" | "recording"` |  |
| `t0` | `number` |  |
| `t1` | `number | null` |  |
| `channels` | `Array<number> | null` |  |
| `streams` | `Array<string> | null` |  |
| `bin` | `number | null` |  |
| `max_edges` | `number` |  |
| `max_events` | `number` |  |

## la.decode

種類: `read`。最後の捕捉の通信を解読する。protocol は解読器の名前（la.decoders で一覧。組み込み: uart / spi / i2c / swd / can（CAN FD 含む）/ onewire / jtag / i2s / parallel / manchester と、積み重ねる jtag_arm（jtag の上）/ spiflash（spi）/ eeprom24（i2c）/ modbus（uart）、la.decoder_load で読み込んだ利用者の解読器）。settings はその設定（uart: channel・baud（省略で推定）・data_bits・parity・stop_bits・invert、spi: sck・mosi・miso・cs・mode・word_bits、i2c: scl・sda、swd: swclk・swdio・drive、can: channel・bitrate・data_bitrate・sample_point、onewire: channel、jtag: tck・tms・tdi・tdo・trst・initial_state・ir_names、i2s: sck・ws・sd・format・word_bits、parallel: data（列）・clock・edge・enable、manchester: channel・bitrate・convention）。積み重ねる解読器は下の解読の設定を下の名前のキーに入れる（例 modbus: {"uart": {"channel": 5, "baud": 9600, "parity": "even"}}、何段でも）。start / end（サンプル）・kinds・errors_only で絞り、offset から limit 件。counts は種類ごとの件数、errors は誤りの数。kinds を省くと解読器の既定の種類（swd は transfer、jtag は ir・dr・reset 等）。記録中なら注釈を共通の時間軸にも入れる。結果の kinds は絞った種類（null = 全部）、total・counts は絞る前の件数。

| 引数 | 型 | 必須 |
|---|---|---|
| `protocol` | `string` | ○ |
| `settings` | `Record<string, unknown>` |  |
| `capture_id` | `number | null` |  |
| `start` | `number | null` |  |
| `end` | `number | null` |  |
| `kinds` | `Array<string> | null` |  |
| `errors_only` | `boolean` |  |
| `offset` | `number` |  |
| `limit` | `number` |  |

## la.decoders

種類: `read`。la.decode に使える解読器の一覧。各要素: name・lower（積み重ねる解読器が受け付ける下の解読。空なら波形を読む）・summary・params（name・required・default）・default_kinds・origin（builtin / 利用者のファイルの絶対パス）。

## la.decoder_load

種類: `control`。利用者の解読器のファイル（Python）を読み込んで登録する。ファイルの中で `from fprobe.decode import Annotation, decoder` とし、`@decoder("名前", lower=("uart",))` を付けた関数 f(lower: Decoding, **設定) -> Decoding を書く（結果は lower.derive(名前, 注釈の列, 設定) で作る。lower を省くと f(tr: Transitions, **設定) で波形を読む）。同じファイルを読み直すと前の登録を置き換える。組み込みの名前は使えない。

| 引数 | 型 | 必須 |
|---|---|---|
| `path` | `string` | ○ |

## la.export

種類: `read`。最後の捕捉をファイルに書く。format: sr（sigrok のセッション。PulseView・sigrok-cli で開き、sigrok の約 100 種のデコーダを使える）/ vcd。channels は書くチャンネル（sr のみ、既定は全部）、names はチャンネルの名前（{"5": "rx"}）。

| 引数 | 型 | 必須 |
|---|---|---|
| `path` | `string` | ○ |
| `format` | `"sr" | "vcd"` |  |
| `channels` | `Array<number> | null` |  |
| `names` | `Record<string, unknown> | null` |  |

## la.load

種類: `control`。sigrok の .sr（他のロジアナ・PulseView で保存したもの）を最後の捕捉として読み込む。以後 la.decode・la.measure・la.view がそれを読む。トリガは先頭のサンプル。

| 引数 | 型 | 必須 |
|---|---|---|
| `path` | `string` | ○ |

## la.measure

種類: `read`。最後の捕捉の範囲 [start, end)（サンプル番号）を測る。チャンネルごと（channels 省略で変化のあるもの）: rises・falls・high_ratio（high の時間の割合）・frequency_hz・duty（平均・最小・最大）と、period・high・low の幅の統計（件数・min/max/mean/stdev_ns・min_at/max_at = サンプル番号）。範囲の端で切れたパルスは数えない。relations でチャンネル間: {"type": "delay", "from", "from_edge", "to", "to_edge"}（rise / fall / any。from のエッジから次の to のエッジまで）、{"type": "setup_hold", "clock", "clock_edge", "data"（番号か列）, "setup_min_ns", "hold_min_ns"}（違反の一覧）。分解能は resolution_ns（1 サンプル）。

| 引数 | 型 | 必須 |
|---|---|---|
| `start` | `number` |  |
| `end` | `number | null` |  |
| `channels` | `Array<number> | null` |  |
| `relations` | `Array<Record<string, unknown>> | null` |  |
| `capture_id` | `number | null` |  |

## events.start

種類: `control`。DAP 転送のイベント記録を始める（外部メモリのリング。古いものから上書き）。

## events.stop

種類: `control`。イベント記録を止める（記録は残り、`events_read` で読める）。

## events.read

種類: `control`。イベントを古い順に返す。since が無ければ最新の last 件。since（前回の結果の next）があれば、その番号から last 件まで（続けて読めば取りこぼさない。読む前にリングが上書きした数は skipped）。first = 返した最初の番号、next = 次に since に渡す番号（記録を始めてから書いた数で数える）。raw = False ならアクセス（ポストされた読みを対応付けた後）、True なら線上の転送そのもの。time_us は記録開始とは無関係なタイムベースの時刻（µs）。

| 引数 | 型 | 必須 |
|---|---|---|
| `last` | `number` |  |
| `raw` | `boolean` |  |
| `since` | `number | null` |  |

## watch.set

種類: `control`。変数を止めずに周期的に読む。expressions: 変数名・C の式・番地（"0x20000000" か "0x20000000:バイト数"）。決まった番地の式は FPGA の DAP サンプラが rate_hz で読む（pyOCD 等のデバッグと同時に使える。合わせて 8 か所・各 16 語まで。値はイベント記録と、記録中なら stream "sample" に入る）。ポインタをたどる式（"head->pos.x"・"*p"）はホストが chase_rate_hz ごとに番地を引き直して読む（method = host）。誤った式（名前が無い・メモリ上に無い等）は rejected（[{expression, error}]）に外して残りを取る（全部が誤りなら始めずに誤り）。jitter（0〜15）: サンプラの周期のゆらぎ（周期的な処理との噛み合いを防ぐ）。

| 引数 | 型 | 必須 |
|---|---|---|
| `expressions` | `Array<string>` | ○ |
| `rate_hz` | `number` |  |
| `ap` | `number` |  |
| `chase_rate_hz` | `number` |  |
| `jitter` | `number` |  |

## watch.stop

種類: `control`。周期的な読みを止める（読んだ値はイベント記録・記録に残る）。

## watch.status

種類: `control`。観測している変数・周期と、サンプラの状態（巡回の数・誤り・飛ばした周期 = watch.set からの数。FPGA の起動からの数は device。最後の ACK）、watch.set で外した誤った式（rejected）、始めてからの秒（since_start_s）。ポインタをたどる式の読みの誤り（chase_errors）と条件つきの取得の状態（capture）も。

## watch.values

種類: `control`。観測している変数の最新の値（型で解釈）と、その時刻（タイムベースの µs）。

## watch.series

種類: `control`。変数 1 つの値の列（古い順。時刻はタイムベースの µs）。last はイベント記録から読む件数（最大 4096）。長い時間は recording_find({"stream": "sample", "a": 番地}) で。

| 引数 | 型 | 必須 |
|---|---|---|
| `name` | `string` | ○ |
| `last` | `number` |  |

## rtos.info

種類: `control`。ELF の RTOS の種類（freertos / zephyr / rtx5 / threadx / ucos3 / nuttx / null）と、接続していればスケジューラの状態・ティック・実行中のタスク（止まっていればタスクの数）。trace_hook はトレースフック（方法 itm の時間軸）の有無、timeline は取っている時間軸。

## rtos.tasks

種類: `control`。タスクの一覧（名前・優先度・状態・待っているもの（キュー名・種類・ミューテックスの持ち主・残りのティック）・スタックの大きさ・いま／最大の使用量・あふれの印）。止まっているときだけ（`target_halt`）。live=true なら走行中にも読む（止めずに読むので一貫しない: consistent=false。実行中のタスクの SP は分からない）。構造体の位置は DWARF から。

| 引数 | 型 | 必須 |
|---|---|---|
| `live` | `boolean` |  |

## rtos.objects

種類: `control`。カーネルのオブジェクトの一覧: FreeRTOS はキュー・セマフォ（数・最大）・ミューテックス（持ち主・継承中か）・タイマ（周期・動いているか・残りのティック・呼ぶ関数）・イベントグループ（ビット・待つビット）・ストリーム / メッセージバッファ（使用量・メッセージの数）、Zephyr は k_sem・k_mutex・k_msgq・k_event・k_timer・k_fifo 等、RTX5 は mutex・semaphore・event_flags・memory_pool・message_queue・timer、ThreadX は mutex・semaphore・queue・event_flags・block_pool・byte_pool・timer、µC/OS-III は semaphore・mutex・queue・event_flags・memory_pool・timer・cond、NuttX は semaphore・mutex・message_queue。それぞれ待っているタスク付き。登録簿に無い・名前の無いものも、変数の型（ハンドル）・待っているタスクから番地で出す（source: registry / kernel / variable / waiter）。ヒープの残量・最小残量（heap）と、止めた瞬間の優先度の逆転（継承の有無・間に動けるタスク）とデッドロック（contention）。kinds で種類を絞る（"semaphore" は binary・counting とも）。止まっているときだけ（live=true で走行中にも。一貫しない: consistent=false）。

| 引数 | 型 | 必須 |
|---|---|---|
| `kinds` | `Array<string> | null` |  |
| `live` | `boolean` |  |

## rtos.backtrace

種類: `control`。タスク（名前か TCB の番地。"0x..." も可）の呼び出し履歴。止まっているタスクは保存された文脈から、実行中のタスクは実レジスタから巻き戻す。variables で各段の引数とローカル変数も。止まっているときだけ。

| 引数 | 型 | 必須 |
|---|---|---|
| `task` | `string | number` | ○ |
| `variables` | `boolean` |  |
| `max_elements` | `number` |  |

## rtos.read

種類: `control`。タスクの文脈で C の式を読む（frame は `rtos_backtrace` の index。そのフレームの引数・ローカル変数も使える）。止まっているときだけ。

| 引数 | 型 | 必須 |
|---|---|---|
| `task` | `string | number` | ○ |
| `expression` | `string` | ○ |
| `frame` | `number` |  |
| `max_elements` | `number` |  |

## rtos.timeline.start

種類: `control`。タスクの時間軸を取り始める（止めずに見る）。method: sampled（組み込み無し。DAP サンプラが rate_hz で実行中のタスクの変数を読む。周期より短い実行は見えないことがあり、回数は下限。`watch_set` とサンプラを取り合う）/ itm（ファームのトレースフック fprobe_trace.h が ITM のポート 31 に書く語から。正確）。itm は swo_baud で SWO の受信も始め、core_clock_hz も渡すと TPIU の速度も設定する（SWO のピンはチップ固有: ファームか `target_peripheral_write`）。一緒に読むもの: memory=true（タスクの保存された SP とヒープの残量 → `rtos_timeline_memory`。tasks で絞る）、mutexes（true か名前の列: 持ち主と待ちの先頭 → `rtos_timeline_inversions`）、exceptions=true（itm だけ: 例外トレースも SWO に流す → 割り込みとタスクの合成 `rtos_timeline_profile`）。memory・mutexes は DAP サンプラの 8 か所に収める（sampled は同じサンプラ、itm は memory_rate_hz で）。記録中（`recording_start`）なら止めたときに記録の stream "rtos" に書く。 kernel=false（itm だけ）でカーネルの出来事（ポート 30・29）を止め、SWO の帯域をタスクの切り替えと例外に回す（overflows が出るとき）。

| 引数 | 型 | 必須 |
|---|---|---|
| `method` | `"sampled" | "itm"` |  |
| `rate_hz` | `number` |  |
| `ap` | `number` |  |
| `swo_baud` | `number | null` |  |
| `core_clock_hz` | `number | null` |  |
| `memory` | `boolean` |  |
| `tasks` | `Array<string> | null` |  |
| `mutexes` | `boolean | Array<string> | null` |  |
| `exceptions` | `boolean` |  |
| `memory_rate_hz` | `number` |  |
| `kernel` | `boolean` |  |

## rtos.timeline.stop

種類: `control`。時間軸を取るのを止め、要約（タスクごとの合計・CPU の使用率・実行の回数・最長／最短・平均、切り替えの回数、分解能、回数が下限か、例外トレースの回数）を返す。記録中なら記録の stream "rtos" に run・switch・gap（itm は task_in 等の生の出来事も、exceptions なら割り込みの区間 isr も）を書く。止めた後も解析の道具（`rtos_timeline_load` 等）は最後の結果を使う。

## rtos.timeline.get

種類: `control`。時間軸（取っている最中ならいままでの分、止めた後なら最後の結果）: 要約と、区間（タスク・始め・終わり・不確かさ。時刻はタイムベース）の offset から limit 件と抜け（gap）。長い時間の区間は記録の `recording_find` で。

| 引数 | 型 | 必須 |
|---|---|---|
| `offset` | `number` |  |
| `limit` | `number` |  |

## rtos.timeline.load

種類: `control`。窓（window_us）ごとの CPU 負荷（1 − アイドルの割合。割り込みは負荷に入る）・タスクごとの割合・割り込みの割合の推移と、最大の窓（いつ）・平均。情報の無い時間（抜け）は除く。取っている最中ならいままでの分、止めた後なら最後の結果。

| 引数 | 型 | 必須 |
|---|---|---|
| `window_us` | `number` |  |
| `limit` | `number` |  |

## rtos.timeline.jobs

種類: `control`。タスクのジョブ（起きてから待つまでの 1 回の仕事）ごとの実行時間（横取り・割り込みを除く）・経過時間・周期の分布（最小・最大・平均・σ・p90・p99）と最悪の回（いつ）。方法 itm でカーネルの出来事があれば ready / block で区切り、応答時間（ready から待つまで）も出す。無ければ区間の終わりは次に動いたタスクの優先度で決める（高い方へ = 横取り、低い方へ = 待った、同じ = uncertain、抜け・端 = truncated で統計に入れない）。task を渡すとそのタスクのジョブの一覧（実行時間の長い順に limit 件）も。

| 引数 | 型 | 必須 |
|---|---|---|
| `task` | `string | null` |  |
| `limit` | `number` |  |

## rtos.timeline.interval

種類: `control`。記録の 2 点（start / end は `recording_find` と同じ条件。`timing_measure` と同じ組の規則）の各組で、タスク（省けば開始の時刻に動いていたタスク）が実際に CPU を使った時間（横取り・割り込みを除く）と経過時間の分布。記録（`recording_start` か `recording_open`）が要る。

| 引数 | 型 | 必須 |
|---|---|---|
| `start` | `Record<string, unknown>` | ○ |
| `end` | `Record<string, unknown>` | ○ |
| `task` | `string | null` |  |
| `limit` | `number` |  |

## rtos.timeline.memory

種類: `control`。タスクごとのスタックの使用量（保存された SP から: 切り替えのたびの深さ。最大・最小・最後・あふれの印）とヒープの残量（最小・最後）の推移。points 個の窓ごとに最悪の値。`rtos_timeline_start` の memory=true が要る。

| 引数 | 型 | 必須 |
|---|---|---|
| `points` | `number` |  |

## rtos.timeline.profile

種類: `control`。実行のプロファイル: 情報のある時間に対するスレッド（タスクごと。割り込みの時間を除く）・割り込み（例外ごとの回数・自分の時間）・アイドル・どれでもない時間の割合。割り込みは方法 itm の exceptions=true のとき（それ以外は interrupts = null で、割り込みはタスクの時間に含まれる）。

## rtos.timeline.inversions

種類: `control`。優先度の逆転の区間: 持ち主の元の優先度より高いタスクがミューテックスを待っていた区間ごとに、持ち主・待ち手・長さ・持ち主が動いた時間・その間に動いた中間の優先度のタスクの時間（interference。正なら際限の無い逆転 = unbounded）。方法 itm でカーネルの出来事があれば、それで正確に（source = trace。継承の時刻 inherited も）。無ければ `rtos_timeline_start` の mutexes で読んだ語から（source = sampled。サンプラの周期より短い逆転は見えないことがある。読んでいなければ available=false。推測しない）。

| 引数 | 型 | 必須 |
|---|---|---|
| `limit` | `number` |  |

## rtos.timeline.events

種類: `control`。カーネルの出来事の一覧（方法 itm でファームの fprobe_trace.h のフックがあるとき）: キュー・セマフォ・ミューテックスの送り・受け（give・take）と失敗・待ち（block、wait = receive/send/peek/notify）・準備完了（ready）・delay・suspend・優先度の継承と変更（prio_*）・通知・イベントグループ・ストリームバッファ・割り込みの出入り。actor は行為者のタスク（割り込みの中なら isr）、object はオブジェクトの名前、arg は操作の前のメッセージ数等。kinds・task（行為者か対象のタスク）・object で絞り、offset から limit 件。

| 引数 | 型 | 必須 |
|---|---|---|
| `kinds` | `Array<string> | null` |  |
| `task` | `string | null` |  |
| `object` | `string | null` |  |
| `offset` | `number` |  |
| `limit` | `number` |  |

## rtos.timeline.flow

種類: `control`。Communication Flow: タスク・割り込み・オブジェクトの有向グラフ（カーネルの出来事から）。辺は send・give（行為者 → オブジェクト）、receive・take・peek（オブジェクト → 行為者）、notify（行為者 → タスク）、set（イベントグループ）、wait（待った）と回数（count）・失敗の回数（failed）・ストリームのバイト数。dot は Graphviz の形。

## rtos.timeline.blocking

種類: `control`。ブロック時間（カーネルの出来事から）: 待ちの区間（block = オブジェクト・通知を待った / delay / suspend から ready まで）と outcome（起きた後の同じオブジェクトへの操作が成功 = ok、失敗 = timeout）。タスクごと・オブジェクトごとの回数・合計・平均・最長（いつ）・時間切れの回数。task で区間の一覧を絞る。

| 引数 | 型 | 必須 |
|---|---|---|
| `task` | `string | null` |  |
| `limit` | `number` |  |

## rtos.timeline.queues

種類: `control`。キュー使用率（カーネルの出来事から）: キュー・セマフォごとのメッセージ数（数）の推移（points 点まで、山を落とさない）・最大・時間で重みを付けた平均・長さに対する割合（max_usage・mean_usage）、満杯（send_failed）・空（receive_failed）・待ち（waits_send・waits_receive）の回数。長さは始めに読んだオブジェクトの一覧から。

| 引数 | 型 | 必須 |
|---|---|---|
| `points` | `number` |  |

## rtos.timeline.overhead

種類: `control`。トレースフックの負荷: 語の数（ポート 31・見出し 30・中身 29）・出来事の種類ごとの数・SWO のバイト数と帯域の使用率（swo_share。1 に近いと FIFO が待ち・あふれる）・CPU の負荷の見積もり（cpu_share。命令数と、記録の中身の語が FIFO の空きを待つ時間から。core_clock_hz が要る）。

## profile.start

種類: `control`。PC サンプリングのプロファイルを始める。method: itm（DWT の PC サンプリング → SWO。core_clock_hz が要る。exception_trace で割り込みの時間も。DWT・ITM・TPIU を設定して SWO の受信も始める。SWO のピンはチップ固有: ファームか `target_peripheral_write`）/ pcsr（DAP サンプラで DWT_PCSR を読む。SWO 不要・Cortex-M0+ も可・遅い。`watch_set` とサンプラを取り合う）。timestamps は省略で exception_trace と同じ。計画（実際のサンプル率・SWO の使用率・警告・書いたレジスタ）を返す。pcsr の速さは SWD の速さで決まる（4 MHz で 1 巡回 約 110〜120 µs = 約 8〜9 kHz）: 計画の swd に 1 巡回の見積もり（swd_hz・swclk_hz・round_us・max_rate_hz）と、求めた rate_hz に要る SWD の速さ（swd_hz_for_rate。target_connect の frequency_hz に渡す。null は最も速い SWD でも届かない）。rate_hz は出せる見込み、requested_rate_hz は求めた値。

| 引数 | 型 | 必須 |
|---|---|---|
| `method` | `"itm" | "pcsr"` |  |
| `rate_hz` | `number` |  |
| `core_clock_hz` | `number | null` |  |
| `swo_baud` | `number` |  |
| `exception_trace` | `boolean` |  |
| `exception_rate_hz` | `number` |  |
| `timestamps` | `boolean | null` |  |
| `ap` | `number` |  |

## profile.stop

種類: `control`。PC サンプリングを止めてプロファイルにする（DWT_CTRL を元に戻す）。要約（分類: code / sleep / unknown、上位 top の関数の割合と信頼区間、割り込みごとの回数）を返す。詳しくは `profile_result` / `profile_coverage` / `profile_exceptions`。pcsr なら sampling（requested_hz・expected_hz・achieved_hz = 読みの時刻からの実際の速さ・SWD の速さ・swd_hz_for_rate）が付き、追いつかなければ notes に要る SWD の速さ。

| 引数 | 型 | 必須 |
|---|---|---|
| `top` | `number` |  |

## profile.result

種類: `control`。プロファイルの上位 top（取っている最中ならいままでの分）。by: function / line / instruction / exception。line・instruction は function で絞れる。割合（自分の時間）には信頼区間 ci が付く（サンプル数による誤差だけ。周期と同期した処理の偏りは含まない）。

| 引数 | 型 | 必須 |
|---|---|---|
| `by` | `"function" | "line" | "instruction" | "exception"` |  |
| `top` | `number` |  |
| `function` | `string | null` |  |

## profile.coverage

種類: `control`。PC サンプルが当たった命令・行からの、関数ごとの実行された行の割合と当たらなかった行（近似: 当たらなかった行が実行されなかったとは限らない）。functions を省くと当たった関数（all_functions なら ELF の全関数）。

| 引数 | 型 | 必須 |
|---|---|---|
| `functions` | `Array<string> | null` |  |
| `all_functions` | `boolean` |  |

## profile.exceptions

種類: `control`。割り込みごとの回数・時間（含む／自分。最小・最大・平均・σ）・最大の回の時刻・負荷（ITM のローカルの時刻 = コアのクロック数。clock_hz で秒にも）。method itm と exception_trace が要る。

## cycles.read

種類: `control`。DWT の CYCCNT（32 ビット）と 8 ビットのイベントカウンタ（cpi・exc・sleep・lsu・fold）のいまの値と、読んだときのタイムベースの時刻。何も変えない。

## cycles.measure

種類: `control`。start から end（関数名・"ファイル:行"・"0x..."）までのサイクルを count 回測る（ブレークポイントを置いて走らせ、止まるたびに CYCCNT を読む。止まっている間は数えない。置いたものは外す）。counters ならCPI・EXC・SLEEP・LSU・FOLD の増分と命令数・IPC も（`swo.configure` の counters で溢れを数えていれば正確、そうでなければ 256 未満とみなす）。core_clock_hz で µs も。終わるとターゲットは end で止まっている。

| 引数 | 型 | 必須 |
|---|---|---|
| `start` | `string` | ○ |
| `end` | `string` | ○ |
| `count` | `number` |  |
| `timeout_s` | `number` |  |
| `core_clock_hz` | `number | null` |  |
| `counters` | `boolean` |  |

## cycles.clock

種類: `control`。CPU クロックを実測する（コアが走っている間に CYCCNT の進みをデバイスのタイムベースと比べる。DAP サンプラが使えれば rate_hz で duration_s の間読んだ直線の傾き、ほかの道具が使っていれば前後 2 回の読み）。CYCCNT を有効にする。clock_hz・標準誤差（ppm）・止まった区間の警告を返す。

| 引数 | 型 | 必須 |
|---|---|---|
| `duration_s` | `number` |  |
| `rate_hz` | `number` |  |
| `ap` | `number` |  |

## timing.measure

種類: `read`。記録（記録中のもの、または開いたもの）の 2 点間の時間の統計（最小・最大・平均・σ・中央値・p90・p99）・分布・外れ値（iqr: 四分位範囲の柵 / zscore）。start / end は `recording_find` と同じ条件。外れ値と min_at・max_at の時刻は `recording_events` で開ける。生の組と外れ値は limit 件まで。

| 引数 | 型 | 必須 |
|---|---|---|
| `start` | `Record<string, unknown>` | ○ |
| `end` | `Record<string, unknown>` | ○ |
| `bins` | `number` |  |
| `outliers` | `"iqr" | "zscore"` |  |
| `limit` | `number` |  |

## testpoint.instrument

種類: `read`。テストポイント（DT+ 方式）の挿入: C のソース（source_dir の下の *.c）に出力のコードを入れた写しを out_dir に作り、ID の対応表（map_path、既定 out_dir/testpoints.json）を書く。写しの行番号は元と同じ（#line）。対応表が既にあれば中身の変わらない関数の ID を保つ（差分の再挿入。renumber で振り直す）。only_changed は前回（baseline）から変わった・新しい関数にだけ入れる。variables は書き込みを記録する変数（"name" か "name:novalue"）、include / exclude は関数名の glob、branches=false で関数の入口・出口と変数だけ。写しは -DFPROBE_TP_BACKEND=（GPIO_SERIAL / GPIO_PARALLEL / ITM / RTT。fprobe_tp.h）でビルドする。機器に触らない。以後の `testpoint_start` の既定の対応表。

| 引数 | 型 | 必須 |
|---|---|---|
| `source_dir` | `string` | ○ |
| `out_dir` | `string` | ○ |
| `map_path` | `string | null` |  |
| `variables` | `Array<string> | null` |  |
| `include` | `Array<string> | null` |  |
| `exclude` | `Array<string> | null` |  |
| `branches` | `boolean` |  |
| `only_changed` | `boolean` |  |
| `baseline` | `string | null` |  |
| `renumber` | `boolean` |  |
| `line_root` | `string | null` |  |

## testpoint.start

種類: `control`。テストポイントの受け取りを始める（すぐ戻る）。method: gpio_serial（2 線。clock_channel・data_channel）/ gpio_parallel（data_channels = D0..D(W-1)、最後が先頭の印。strobe_channel）はロジアナで、capture deep（深いバッファ。trigger なら CLK / ストローブの最初の立ち上がりから after_beats 拍。止めたときに読む）か stream（流し続ける捕捉を背景で解読・解析する。長時間向け。メモリは一定）。itm は SWO（port のスティミュラス・ポート。swo_baud で受信を始め、core_clock_hz も渡すと TPIU も設定。受信を始めてからポートを有効にし、止めると元に戻す。時刻は FPGA が SWO のバイトに付けた時刻）。rtt は RTT の上りの rtt_channel（core_clock_hz = CYCCNT の速さが要る。時刻はホストの時計で写すので不確かさ付き）。map_path を省くと最後の `testpoint_instrument` の対応表。duration_s を渡すと `testpoint_stop` はそこまで待つ（stream は自分で終わる）。silence_s（何も来ない）・open_s（出口が無い）・long_*（長すぎる呼び出し = 平均 + long_sigma·σ かつ平均の long_ratio 倍）は異常の印。記録中（`recording_start`）なら stream "testpoint"（call / hit / anomaly）に書く。

| 引数 | 型 | 必須 |
|---|---|---|
| `method` | `"gpio_serial" | "gpio_parallel" | "itm" | "rtt"` | ○ |
| `map_path` | `string | null` |  |
| `clock_channel` | `number | null` |  |
| `data_channel` | `number | null` |  |
| `data_channels` | `Array<number> | null` |  |
| `strobe_channel` | `number | null` |  |
| `capture` | `"deep" | "stream"` |  |
| `rate_hz` | `number | null` |  |
| `rle` | `boolean` |  |
| `trigger` | `boolean` |  |
| `after_beats` | `number` |  |
| `port` | `number` |  |
| `swo_baud` | `number | null` |  |
| `core_clock_hz` | `number | null` |  |
| `rtt_channel` | `number` |  |
| `duration_s` | `number | null` |  |
| `bucket_s` | `number` |  |
| `silence_s` | `number | null` |  |
| `open_s` | `number | null` |  |
| `long_sigma` | `number` |  |
| `long_min_calls` | `number` |  |
| `long_ratio` | `number` |  |

## testpoint.stop

種類: `control`。テストポイントの受け取りを止めて解析を終える（duration_s を渡して始めたならそこまで待つ。deep は深いバッファが埋まるのを timeout_s まで待って読む。捕捉は `la_view` と記録の波形にも残る）。要約（上位 top の関数・カバレッジ・異常）を返す。詳しくは `testpoint_report` / `testpoint_calls` / `testpoint_coverage`。

| 引数 | 型 | 必須 |
|---|---|---|
| `timeout_s` | `number` |  |
| `top` | `number` |  |

## testpoint.status

種類: `control`。テストポイントの受け取りの様子（方式・設定・受けた印の数・取りこぼし・経過・流し続ける捕捉の速さと overrun・時刻の写し・警告）。

## testpoint.report

種類: `control`。テストポイントの解析の要約（受け取っている最中ならいままでの分）: 関数ごとの回数・子を含む／自分だけの時間（合計・最小・最大・平均・σ）を多い順に top、カバレッジの割合（関数・C0・C1）、異常（long_call / missing_exit / exit_without_entry / unknown_id / silence / no_exit / loss）の種類ごとの数と先頭 anomalies 件、時間の区切りごとの件数の最後の buckets 個、変数（最新・最小・最大）、開いたままの呼び出し、呼び出しの木（深さ tree_depth まで）。t_s は最初の印からの秒、t はタイムベース（`recording_events` 等の時刻）。

| 引数 | 型 | 必須 |
|---|---|---|
| `top` | `number` |  |
| `anomalies` | `number` |  |
| `buckets` | `number` |  |
| `tree_depth` | `number` |  |

## testpoint.calls

種類: `control`。テストポイントの関数の遷移の時間軸: 呼び出しを始まりの順に（関数・始まり t_s / t・時間・自分だけの時間・深さ・出口を見たか）と、深さで字下げした文字列 text。t0_s ≤ 始まり < t1_s（最初の印からの秒）・function で絞り、先頭 limit 件。持っているのは最近の 65536 件（dropped はそれより前に捨てた数。全部は記録の stream "testpoint" の call）。

| 引数 | 型 | 必須 |
|---|---|---|
| `t0_s` | `number | null` |  |
| `t1_s` | `number | null` |  |
| `function` | `string | null` |  |
| `limit` | `number` |  |

## testpoint.coverage

種類: `control`。テストポイントのカバレッジ（実行の記録に基づく正確なもの）: 関数（入った / 入れた）・C0（ソースにある塊の印）・C1（分岐の組の結果。足した else・default を含む）の割合と、通らなかった印（ファイル・行・関数・種類。試験を足す手がかり）、分岐の組ごとの結果の回数。file（対応表の相対パス）・function で絞り、先頭 limit 件。

| 引数 | 型 | 必須 |
|---|---|---|
| `file` | `string | null` |  |
| `function` | `string | null` |  |
| `limit` | `number` |  |

## xtrig.route

種類: `control`。源（"la_trigger" / "dap_match" / "host" / "sequence" = 多段トリガ）を動作につなぐ。to_la: ロジアナをトリガする源、
to_inject: 割り込み書き込み（既定はターゲット停止）をする源。指定しない動作は切る。

| 引数 | 型 | 必須 |
|---|---|---|
| `to_la` | `Array<string> | null` |  |
| `to_inject` | `Array<string> | null` |  |

## xtrig.set_inject

種類: `control`。割り込み書き込みの番地と値（既定は DHCSR ← C_HALT | C_DEBUGEN = ターゲット停止）。
ホストの DAP コマンドの境界で行い、ホストの SELECT・CSW・TAR は元に戻す。

| 引数 | 型 | 必須 |
|---|---|---|
| `address` | `number` |  |
| `value` | `number` |  |
| `ap` | `number` |  |

## xtrig.set_match

種類: `control`。番地一致（源 dap_match）の条件: デバッガ（ホスト）がこの番地にアクセスしたら成立。data_mask = 0 なら値を見ない。

| 引数 | 型 | 必須 |
|---|---|---|
| `address` | `number` | ○ |
| `address_mask` | `number` |  |
| `data` | `number` |  |
| `data_mask` | `number` |  |
| `on_write` | `boolean` |  |
| `on_read` | `boolean` |  |

## xtrig.set_sequence

種類: `control`。多段トリガ（源 "sequence"）。stages は 1〜4 個の {"when": ["pattern_a" / "pattern_b" / "dap_match" / "host"],
"count": 回数（既定 1）, "within_us": 段に入ってからの制限時間（省略で無制限、段 0 では無視）}。
pattern_a / pattern_b は {"mask", "value", "rise", "fall"}（ロジアナのチャンネルのビット）。
例: 「ch3 の立ち上がりの後 10 µs 以内に ch5 の立ち下がり」= stages=[{"when": ["pattern_a"]},
{"when": ["pattern_b"], "within_us": 10}], pattern_a={"rise": 8}, pattern_b={"fall": 32}。
`xtrig_route` の to_la / to_inject に "sequence" を入れてつなぐ。単発（再び使うときは arm）。

| 引数 | 型 | 必須 |
|---|---|---|
| `stages` | `Array<Record<string, unknown>>` | ○ |
| `pattern_a` | `Record<string, unknown> | null` |  |
| `pattern_b` | `Record<string, unknown> | null` |  |
| `arm` | `boolean` |  |

## xtrig.sequence_status

種類: `read`。多段トリガの状態（今の段、発火したか、発火した時刻 = タイムベースのカウント）。

## xtrig.fire

種類: `control`。源 host を 1 回発火する。

## xtrig.status

種類: `read`。割り込み書き込みの状態（結果・回数・時刻）と番地一致の回数・時刻（タイムベースのカウント）。

## xtrig.output_set

種類: `control`。クロストリガの動作「出力の端子を動かす」の設定（U41）。channel = 出力に使う端子のロジアナの ch（使える端子は xtrig.output_status。Primer: 4 予備・5 UART RX・6 SWO・7 UART TX）。条件 = pattern（端子の並び。ロジアナの ch 0〜7 のビット {"mask", "value", "any"}。any = true は見る ch のどれかが一致で成立。毎周期その場で照合）or sources（源 "la_trigger" / "dap_match" / "host" / "sequence"）。mode: "pulse"（条件の立ち上がりで pulse_us の脈。刻みに丸めた実際の範囲を返す）/ "hold"（立ち上がりから無効にするまで）/ "while"（条件の間だけ。遅れ 40 ns 程度）。level: 動いている間に出す値（high / low）。動いていない間は駆動しない（高インピーダンス。プルアップのある端子は弱く Highなので notes を返す）。設定するとその端子の普段の機能（UART・SWO）を止めて出力に使う（戻すのは xtrig.output_release）。同時に動かせる数は xtrig.output_status の actions。enable = true で続けて有効にする（既定は無効のまま。xtrig.output_enable）。

| 引数 | 型 | 必須 |
|---|---|---|
| `channel` | `number` | ○ |
| `mode` | `"pulse" | "hold" | "while"` |  |
| `level` | `"high" | "low"` |  |
| `pattern` | `Record<string, unknown> | null` |  |
| `sources` | `Array<"la_trigger" | "dap_match" | "host" | "sequence"> | null` |  |
| `pulse_us` | `number | null` |  |
| `enable` | `boolean` |  |

## xtrig.output_enable

種類: `control`。出力の端子の動作を有効にする（脈・保持は次の条件の立ち上がりから、条件の間はすぐ）。

| 引数 | 型 | 必須 |
|---|---|---|
| `channel` | `number` | ○ |

## xtrig.output_disable

種類: `control`。出力の端子の動作を無効にする（保持も戻る）。端子は出力に使ったまま駆動しない（高インピーダンス）。

| 引数 | 型 | 必須 |
|---|---|---|
| `channel` | `number` | ○ |

## xtrig.output_release

種類: `control`。出力の端子の動作を無効にし、設定を消して、端子を普段の機能（UART・SWO）に戻す。

| 引数 | 型 | 必須 |
|---|---|---|
| `channel` | `number` | ○ |

## xtrig.output_status

種類: `control`。出力の端子の候補ごとの状態: channel（ロジアナの ch）・pull_up（外せないプルアップ）・configured・enabled・active（いま駆動中）・claim・mode・level・pattern・sources・脈の長さ。actions = 同時に動かせる数、free = 空いている数。この構成に出力の端子が無ければ空。

## gpio.read

種類: `control`。拡張ヘッダの汎用入出力 8 本の状態（U41）: drive（出力の値）・enable（出力にしている端子）・level（端子の値）。ビット n = ロジアナの ch n（Primer の J12: 0 SWCLK/TCK・1 TDI・2 nRESET・3 SWDIO/TMS・4 予備・5 UART RX・6 SWO/TDO・7 UART TX）。変えない。

## gpio.set

種類: `control`。拡張ヘッダの端子を出力にする・値を決める（U41）。mask の端子だけ、出力の値を drive、出力にするかを enable のビットにする（ほかの端子は変えない）。出力にした端子は普段の機能（デバッグの線・UART・SWO・クロストリガの出力）より優先して駆動する（戻すのは gpio.release）。ターゲットに接続中は断る。書いた後の状態を返す。

| 引数 | 型 | 必須 |
|---|---|---|
| `drive` | `number` | ○ |
| `enable` | `number` | ○ |
| `mask` | `number` |  |

## gpio.release

種類: `control`。mask の端子を出力にしない（普段の機能に戻す。U41）。spi.transfer の後の SPI の端子もこれで離す。

| 引数 | 型 | 必須 |
|---|---|---|
| `mask` | `number` |  |

## gpio.sequence

種類: `control`。drive の値の列を順に書き、書くごとに端子を読む（汎用のビットバング。U41・U42）。USB の 1 往復で最大 126 状態（CONTROL の EXCHANGE。プロトコル 2 のビットストリームが要る）。出力にする端子は先に gpio.set で決める。levels[i] は states[i] を書いた頃の端子（同期化の分、書く前の値のことがある。同じ値を 2 回書けば 2 回目には必ず見える）。ターゲットに接続中は断る。

| 引数 | 型 | 必須 |
|---|---|---|
| `states` | `Array<number>` | ○ |

## spi.transfer

種類: `control`。拡張ヘッダの端子で SPI の 1 回の転送（汎用の SPI マスタ。gpio のビットバングで、FPGA に回路を足さない。U42）。端子はロジアナの ch（Primer の J12: 0 SWCLK/TCK・1 TDI・2 nRESET・3 SWDIO/TMS・4 予備・5 UART RX・6 SWO/TDO・7 UART TX）。CS を選ぶ → data の語を送りながら受ける → 続けて read_length 語を fill（既定 全 1）を送りながら受ける → SCK を休止に戻して CS を離す（keep_cs なら選んだまま。次の転送で続ける）。mode 0〜3（CPOL・CPHA）、語長 bits 1〜32、lsb_first、cs_active_high。語は 16 進の文字列で 1 語 = ceil(bits / 8) バイト（上位から）。received は受けた全部の語（data の語数 + read_length）。速さは 1 ビット 2 状態・USB の 1 往復 126 状態（約 8 バイト / 往復）。長い配線で化けるなら stretch（各状態を 1 + stretch 回）。SPI の端子は転送の後も休止の値で駆動したまま: 使い終えたら gpio.release。SPI Flash の手順は本体ではなくプラグイン fprobe.plugins.spiflash。ターゲットに接続中は断る。

| 引数 | 型 | 必須 |
|---|---|---|
| `sck` | `number` | ○ |
| `mosi` | `number | null` |  |
| `miso` | `number | null` |  |
| `cs` | `number | null` |  |
| `data` | `string` |  |
| `read_length` | `number` |  |
| `mode` | `0 | 1 | 2 | 3` |  |
| `bits` | `number` |  |
| `lsb_first` | `boolean` |  |
| `cs_active_high` | `boolean` |  |
| `keep_cs` | `boolean` |  |
| `stretch` | `number` |  |
| `fill` | `number | null` |  |

## swo.start

種類: `control`。SWO の受信を始める。mode: "uart"（NRZ。TPIU の SPPR = 2）/ "manchester"（SPPR = 1。ケイパビリティの swo_manchester がある構成だけ。ビットごとに同期し直すので送り手との速さのずれに強い）。ターゲット側の TPIU（ACPR = TRACECLKIN / baud - 1、SPPR）と ITM はファームウェアかデバッガで設定しておくこと。受けられる速さ（sys 100 MHz）: UART は 25 Mbaud まで（速さが一致すること）、Manchester は 16.7〜20 Mbit/s までが実用。

| 引数 | 型 | 必須 |
|---|---|---|
| `baud` | `number` |  |
| `mode` | `"uart" | "manchester"` |  |

## swo.read

種類: `control`。timeout_s の間に届いた SWO のバイト列（ITM パケットのまま、16 進文字列）。

| 引数 | 型 | 必須 |
|---|---|---|
| `timeout_s` | `number` |  |

## swo.stop

種類: `control`。SWO の受信を止める。

## swo.decode

種類: `control`。timeout_s の間に届いた SWO を ITM / DWT のパケットに解読する（`swo_start` の後。`swo_read` と同じ受信を使うのでどちらか一方で読む）。packets（例外トレースは name 付き・PC サンプル・スティミュラス等）は先頭 limit 件、text はポートごとの printf の行。記録中なら共通の時間軸にも入れる。

| 引数 | 型 | 必須 |
|---|---|---|
| `timeout_s` | `number` |  |
| `limit` | `number` |  |

## swo.text

種類: `control`。timeout_s の間に届いた SWO から、ITM のポート port に printf された行（pending は改行の来ていない途中）。

| 引数 | 型 | 必須 |
|---|---|---|
| `port` | `number` |  |
| `timeout_s` | `number` |  |

## swo.configure

種類: `control`。ITM・DWT の SWO への出力を、ターゲットを止めずに設定する（前の設定は戻してから）。ports: ITM のスティミュラス・ポート。timestamps: ローカルのタイムスタンプ（timestamp_prescale 1/4/16/64）。exception_trace: 例外の入る・出る・戻る。counters: DWT のイベントカウンタ（cpi・exc・sleep・lsu・fold）の溢れ。data_trace: DWT の比較器で変数へのアクセスを値・PC・時刻付きで記録（expression は変数名・C の式・"0x番地:大きさ"、access read/write/rw、pc・value・offset のどれを出すか。ARMv7-M）。value_watch: 値一致（変数に value が読み書きされたら action halt = 止める / trace = 止めずに記録。expression を省くと番地を問わない）。swo_baud で SWO の受信も始め、core_clock_hz も渡すと TPIU（分周）も設定する。受信を始めてから出力を有効にする。trace_pin: SVD に DBGMCU_CR.TRACE_IOEN があれば SWO のピンを有効に。pyOCD のウォッチポイントと比較器を取り合わない。計画（比較器・書いたレジスタ・帯域の見積もり・警告）を返す。結果は `swo.trace`。

| 引数 | 型 | 必須 |
|---|---|---|
| `ports` | `Array<number> | null` |  |
| `timestamps` | `boolean` |  |
| `timestamp_prescale` | `1 | 4 | 16 | 64` |  |
| `exception_trace` | `boolean` |  |
| `counters` | `Array<"cpi" | "exc" | "sleep" | "lsu" | "fold"> | null` |  |
| `data_trace` | `Array<Record<string, unknown>> | null` |  |
| `value_watch` | `Array<Record<string, unknown>> | null` |  |
| `swo_baud` | `number | null` |  |
| `core_clock_hz` | `number | null` |  |
| `trace_pin` | `boolean` |  |

## swo.trace

種類: `control`。`swo.configure` の結果（設定中ならいままでの分、戻した後は最後の結果）。accesses: データトレース・値一致（trace）のアクセス（t = タイムベースの時刻・変数・値（型で解釈）・読み書き・番地・PC とその関数と行）の offset から limit 件（name で変数を絞る）、counters: イベントカウンタの合計（溢れ × 256）、exceptions: 例外ごとの回数。記録中なら記録の stream "swo"（kind data_access・counter_overflow）にも入る。

| 引数 | 型 | 必須 |
|---|---|---|
| `offset` | `number` |  |
| `limit` | `number` |  |
| `name` | `string | null` |  |

## swo.unconfigure

種類: `control`。`swo.configure` の設定を戻す（比較器を外し、DWT_CTRL・ITM の変えたビットを元に。SWO の受信と TPIU は残す）。届いている分までの結果（`swo.trace` と同じ形）を返す。

## rtt.start

種類: `control`。RTT の受信を始める（ターゲットを止めずに、プローブが背景で周期的に読んで貯める）。制御ブロックは address、無ければ ELF の _SEGGER_RTT、それも無ければ RAM を走査（ranges = [[番地, 長さ], ...]、省略時はターゲットのメモリマップの RAM）。text_channels（既定 [0]）は記録で行に区切るチャンネル。wake: poll（ホストが interval_s ごとに記述子を読む）/ sampler（FPGA の DAP サンプラが sampler_rate_hz で上りの WrOff を読み、変わったチャンネルだけを読む。何も来ない間はターゲットの線を使わない。サンプラは 1 つなので watch.set 等と同時には使えない）。interval_s: 背景の読みの周期（既定 0.01 秒）。

| 引数 | 型 | 必須 |
|---|---|---|
| `address` | `number | null` |  |
| `ranges` | `Array<Array<number>> | null` |  |
| `text_channels` | `Array<number> | null` |  |
| `wake` | `"poll" | "sampler"` |  |
| `interval_s` | `number | null` |  |
| `sampler_rate_hz` | `number` |  |

## rtt.read

種類: `read`。貯めた受信を読む（機器には触らない）。channel は RTT の上りの番号か semihosting（セミホスティングの出力）。since を省くと前回の続きから（読むと進む）、since（前回の next）を渡すとそこから（画面用。進まない）。timeout_s までデータを待つ。encoding: text / hex。lost はあふれて読めなかったバイト数。

| 引数 | 型 | 必須 |
|---|---|---|
| `channel` | `number | "semihosting"` |  |
| `since` | `number | null` |  |
| `max_bytes` | `number` |  |
| `encoding` | `"text" | "hex"` |  |
| `timeout_s` | `number` |  |

## rtt.write

種類: `control`。ターゲットへ送る（RTT の下りの番号。semihosting ならセミホスティングの標準入力）。text（UTF-8）か data_hex。ターゲットが読んで空くまで timeout_s 待つ。written は書けたバイト数。

| 引数 | 型 | 必須 |
|---|---|---|
| `channel` | `number | "semihosting"` |  |
| `text` | `string | null` |  |
| `data_hex` | `string | null` |  |
| `timeout_s` | `number` |  |

## rtt.stop

種類: `control`。RTT の受信を止める（貯めたものは `rtt_read` で読める）。

## rtt.status

種類: `read`。RTT の受信の様子（制御ブロックの番地・チャンネルの名前と大きさ・受信したバイト数・未読・最後の誤り）とセミホスティングの出力の量。wake（読みの起こし方と、サンプラなら idle = ターゲットに触れなかった周期等）、stats（poll の数・デバッグポートの読み書きの回数とバイト数）も。

## terminal.sources

種類: `read`。端末に出せる源の一覧（機器には触れない）: id（rtt0〜 / swo0〜swo31 / uart / semihosting）・kind・name・writable（RTT は同じ番号の下りがあるとき、UART、セミホスティングの標準入力）・received（貯めた量）。SWO のポートは swo_start の後（0 と、データの来たポート）。 uart には baud（いまのボーレート。まだ開いていなければ null）。

## terminal.read

種類: `read`。端末の源 source（rtt0〜 / swo0〜swo31 = ITM のポートの printf / uart / semihosting）の受信を位置 since から読む（UTF-8。ほかの読み手の位置は動かさない。next を次の since に）。UART・SWO は初めて読んだときから背景で受ける（SWO は swo_start の後）。timeout_s までデータを待つ。lost はあふれて読めなかったバイト数。書くのは rtt_write（RTT・semihosting）と uart_write。 times なら受け取りごとの時刻 times = [{at（data の中の文字の位置）, wall（壁時計の秒）, t（記録中に受けたものはタイムベースの時刻、ほかは null）}] と timebase_hz（記録中なら）も（画面の行の時刻・時間軸へ飛ぶ）。 terminals なら（RTT の源だけ）SEGGER の仮想端末の切り替え（SEGGER_RTT_SetTerminal・TerminalOut が書く 0xFF と番号 '0'〜'F'）を文字から除き、terminals = [{at（そこから後の文字の位置）, id（端末の番号 0〜15）}] に返す（番号がまだ来ていない末尾の 0xFF は次の読みに回す）。

| 引数 | 型 | 必須 |
|---|---|---|
| `source` | `string` | ○ |
| `terminals` | `boolean` |  |
| `since` | `number` |  |
| `max_bytes` | `number` |  |
| `timeout_s` | `number` |  |
| `times` | `boolean` |  |

## user.trace.start

種類: `control`。ターゲットが出す値とイベントを取り始める（止めない）。channels: ITM のポート（source itm、index 1〜23 等）か RTT のチャンネル（source rtt、rtt_start 済み）の値の型（i8〜u32・f32・qM.N・struct）と名前。events: ファームの fprobe_user.h の名前付きイベント（FPROBE_EVENT_START / STOP / MARK・FPROBE_VALUE_*。ITM のポート 24・25。名前は ELF の文字列）。event_rtt_channel: 名前付きイベントを RTT のこのチャンネルからも。ITM を使うなら swo_baud で SWO の受信も始め（受信が先、ポートを有効にするのは後）、core_clock_hz も渡すと TPIU も設定する。

| 引数 | 型 | 必須 |
|---|---|---|
| `channels` | `Array<Record<string, unknown>> | null` |  |
| `events` | `boolean` |  |
| `event_rtt_channel` | `number | null` |  |
| `swo_baud` | `number | null` |  |
| `core_clock_hz` | `number | null` |  |

## user.trace.stop

種類: `control`。値とイベントを取るのを止め（足した ITM のポートを戻す）、要約（数・区間の数・誤り）を返す。記録中なら stream "user" に書く（value・mark・start・stop・interval）: `timing_measure` の start / stop に {"stream": "user", "kind": "start", "body": {"name": …}} がそのまま使える。

## user.trace.get

種類: `control`。値とイベントの一覧（時刻順。取っている最中ならいままでの分）。name・kinds（value / mark / start / stop）で絞り、offset から limit 件。時刻はタイムベース（ITM は SWO のバイトのデバイスの時刻、RTT はホストの時刻を写したものと不確かさ）。

| 引数 | 型 | 必須 |
|---|---|---|
| `name` | `string | null` |  |
| `kinds` | `Array<"value" | "mark" | "start" | "stop"> | null` |  |
| `offset` | `number` |  |
| `limit` | `number` |  |

## user.trace.stats

種類: `control`。値とイベントの集計（Keil の Event Statistics・SystemView のユーザイベント相当）: 値は名前ごと（構造体は 名前.欄）の数・最小・最大・平均・最後、区間（start / stop を名前ごとに後入れ先出しで対に。入れ子・割り込みの中も正しい）は回数・最小・最大・平均・σ・分位・分布・外れ値（開始の時刻）、mark は回数。name で絞る。

| 引数 | 型 | 必須 |
|---|---|---|
| `name` | `string | null` |  |
| `bins` | `number` |  |
| `outliers` | `"iqr" | "zscore"` |  |
| `limit` | `number` |  |

## uart.write

種類: `control`。ターゲットの UART に送る（latin-1）。baud を指定するとボーレートも変える。

| 引数 | 型 | 必須 |
|---|---|---|
| `data` | `string` | ○ |
| `baud` | `number | null` |  |

## uart.read

種類: `control`。timeout_s の間に届いたものを返す（latin-1 で文字列化）。

| 引数 | 型 | 必須 |
|---|---|---|
| `timeout_s` | `number` |  |
| `baud` | `number | null` |  |

## uart.configure

種類: `control`。ターゲットの UART のボーレートを変える（送らない・読まない。端末の画面の設定）。変えた後の baud を返す。

| 引数 | 型 | 必須 |
|---|---|---|
| `baud` | `number` | ○ |

## uart.flow

種類: `control`。UART のハードウェアのフロー制御（RTS/CTS）。rts_channel / cts_channel: RTS を出す / CTS を読む端子（ロジアナの ch。候補はケイパビリティ uart_flow = 結果の pins。Primer は 4 予備・1 TDI。選んだ端子の普段の機能の出力は止める。null で使わない）。auto: 自動の RTS/CTS（CTS が出ている間だけ送り、受信の下流が満杯なら RTS を下げる。CTS を取らなければ出ているとみなす）。auto = false なら RTS は COM ポートの RTS どおり。線はアクティブ Low。設定と制御線の状態（`uart.lines` と同じ形）を返す。

| 引数 | 型 | 必須 |
|---|---|---|
| `auto` | `boolean` |  |
| `rts_channel` | `number | null` |  |
| `cts_channel` | `number | null` |  |

## uart.lines

種類: `control`。UART の制御線の状態: cts（CTS が出ている）・rts（RTS を出している）・host_rts（COM ポートの RTS）・host_break（COM ポートのブレークを送っている）と、フロー制御の設定（auto・rts_channel・cts_channel・pins）。

## recording.start

種類: `control`。共通の時間軸の記録を始める。記録中は、ロジアナの捕捉・DAP / UART / SWO / 電圧のイベント・時計の対応付け・印・
すべての操作（AI と人）・ターゲットの状態の変化が、デバイスのタイムベースの上の 1 つの記録に保存される。
長期の記録（日単位）: segment_s（分割の時間。既定 3600）・segment_bytes（分割の大きさ）・max_bytes（全体の容量の上限。超えたら古い分割から消す。粗い縮図は残る）・decimate_after_s（閉じてからこの秒数が経った分割から decimate_streams を消す。既定 dap / sample / swo / clock / waveform。件数の縮図は残る）のどれかを指定すると、分割した記録になる（`recording_segments`・`recording_overview`）。

| 引数 | 型 | 必須 |
|---|---|---|
| `name` | `string` |  |
| `events` | `boolean` |  |
| `segment_s` | `number | null` |  |
| `segment_bytes` | `number | null` |  |
| `max_bytes` | `number | null` |  |
| `decimate_after_s` | `number | null` |  |
| `decimate_streams` | `Array<string> | null` |  |

## recording.stop

種類: `control`。記録を止める。

## recording.status

種類: `read`。記録中か、名前・件数・時刻の範囲（タイムベース）・ホストの出来事の時刻の不確かさ。

## recording.list

種類: `read`。保存した記録の一覧（新しい順）。

## recording.open

種類: `read`。保存した記録を開く（以後の読み出しの対象）。name は `recording_list` の name。

| 引数 | 型 | 必須 |
|---|---|---|
| `name` | `string` | ○ |

## recording.events

種類: `read`。時刻（タイムベース、t1 は含まない）の範囲の出来事を時刻順に。streams: dap / uart / swo / rtt / aux / host / target /
annotation / sample / clock。

| 引数 | 型 | 必須 |
|---|---|---|
| `t0` | `number | null` |  |
| `t1` | `number | null` |  |
| `streams` | `Array<string> | null` |  |
| `kinds` | `Array<string> | null` |  |
| `limit` | `number` |  |

## recording.find

種類: `read`。条件に一致する出来事の一覧。query: stream / kind / t0 / t1 / a / b（値か {"eq"|"lt"|"ge"…: 値}）/ body（{欄: 値か {"regex": …}}）。

| 引数 | 型 | 必須 |
|---|---|---|
| `query` | `Record<string, unknown>` | ○ |

## recording.intervals

種類: `read`。開始の条件から次の終了の条件までの時間（タイムベース）の一覧と統計（最小・最大・平均・標準偏差）。条件は `recording_find` と同じ。

| 引数 | 型 | 必須 |
|---|---|---|
| `start` | `Record<string, unknown>` | ○ |
| `end` | `Record<string, unknown>` | ○ |

## recording.density

種類: `read`。画素ごとの出来事の件数（縮図・タイムラインの要約）。長期の記録は事前計算した縮図の和で正確に出す。間引いた・消した分割にかかれば coverage（欠けの一覧。その部分は近似）。

| 引数 | 型 | 必須 |
|---|---|---|
| `t0` | `number` | ○ |
| `t1` | `number` | ○ |
| `width` | `number` |  |
| `streams` | `Array<string> | null` |  |

## recording.waveform

種類: `read`。記録の波形を画素ごとに '0' / '1' / 'x'（変化）/ '-'（波形が無い）。

| 引数 | 型 | 必須 |
|---|---|---|
| `t0` | `number` | ○ |
| `t1` | `number` | ○ |
| `width` | `number` |  |

## recording.waveform_measure

種類: `read`。記録の波形の時刻の範囲 [t0, t1)（タイムベース。省略で全体）を `la_measure` と同じく測る（周波数・周期・デューティ・パルス幅・relations のチャンネル間の時間差）。捕捉（かたまり）ごとに測って合わせ、かたまりをまたぐ周期・時間差は作らない。位置（*_at・violations の at）はタイムベース。

| 引数 | 型 | 必須 |
|---|---|---|
| `t0` | `number | null` |  |
| `t1` | `number | null` |  |
| `channels` | `Array<number> | null` |  |
| `relations` | `Array<Record<string, unknown>> | null` |  |

## recording.mark

種類: `control`。印とメモを残す（調査の経緯。人と AI の両方が使う）。t（タイムベース）を省くと記録中の記録のいまの時刻、指定するとその時刻（記録中の記録か、開いた記録。時間軸で選んだ時刻に後から印を置く。長期の記録は記録中だけ）。

| 引数 | 型 | 必須 |
|---|---|---|
| `text` | `string` | ○ |
| `t` | `number | null` |  |

## recording.export

種類: `read`。記録を書き出す。format: vcd（波形）/ csv / json（出来事）/ perfetto（Chrome JSON のトレースイベント。Perfetto UI（ui.perfetto.dev）・trace_processor・chrome://tracing で開く。タスク・割り込み・テストポイントの関数・解読・API の操作はスライス、変数（サンプラの語・テストポイントの変数）・電圧・ロジアナのチャンネルはカウンタ、DAP・UART・印は点。ts は記録の最初からの µs）。t0 / t1（タイムベース）・streams は csv・json・perfetto の範囲と種類（perfetto は範囲をまたぐ区間も含める。波形は streams に "waveform"）、waveform・channels は perfetto の波形（channels は vcd も）。

| 引数 | 型 | 必須 |
|---|---|---|
| `format` | `"vcd" | "csv" | "json" | "perfetto"` | ○ |
| `path` | `string` | ○ |
| `t0` | `number | null` |  |
| `t1` | `number | null` |  |
| `streams` | `Array<string> | null` |  |
| `waveform` | `boolean` |  |
| `channels` | `Array<number> | null` |  |

## recording.query

種類: `read`。記録（記録中のもの、または開いたもの）に読み取り専用の SQL を 1 文（AI の集計・検索用）。表: events(t, t_end, stream, kind, a, b, body, unc)（時刻はタイムベース、body は JSON で json_extract(body, '$.text') の形で読む）、chunks(t0, t1, path, ticks_per_sample, channels, length)（波形のかたまり）。関数: timebase_hz()、seconds(t)。読み以外（書き・PRAGMA・ATTACH・一時表）は断る。params は ? か :名前 の値。limit 行・timeout_s 秒まで（超えたら truncated / 誤り）。結果は columns と rows（列の並び）。長期の記録は t0 / t1 に重なる分割（8 個まで。多ければ断る）をつないで読み、表 segments・summary も読める。t0 / t1 を省くと新しい 8 個（それより多ければ partial = true）。結果に segments（つないだ分割の番号）。

| 引数 | 型 | 必須 |
|---|---|---|
| `sql` | `string` | ○ |
| `params` | `Array<unknown> | Record<string, unknown> | null` |  |
| `limit` | `number` |  |
| `timeout_s` | `number` |  |
| `t0` | `number | null` |  |
| `t1` | `number | null` |  |

## target.flash.erase

種類: `control`。Flash を消す。mode: range（address から length バイトにかかるセクタ。境界まで広がる）/ chip（Flash の領域すべて）/ mass（チップの mass erase。保護の解除等を伴うことがある）。ターゲットは止まったまま（プログラムの状態は壊れる）。消した範囲・セクタ数・速度を返す。 既定は消す前にリセットする。preserve でしない。

| 引数 | 型 | 必須 |
|---|---|---|
| `mode` | `"range" | "chip" | "mass"` |  |
| `address` | `number | null` |  |
| `length` | `number | null` |  |
| `preserve` | `boolean` |  |

## target.flash.verify

種類: `control`。ファイル（ELF / HEX / BIN。BIN は address）とターゲットのメモリを比べる（書かない・止めない）。match、違うバイト数、違う箇所（番地・期待値・実際の値。先頭 32 箇所）、読み出しの速度。

| 引数 | 型 | 必須 |
|---|---|---|
| `path` | `string` | ○ |
| `address` | `number | null` |  |

## target.flash.upload

種類: `control`。画面で選んだ書き込むファイル（ELF / HEX / BIN。中身は data_base64）をデーモンの PC の FPROBE_HOME/uploads/<name> に置き、path・bytes・format（elf / hex / bin）を返す（読まない。ブラウザはファイルの場所を渡せないため。返った path を target.flash・target.flash.verify に渡す。BIN は番地 address が要る）。64 MB まで。

| 引数 | 型 | 必須 |
|---|---|---|
| `name` | `string` | ○ |
| `data_base64` | `string` | ○ |

## target.flash.read

種類: `control`。ターゲットのメモリを読んでファイルに保存する（.hex / .ihex は Intel HEX、ほかは BIN）。address・length 省略時は起動する Flash（隙間なく続く領域を含む）の全体。止めない。CRC-32 と読み出しの速度を返す。

| 引数 | 型 | 必須 |
|---|---|---|
| `path` | `string` | ○ |
| `address` | `number | null` |  |
| `length` | `number | null` |  |

## target.flash.regions

種類: `control`。Flash の領域（番地・大きさ・ページ・セクタの並び・消した値・起動するか・アルゴリズムの出所 builtin / pack / flm:<path>）と、`target_flash_algo_add` で足したアルゴリズム。

## target.flash.algo.add

種類: `control`。CMSIS の書き込みアルゴリズム（.FLM）で Flash の領域を足す（外付けの QSPI NOR 等。以後 `target_flash` 等がその番地に使える）。address・size 省略時は FLM の値、アルゴリズムを置く RAM は ram_address / ram_size（省略時は既定の RAM）。接続し直しても足し直す。未接続なら覚えておき、接続後の最初の操作で足す。

| 引数 | 型 | 必須 |
|---|---|---|
| `flm` | `string` | ○ |
| `address` | `number | null` |  |
| `size` | `number | null` |  |
| `ram_address` | `number | null` |  |
| `ram_size` | `number | null` |  |
| `name` | `string | null` |  |

## target.flash.algo.remove

種類: `control`。`target_flash_algo_add` で足した領域を外す。

| 引数 | 型 | 必須 |
|---|---|---|
| `name` | `string` | ○ |

## target.flash.benchmark

種類: `control`。書き込み・読み出しの速度を計る。ram_length: 既定の RAM へ書いて読む（中身と走る状態を戻す。0 で省く）。read_length: 起動する Flash を読む（0 で省く）。program_address（と program_length、省略時は 1 セクタ）: そのセクタを消して書いて読み比べ、元の中身・RAM・レジスタ・走る状態に戻す（使っていないセクタを選ぶ）。SWD の周波数と各段のバイト数・秒・bytes_per_s。

| 引数 | 型 | 必須 |
|---|---|---|
| `ram_length` | `number` |  |
| `read_length` | `number` |  |
| `program_address` | `number | null` |  |
| `program_length` | `number | null` |  |

## target.pack.find

種類: `read`。CMSIS-Pack の索引を部品番号で探す（グロブ。ワイルドカードが無ければ部分一致）。索引が無いか update なら取ってくる（ネットに出る）。部品・メーカー・Pack・版・入れてあるか。

| 引数 | 型 | 必須 |
|---|---|---|
| `pattern` | `string` | ○ |
| `update` | `boolean` |  |

## target.pack.install

種類: `control`。部品番号に合う部品の CMSIS-Pack を落として入れる（ネットに出る）。以後 `target_connect` の target に返した targets の名前が使える（Pack の書き込みアルゴリズム・メモリマップ）。

| 引数 | 型 | 必須 |
|---|---|---|
| `pattern` | `string` | ○ |
| `update` | `boolean` |  |

## target.pack.list

種類: `read`。入れてある CMSIS-Pack（名前・版・ファイル）。

## target.pack.load

種類: `control`。手元の .pack ファイルのデバイスをターゲットとして登録する（この実行の間。索引に無い Pack・版を固定したいとき）。`target_connect` に使える名前を返す。

| 引数 | 型 | 必須 |
|---|---|---|
| `path` | `string` | ○ |

## snapshot.save

種類: `control`。止まったターゲットの状態を 1 つのファイル（.fsnap、既定は ~/.fprobe/snapshots）に保存する: コアレジスタ（浮動小数点も）・RAM・システム制御空間（SCB・NVIC・SysTick 等。読むと変わるデバッグのレジスタは読まない）・SVD の周辺レジスタ（readAction とデータ・FIFO の口は読まない）・停止の理由と場所・RTOS のタスクの一覧・故障の解析・記録の参照（記録中なら stream "snapshot" にも）・ELF の指紋（embed_elf で ELF も）。halt なら動いていても止める。ranges（[[番地, 長さ], …]）を省くとメモリマップの RAM すべて、flash で Flash も。peripherals は周辺の名前（省略で SVD のすべて、[] で読まない）。

| 引数 | 型 | 必須 |
|---|---|---|
| `name` | `string` |  |
| `path` | `string | null` |  |
| `halt` | `boolean` |  |
| `ranges` | `Array<unknown> | null` |  |
| `flash` | `boolean` |  |
| `peripherals` | `Array<unknown> | null` |  |
| `embed_elf` | `boolean` |  |
| `note` | `string` |  |

## snapshot.open

種類: `control`。スナップショットを開く（未接続のとき）。以後 `debug_backtrace`・`debug_evaluate`・`debug_frame_variables`・`rtos_tasks`・`rtos_backtrace`・`target_peripheral_read`・`target_fault`・`target_memory_view` がスナップショットを読む（書き込み・実行は不可）。ELF は elf、無ければ埋め込んだもの、それも無ければ保存した時の場所（指紋が合うときだけ）。`target_connect` で閉じる。

| 引数 | 型 | 必須 |
|---|---|---|
| `path` | `string` | ○ |
| `elf` | `string | null` |  |

## snapshot.close

種類: `control`。開いたスナップショットを閉じる。

## snapshot.info

種類: `read`。スナップショットの説明（path 省略で開いているもの）: 時刻・停止の理由と場所・区画・ELF の指紋・記録の参照と、保存した時の RTOS のタスクの一覧・故障の解析・読まなかった周辺レジスタ。

| 引数 | 型 | 必須 |
|---|---|---|
| `path` | `string | null` |  |

## snapshot.list

種類: `read`。保存したスナップショットの一覧（新しい順。止まった場所・ELF・メモ）。

## snapshot.export_core

種類: `read`。スナップショット（path 省略で開いているもの）を ELF のコアファイル（NT_PRSTATUS・PT_LOAD。GDB の core-file で読める）に書き出す。out 省略で拡張子 .core。

| 引数 | 型 | 必須 |
|---|---|---|
| `path` | `string | null` |  |
| `out` | `string | null` |  |

## snapshot.restore

種類: `control`。接続中のターゲットをスナップショットの状態に戻す: 止めて、ELF のコード（スナップショットの RAM の外）が同じかを確かめ（force で省く）、RAM・システムのレジスタ（VTOR・優先度・割り込みの許可と保留・SysTick・CPACR・FPU）・peripherals に挙げた周辺・コアレジスタを書く。resume で再開。Flash は書かない。

| 引数 | 型 | 必須 |
|---|---|---|
| `path` | `string` | ○ |
| `resume` | `boolean` |  |
| `peripherals` | `Array<unknown> | null` |  |
| `force` | `boolean` |  |

## rules.check

種類: `read`。規則の検査（宣言的な規則集を記録に当てる）。rules（辞書）か path（JSON / YAML のファイル）のどちらか: {name, requirements: {要件 ID: 文}, rules: [{id, kind, requirement, severity, allow_empty, ...}]}。kind: interval（start・end・min_us・max_us）/ period（event・period_us と tolerance_us、または min_us・max_us）/ sequence（trigger・steps・within_us・forbid）/ count（event・min・max・window_us）/ never（event）/ value（event・field・min・max・equals）/ states（event・field・allowed = [from, to] の列・labels・initial）。条件は `recording_find` と同じ、field は a / b / kind / body.<名前>。結果は規則ごとの verdict（pass / fail / warn / no_data）・expected・measured・violations（t・at_us・kind・message）と全体の verdict。mark（既定 true）で違反を記録の stream "rule"（kind violation）に派生イベントとして書く（同じ規則集の前の印は置き換え）。baseline（`recording_list` の name）で基準の記録の実測と比べる（baseline.delta）。範囲は t0 / t1（タイムベース）。

| 引数 | 型 | 必須 |
|---|---|---|
| `rules` | `Record<string, unknown> | null` |  |
| `path` | `string | null` |  |
| `t0` | `number | null` |  |
| `t1` | `number | null` |  |
| `baseline` | `string | null` |  |
| `mark` | `boolean` |  |
| `limit` | `number` |  |

## rules.states

種類: `read`。状態遷移図: 記録の出来事（event は `recording_find` と同じ条件）の field（a / b / kind / body.<名前>）の値を状態として、状態ごとの入った回数・滞在時間（µs）、遷移ごとの回数と最初の時刻、Graphviz の dot（dot_path にも書く）。labels は {値: 名前}。例: 変数の状態 = event {stream: "sample", a: 番地}, field "b"。

| 引数 | 型 | 必須 |
|---|---|---|
| `event` | `Record<string, unknown>` | ○ |
| `field` | `string` | ○ |
| `labels` | `Record<string, unknown> | null` |  |
| `t0` | `number | null` |  |
| `t1` | `number | null` |  |
| `dot_path` | `string | null` |  |

## rules.report

種類: `read`。証跡を書く: 最後の `rules_check` と `inject_run` の結果を、要件ごとの判定（要件 → 規則・試行 → 判定）・規則ごとの期待値と実測・違反の時刻・基準との差・故障注入の試行・記録と ELF（SHA-256）・規則集（SHA-256）を含む 1 つの文書（format markdown / json）として path に書く。

| 引数 | 型 | 必須 |
|---|---|---|
| `path` | `string` | ○ |
| `format` | `"markdown" | "json"` |  |

## inject.run

種類: `control`。故障注入の台本を試行する（クロストリガの割り込み書き込み: 条件の成立から数 µs で、ハードウェアが任意の番地へ 1 語書く）。script（辞書）か path（JSON / YAML）: {name, requirements, trials: [{id, requirement, when, write, expect, restore, timeout_s, observe_s, repeat}]}。when は 1 つ: pattern {mask, value, rise, fall}（ピン）/ sequence（`xtrig_set_sequence` と同じ stages・pattern_a・pattern_b）/ dap_match（`xtrig_set_match` と同じ）/ host {delay_s}。write: {address（数か ELF の変数名。4 の倍数）, value か flip（仕掛けた時点の値とのビット反転）, ap}。expect: {state: running|halted} / {expression: C の式, equals|not_equals|min|max} / {memory: {address, size}, 同じ比較} / {rules: [規則]}（注入から observe_s の間の記録。記録が要る）。restore: {write: [{address, value, size}], reset: 種類, resume}。試行ごとの verdict: pass / fail / not_triggered / inject_error / error と、注入の時刻 t（タイムベース）。記録中なら stream "inject"（kind trial）に入る。最後に割り込み書き込みを既定（停止）へ戻す。

| 引数 | 型 | 必須 |
|---|---|---|
| `script` | `Record<string, unknown> | null` |  |
| `path` | `string | null` |  |

## hooks.add

種類: `control`。デバッグの出来事のフックを登録する（デーモンが、AI・画面・GDB のどの操作でも同じに実行する）。event: connect / disconnect / reset / flash（書き込み・RAM への読み込み・消去）/ resume / halt、phase: before / after。halt の after は操作に限らず「止まったのを見つけた後」（ブレークポイント・GDB・ステップも。同じ止まりで 1 回）。actions（順に実行）: {"evaluate": C の式}（`debug_evaluate`）/ {"mark": 文}（記録の印。{event} {phase} {method} {client} {pc} {where} を置き換え）/ {"call": API のメソッド名, "params": {...}}（hooks.* と session.* は不可）。required の before フックの動作が失敗したら操作をしない（誤りで返る）。フックの中の操作はフックを起こさない。結果は `hooks_log`・通知 hook・記録の stream "hook"。

| 引数 | 型 | 必須 |
|---|---|---|
| `event` | `"connect" | "disconnect" | "reset" | "flash" | "resume" | "halt"` | ○ |
| `phase` | `"before" | "after"` | ○ |
| `actions` | `Array<Record<string, unknown>>` | ○ |
| `name` | `string` |  |
| `required` | `boolean` |  |
| `enabled` | `boolean` |  |

## hooks.remove

種類: `control`。フックを外す（id、または all=true ですべて）。

| 引数 | 型 | 必須 |
|---|---|---|
| `id` | `number | null` |  |
| `all` | `boolean` |  |

## hooks.list

種類: `read`。登録したフックの一覧（id・event・phase・actions・name・required・enabled）。AI と人が同じものを見る。

## hooks.log

種類: `read`。フックの実行の記録（最新 last 件。seq・時刻・フック・event・phase・きっかけの操作 method と利用者 client・動作ごとの ok と value か error・所要時間）。

| 引数 | 型 | 必須 |
|---|---|---|
| `last` | `number` |  |

## hooks.save

種類: `read`。フックの宣言を JSON のファイルに書く（`hooks_load` で読み直せる。プロジェクトに置いて人と AI で共有する）。

| 引数 | 型 | 必須 |
|---|---|---|
| `path` | `string` | ○ |

## hooks.load

種類: `control`。JSON のファイル（`hooks_save` の形 {hooks: [...]}）からフックを読む。replace（既定 true）なら今のフックを置き換え、false なら足す。全部を確かめてから入れる。

| 引数 | 型 | 必須 |
|---|---|---|
| `path` | `string` | ○ |
| `replace` | `boolean` |  |

## heap.scan

種類: `control`。ヒープを辿る（ELF のシンボルから。DWARF 不要）: allocator = freertos（heap_4 / heap_5）/ newlib_nano / newlib（dlmalloc）。省略で ELF にあるもの全部。使用量・空き・最大の空き・断片化（1 − 最大の空き / 空きの合計）・大きさの分布・アロケータのカウンタと、壊れ（issues: bad_size = 大きさが不正。suspect は直前の使用中のブロック = あふれの疑い / bad_link / bad_end / free_list_loop・unsorted・outside / free_not_listed / listed_not_free / unmerged_free / bad_padding / bad_links・bad_footer（dlmalloc）/ counter_mismatch）。heap_5 の領域は xHeapRegions か regions（[[先頭, 大きさ]]）、newlib-nano のヒープの終わりは sbrk の変数か end。blocks でブロックの一覧（先頭から）、compare で前回の走査からの増減（増えた使用中のブロック = リークの手がかり）。`heap_trace_start` 中なら使用中のブロックを呼び出し元ごとに（callers）。止まっているときだけ（live=true で走行中にも: 一貫しない）。

| 引数 | 型 | 必須 |
|---|---|---|
| `allocator` | `"freertos" | "newlib_nano" | "newlib" | null` |  |
| `regions` | `Array<Array<number>> | null` |  |
| `end` | `number | null` |  |
| `live` | `boolean` |  |
| `blocks` | `number` |  |
| `compare` | `boolean` |  |

## heap.trace.start

種類: `control`。malloc / free の記録を始める（止めずに）: 確保の関数（FreeRTOS pvPortMalloc・newlib _malloc_r）の入口で大きさと呼び出し元、戻ったところで返した番地を、解放の関数（vPortFree・_free_r）の入口で番地を読む（ブレークポイント。当たるたびにコアを数 ms 止める）。calloc / realloc は中の確保・解放として残る。allocators で絞る（省略で ELF にあるもの）。記録中なら stream "heap"（kind alloc / free、a = 番地、b = 大きさ）にも入る。→ `heap_trace_report`・`heap_trace_events`・`heap_trace_stop`。

| 引数 | 型 | 必須 |
|---|---|---|
| `allocators` | `Array<"freertos" | "newlib_nano" | "newlib"> | null` |  |

## heap.trace.stop

種類: `control`。malloc / free の記録を止める（ブレークポイントを外す。記録は `heap_trace_report` で読める）。

## heap.trace.report

種類: `read`。malloc / free の記録の要約: 確保・解放の回数、生きている確保（数・バイト）、最大の使用量、誤り（double_free・failed_allocation・reused_live_pointer）、リークの候補（leak_candidates: 呼び出し元ごとの、min_age_s 秒より古い生きている確保。growing = 記録の間に生きている数が増え続けた = 強い候補）、使用量の推移（series: 記録を始めてからの秒, バイト）。

| 引数 | 型 | 必須 |
|---|---|---|
| `min_age_s` | `number` |  |
| `top` | `number` |  |

## heap.trace.events

種類: `read`。記録した確保・解放の出来事（op・pointer・size・caller・t = 記録を始めてからの秒）を古い順に。seq が since より後を limit 件、next を次の since に。

| 引数 | 型 | 必須 |
|---|---|---|
| `since` | `number` |  |
| `limit` | `number` |  |

## recording.segments

種類: `read`。長期の記録の分割の一覧（no・path・state = open / sealed / deleted・時刻の範囲 t0 / t1・件数・バイト数・dropped = 間引いた stream）と方針（policy）。普通の記録は segmented = false。

## recording.overview

種類: `read`。記録の全体の縮図（ミニマップ。Tracealyzer の Trace Preview の相当）: 画素ごと・stream ごとの出来事の件数（streams）と合計（total）、波形（waveform: チャンネルごとに '0' / '1' / 'x' / '-'）、分割の状態（segments）。画素の境は edges（タイムベース）。長期の記録は事前計算した索引から出すので 30 日でも速く、消した分割の件数も残る。streams で種類を絞る。

| 引数 | 型 | 必須 |
|---|---|---|
| `width` | `number` |  |
| `streams` | `Array<string> | null` |  |

## recording.la_stream.start

種類: `control`。記録中に、ロジアナを止めずに流し続けて記録へ入れる（`recording_la_stream_stop` まで。日単位も可）。chunk_s 秒ごとの波形のかたまりにし、時刻はデバイスのタイムベース（サンプル 0 の基準 anchor: 深いバッファのトリガ時刻か、始める前後の読みの中点。不確かさ付き）。rle = true は変化点圧縮（遅い信号向き）、false は RAW（12.5 MS/s 程度まで）。イベントも流す構成（`probe.design.get` の features.la_stream_events。fprobe_la）では、イベント記録（DAP・UART・SWO・補助の出来事）も同じ流れでデバイスの時刻のまま記録へ入り続ける（止めると取り込みは通常の周期の読みに戻る。欠けも二重も出ない）。ほかの構成では、流している間はイベント記録を取り込めない。どちらでも、流している間はロジアナ・イベント記録の道具は使えない（読み出しの経路が同じ）。溢れたら（ロジアナの未読がリングに迫る・イベントの未読がイベントのリングの半分）止まり、記録に印（annotation la_overrun）が残る。

| 引数 | 型 | 必須 |
|---|---|---|
| `rate_hz` | `number | null` |  |
| `rle` | `boolean` |  |
| `chunk_s` | `number` |  |

## recording.la_stream.stop

種類: `control`。ロジアナの流し続けを止める（残りを出し切って記録へ入れる）。サンプル数・長さ・溢れ（overrun）・時刻の基準（anchor）・かたまりの数・記録へ入れたイベントの数（events。イベントも流す構成のとき）を返す。

## debug.elf.watch

種類: `control`。読み込んだ ELF の更新の見張りの設定（既定で有効）。更新（再ビルド）を見つけると auto_reload なら読み込み直し、通知 elf.changed（AI には次の道具の結果の notices）で知らせる。ターゲットに書いた中身と違えば target_image = stale（`target_flash` で書き直す）。interval_s は見回りの間隔。

| 引数 | 型 | 必須 |
|---|---|---|
| `enabled` | `boolean` |  |
| `auto_reload` | `boolean` |  |
| `interval_s` | `number` |  |

## debug.elf.status

種類: `read`。読み込んだ ELF と更新の見張りの様子（機器には触れない）: path・loaded_at・file_changed（読み込んだ後にファイルが変わったか）・target_image（current / stale = 書き直しが要る / unknown）・changes・reloads・last（最後の更新の知らせ）・error。

## debug.halt_risk

種類: `read`。コアを止める操作（中断・ブレークポイント・ステップ・関数の呼び出し）で壊れうるか（ELF のシンボルだけ。機器には触れない）: risky、reasons（重い順。kind = radio（無線の割り込み・BLE 等のスタック）/ usb（USB の機器）/ watchdog / rtos、count、symbols = 手がかりのシンボルの先頭、rtos = RTOS の名前）、monitor_in_elf（止めずにデバッグするモニタがファームにあるか。あれば target.debug_mode.set の mode="monitor"）。risky なら止める前に人に知らせ、止めずに調べる道具（watch.set・rtos.tasks の live・profile.start）かモニタ方式を先に考える。

## watch.trigger

種類: `control`。観測している変数の取得を条件で始める・止める（`watch_set` の後。結果は `watch_capture`）。start / stop: {"variable", "op"（> >= < <= == !=）, "value", "edge"（偽→真の変化で）} か {"event": "xtrig"}（クロストリガの割り込み書き込みの時刻。`xtrig_route` の to_inject にロジアナのトリガ・DAP の一致・多段の条件をつなぐ）。start を省くといまから。pre_s: 始まりより前の分（プリトリガ）、post_s: 始まりから取る長さ、max_samples: 件数の上限。

| 引数 | 型 | 必須 |
|---|---|---|
| `start` | `Record<string, unknown> | null` |  |
| `stop` | `Record<string, unknown> | null` |  |
| `pre_s` | `number` |  |
| `post_s` | `number | null` |  |
| `max_samples` | `number` |  |

## watch.capture

種類: `read`。条件つきの取得（`watch_trigger`）の状態と結果（機器には触れない）。終わるまで wait_s 待つ。state: armed / capturing / done（reason: stop / post / max_samples / cancelled）、t_start_us・t_end_us、samples（変数ごとの値の列。時刻は `watch_series` と同じ軸の µs）。

| 引数 | 型 | 必須 |
|---|---|---|
| `wait_s` | `number` |  |

## watch.write

種類: `control`。走行中の変数に書く（止めない）。expression: 大域・静的の C の式（ポインタをたどってもよい）か番地（"0x20000000:4" は整数）。value: 数・列挙の名前・真偽。書いた後に読み直した値を返す。ビットフィールドは読み・変え・書き（atomic = false: ターゲットが同じ語を同時に書くと失われうる）。

| 引数 | 型 | 必須 |
|---|---|---|
| `expression` | `string` | ○ |
| `value` | `number | string | boolean` | ○ |

## watch.buffer_start

種類: `control`。ターゲット側のバッファを吸い出す（Snapshot 方式。速い変化を取りこぼさず、ターゲットの周期に同期する）。ファームが配列 array に値（構造体でも）を書いてから head を進める（counter = true: 書いた総数。false: 次に書く位置）。rate_hz ごとに head を読み、増えた要素だけを読んで DWARF の型で解釈する。読みは `watch_buffer_read`。

| 引数 | 型 | 必須 |
|---|---|---|
| `array` | `string` | ○ |
| `head` | `string` | ○ |
| `counter` | `boolean` |  |
| `rate_hz` | `number` |  |

## watch.buffer_read

種類: `read`。吸い出した要素（機器には触れない）: samples = [{seq（通し番号）, time_us（PC が読んだ時刻）, value}]。since を省くと前回の続きから（読むと進む）、渡すとそこから（画面用）。lost は一周されて読めなかった要素の数、next は次の since。

| 引数 | 型 | 必須 |
|---|---|---|
| `since` | `number | null` |  |
| `limit` | `number` |  |

## watch.buffer_stop

種類: `control`。ターゲット側のバッファの吸い出しを止める（吸い出した要素は `watch_buffer_read` で読める）。

## rtt.measure

種類: `read`。RTT の受信の速さを duration_s の間はかる（機器には触れない。背景の読みの数え上げを比べる。ターゲットが出し続けている間に呼ぶ）: bytes_per_s（チャンネルごとと合計）、polls_per_s、empty_poll_ratio、reads_per_s・read_bytes_per_s（デバッグポートの読み）、efficiency（受けたバイト ÷ 読んだバイト）、wake。

| 引数 | 型 | 必須 |
|---|---|---|
| `duration_s` | `number` |  |

## stream.serve

種類: `control`。生の流れ stream（rtt0〜 = RTT の上り / swo / uart / semihosting）を TCP の 127.0.0.1:port（0 なら空いているポート）で公開する（SEGGER の RTT Telnet サーバ・OpenOCD の RTT ポート・orbuculum に相当）。つながった全員が同じバイトを受け、送ったバイトはターゲットへ書く（rttN は下り N、uart、semihosting は標準入力。swo と writable = false は読むだけ）。書き込みは調停と操作の記録を通る。protocol: raw / telnet。流れの出どころは `rtt_start`・`swo_start` で別に始める。LAN には開かない。

| 引数 | 型 | 必須 |
|---|---|---|
| `stream` | `string` | ○ |
| `port` | `number` |  |
| `protocol` | `"raw" | "telnet"` |  |
| `writable` | `boolean` |  |

## stream.close

種類: `control`。流れの公開をやめる（つながっている相手は切れる）。

| 引数 | 型 | 必須 |
|---|---|---|
| `stream` | `string` | ○ |

## stream.log_start

種類: `control`。流れ stream をファイル path に書き始める（SEGGER の RTT Logger に相当。append = false なら書き直す）。timestamps: 行の頭にこの PC の時刻（文字の流れ用）。

| 引数 | 型 | 必須 |
|---|---|---|
| `stream` | `string` | ○ |
| `path` | `string` | ○ |
| `timestamps` | `boolean` |  |
| `append` | `boolean` |  |

## stream.log_stop

種類: `control`。流れのファイルへの記録をやめる。

| 引数 | 型 | 必須 |
|---|---|---|
| `stream` | `string` | ○ |

## stream.list

種類: `read`。公開中の流れ（ポート・相手ごとの送ったバイトと捨てたバイト・書き込みの誤り）・ファイルへの記録・流れの出どころ（RTT の上り・SWO の受信中か・背景の読みの誤り）。機器には触れない。

## log.start

種類: `control`。ログの流れの解読を始める（止めずに。すぐ戻る）。format: defmt（ELF の .defmt の表。RTT は既定で名前 "defmt" のチャンネル）/ trice（til・li = til.json・li.json、framing tcobs / cobs / none）/ compact（書式の文字列の番地 + 引数の語。ELF から引く）/ memfault（ELF の log_fmt。transport cbor / text = ML:…: の行）/ text（行）。source: rtt（channel は番号か名前）/ swo（ITM の port。swo_baud・core_clock_hz で受信と TPIU も設定）/ uart（プローブの UART の受信。時刻は FPGA が受けた時刻）。ELF は elf か読み込み済みのもの。annotate なら本文の中のコードの番地を関数・ファイル:行に。読むのは `log_read`。記録中なら stream "log"（kind = 重要度、解読できないものは undecoded）。

| 引数 | 型 | 必須 |
|---|---|---|
| `format` | `"text" | "defmt" | "trice" | "compact" | "memfault"` | ○ |
| `source` | `"rtt" | "swo" | "uart"` |  |
| `channel` | `number | string | null` |  |
| `port` | `number` |  |
| `elf` | `string | null` |  |
| `til` | `string | null` |  |
| `li` | `string | null` |  |
| `framing` | `"rzcobs" | "raw" | "tcobs" | "cobs" | "none" | null` |  |
| `big_endian` | `boolean` |  |
| `transport` | `"cbor" | "text" | null` |  |
| `annotate` | `boolean` |  |
| `swo_baud` | `number | null` |  |
| `core_clock_hz` | `number | null` |  |

## log.stop

種類: `control`。ログの受け取りを止める（貯めた行は `log_read` で読める）。要約（行の数・重要度ごとの数・解読できなかった数）。

## log.status

種類: `read`。ログの受け取りの様子（形式・行の数・重要度ごとの数・解読できなかった数・受けたバイト・誤り）。

## log.read

種類: `read`。解読したログの行（since の番号より後を limit 件）: 本文・重要度・書式・引数・ターゲットの時刻・ソースの場所・本文の中の番地の解読・受けた時刻 t（タイムベース）。level 以上の重要度だけ（重要度の無い行は常に）、contains を含むものだけ。next を次の since に。最近の 10000 行だけ持つ（全部は記録の stream "log"）。

| 引数 | 型 | 必須 |
|---|---|---|
| `since` | `number` |  |
| `limit` | `number` |  |
| `level` | `"trace" | "debug" | "info" | "warn" | "error" | null` |  |
| `contains` | `string | null` |  |

## log.decode

種類: `read`。ログのバイト（data_hex か path のファイル）を解読する（機器に触らない）。形式と引数は `log_start` と同じ。

| 引数 | 型 | 必須 |
|---|---|---|
| `format` | `"text" | "defmt" | "trice" | "compact" | "memfault"` | ○ |
| `data_hex` | `string | null` |  |
| `path` | `string | null` |  |
| `elf` | `string | null` |  |
| `til` | `string | null` |  |
| `li` | `string | null` |  |
| `framing` | `"rzcobs" | "raw" | "tcobs" | "cobs" | "none" | null` |  |
| `big_endian` | `boolean` |  |
| `transport` | `"cbor" | "text" | null` |  |
| `limit` | `number` |  |

## log.annotate

種類: `read`。文字（UART のパニックの出力・Backtrace の行等）の中のコードの番地を、ELF で関数・ファイル:行にする（機器に触らない。elf か読み込み済みの ELF）。

| 引数 | 型 | 必須 |
|---|---|---|
| `text` | `string` | ○ |
| `elf` | `string | null` |  |

## trace.start

種類: `control`。トレースの流れの取り込みを始める（すぐ戻る）。format: systemview（RTT の "SysView"。send_start なら開始のコマンドを下りに書く。description = SYSVIEW_<OS>.txt で OS の API の名前と引数）/ ctf（Zephyr。metadata = ビルドの TSDL、無ければ組み込みの表と ctf_header）/ tracealyzer（RTT の "TzData"。開始は "TzCtrl"）/ eventrecorder（source memory: Event Recorder の RAM を period_s ごとに止めずに読む。EventRecorderInfo か address。書式は `scvd_load`）。source: rtt / swo（ITM の port）/ uart / memory。記録中なら出来事を stream "trace" に。`trace_stop` で時間軸にも。

| 引数 | 型 | 必須 |
|---|---|---|
| `format` | `"systemview" | "ctf" | "tracealyzer" | "eventrecorder"` | ○ |
| `source` | `"rtt" | "swo" | "uart" | "memory"` |  |
| `channel` | `number | string | null` |  |
| `port` | `number` |  |
| `description` | `string | null` |  |
| `metadata` | `string | null` |  |
| `ctf_header` | `"ts32_id8" | "ts32_id16" | "ts64_id16" | "id16"` |  |
| `send_start` | `boolean` |  |
| `period_s` | `number` |  |
| `address` | `number | null` |  |
| `swo_baud` | `number | null` |  |
| `core_clock_hz` | `number | null` |  |

## trace.stop

種類: `control`。トレースの取り込みを止め、要約（出来事の種類ごとの数・タスク・失った数・時刻の速さ・時刻の対応）を返す。timeline ならタスクの出入りを時間軸にし、`rtos_timeline_get`・`rtos_timeline_load` 等の解析がそのまま使える（記録中なら stream "rtos" にも）。

| 引数 | 型 | 必須 |
|---|---|---|
| `timeline` | `boolean` |  |

## trace.status

種類: `read`。トレースの取り込みの様子（形式・出来事の数・タスク・失った数・時刻の対応・誤り）。

## trace.events

種類: `read`。取り込んだトレース（取り込み中・最後の結果・`trace_snapshot`・`trace_import`）の出来事を offset から limit 件（ターゲットの時刻 ts、タイムベースの時刻 t、種類 kind = task_in / task_out / task_ready / task_block / task_create / task_delete / isr_enter / isr_exit / idle / print / marker / object / overflow / info / other、形式での名前、値、本文）。kinds・task で絞る。

| 引数 | 型 | 必須 |
|---|---|---|
| `offset` | `number` |  |
| `limit` | `number` |  |
| `kinds` | `Array<string> | null` |  |
| `task` | `number | null` |  |

## trace.snapshot

種類: `control`。ターゲットの RAM のトレースのバッファを吸い出して解読する（ポストモーテム・スナップショット。止めなくてよいが、止めた方が書きかけが無い。`snapshot_open` 中ならその RAM から）。format: systemview（RTT の "SysView" のリングを消費せずに）/ ctf（Zephyr の ram_tracing。address なら size も）/ tracex（ThreadX の _tx_trace_header_ptr か address。ts_hz = 時刻の速さ）/ eventrecorder（EventRecorderInfo か address）。最後の出来事を読んだ時刻に合わせて記録・時間軸にする（近似）。

| 引数 | 型 | 必須 |
|---|---|---|
| `format` | `"systemview" | "ctf" | "tracex" | "eventrecorder"` | ○ |
| `address` | `number | null` |  |
| `size` | `number | null` |  |
| `description` | `string | null` |  |
| `metadata` | `string | null` |  |
| `ctf_header` | `"ts32_id8" | "ts32_id16" | "ts64_id16" | "id16"` |  |
| `ts_hz` | `number | null` |  |
| `timeline` | `boolean` |  |
| `rtt_address` | `number | null` |  |

## trace.import

種類: `control`。トレースのファイル（SystemView の .SVDat・Zephyr の CTF のストリーム・Tracealyzer の PSF・TraceX のバッファのダンプ）を解読する（機器に触らない）。時刻はターゲットの時刻のまま（記録には書かない）。timeline なら時間軸の解析にも。ram_buffer は CTF の RAM の backend のダンプ（0 埋めで終わる）。

| 引数 | 型 | 必須 |
|---|---|---|
| `format` | `"systemview" | "ctf" | "tracealyzer" | "tracex"` | ○ |
| `path` | `string` | ○ |
| `description` | `string | null` |  |
| `metadata` | `string | null` |  |
| `ctf_header` | `"ts32_id8" | "ts32_id16" | "ts64_id16" | "id16"` |  |
| `ts_hz` | `number | null` |  |
| `ram_buffer` | `boolean` |  |
| `timeline` | `boolean` |  |

## trace.etm.start

種類: `control`。ETM（ETMv4、Cortex-M33 等）の命令トレースを始める（CoreSight の標準の手順。止めずに設定できる）: DEMCR.TRCENA、ATB ファンネル（funnel_base があれば funnel_ports の入口）、TPIU（tpiu_base があれば port_size ビット・formatter = フォーマッタの継続モード、null なら触らない）、ETM（etm_base、trace_id、sync_period バイトごとに同期、全命令。cycle_counting・timestamps は任意）。ETMv4 でなければ断る。結果に ETM の版と構成（TRCIDRn）、書いたレジスタ。取ったバイト列は `trace_etm_decode` へ。RP2350（Pico 2）は配線なしでトレース FIFO + DMA に溜められる（プラグイン fprobe.plugins.rp2350_trace が番地を渡してこれを呼ぶ）。

| 引数 | 型 | 必須 |
|---|---|---|
| `etm_base` | `number` |  |
| `trace_id` | `number` |  |
| `tpiu_base` | `number | null` |  |
| `port_size` | `number` |  |
| `formatter` | `boolean` |  |
| `funnel_base` | `number | null` |  |
| `funnel_ports` | `number` |  |
| `cycle_counting` | `boolean` |  |
| `cycle_threshold` | `number` |  |
| `timestamps` | `boolean` |  |
| `sync_period` | `number` |  |
| `fsync_frames` | `number` |  |

## trace.etm.stop

種類: `control`。ETM を止め（TRCSTATR.IDLE を待つ = 出し切る）、TPIU のフォーマッタを出し切る（FFCR.FOnMan）。番地を省くと `trace_etm_start` のもの。

| 引数 | 型 | 必須 |
|---|---|---|
| `etm_base` | `number | null` |  |
| `tpiu_base` | `number | null` |  |

## trace.etm.decode

種類: `control`。命令トレースのバイト列を解読し、ELF（無ければ実機のメモリ。from_elf で選ぶ）と合わせて実行した命令の流れを再構成する。バイト列は data_hex・path（ファイル）・address + length（ターゲットのメモリのリング。oldest = 最も古いバイトの位置）のどれか 1 つ。formatted: TPIU のフォーマッタの枠付き（align = auto / fsync / start でフレームを揃え、trace_id の流れを取り出す。省略時は `trace_etm_start` の ID、無ければ最も多い ID）か、ETM のバイト列そのものか。結果は要約（命令の数・例外の数・解けなかったアトム・誤り・最初と最後の命令・次に実行する番地 next_address・ID ごとのバイト数）。続けて `trace_etm_history`（止まる直前の命令）/ `trace_etm_instructions` / `trace_etm_packets`。protocol は etmv4（ETMv3.5 は未対応）。

| 引数 | 型 | 必須 |
|---|---|---|
| `data_hex` | `string | null` |  |
| `path` | `string | null` |  |
| `address` | `number | null` |  |
| `length` | `number | null` |  |
| `oldest` | `number` |  |
| `formatted` | `boolean` |  |
| `align` | `"auto" | "fsync" | "start"` |  |
| `trace_id` | `number | null` |  |
| `protocol` | `"etmv4"` |  |
| `from_elf` | `boolean | null` |  |

## trace.etm.packets

種類: `read`。解読した ETM のパケット（共通の要素: async / trace_info / address / atoms（E = 成立、N = 不成立、古い順）/ exception / exception_return / timestamp / cycle_count / trace_on / overflow / decode_error 等。トレースの中の位置 offset とバイト raw 付き）を offset から limit 件。kinds で絞る。OpenCSD 等との照合に。

| 引数 | 型 | 必須 |
|---|---|---|
| `offset` | `number` |  |
| `limit` | `number` |  |
| `kinds` | `Array<string> | null` |  |

## trace.etm.instructions

種類: `read`。再構成した命令の一覧（実行順の通し番号 index・番地・命令・関数・ファイル:行。分岐の命令は branch = taken / not_taken / exception と行き先 target）を offset から limit 件と、その範囲の出来事 markers（例外の入口・戻り・時刻・途切れ。index = その前に実行した命令の数）。function で関数に絞る（通った回数・順序を見る）。

| 引数 | 型 | 必須 |
|---|---|---|
| `offset` | `number` |  |
| `limit` | `number` |  |
| `function` | `string | null` |  |

## trace.etm.history

種類: `read`。事後の命令履歴: 解読したトレースの最後（止まった所・故障の直前）の count 命令（古い順）と、その間の例外・途切れ、次に実行する番地 next_address。実行した命令は確実だが、M-profile の ETM にデータの値は無いので、レジスタ・メモリの過去の値は含まない（note）。

| 引数 | 型 | 必須 |
|---|---|---|
| `count` | `number` |  |

## scvd.load

種類: `control`。SCVD（Component Viewer Description: RTX5.scvd・EventRecorder.scvd・ミドルウェアのもの）を読む。`scvd_view` の表示と、Event Recorder の出来事の名前・書式・状態に使う（前に読んだものに足す）。

| 引数 | 型 | 必須 |
|---|---|---|
| `paths` | `Array<string>` | ○ |

## scvd.view

種類: `control`。SCVD の objects（Component Viewer）を評価して、ターゲットのメモリから表示の木（property・value・children）を作る（RTOS のオブジェクト・ミドルウェアの状態等。止めずに読める。一貫させたいなら止めて）。読めない・式の誤りは notes。

## debug.elf.size

種類: `read`。ELF のメモリ使用量（Build Analyzer 相当）: 領域ごとの使用量・空き・使用率と内訳（区画ごと。.data の初期値のように読み込み番地が違う区画は Flash にも数える = as "load"）、読み込む区画の一覧（VMA・LMA・大きさ・領域）、どの領域にも入らない区画（outside）、大きいシンボル（section・region・kind = function / object・name（glob）で絞り、大きい順に top 個）。領域は regions（[{name, origin, length}]）> map_file（GNU ld の -Map の Memory Configuration）> linker_script（MEMORY）> 接続中のターゲットのメモリマップ（regions_source）。elf を省くと読み込んだ ELF。機器に触らない。

| 引数 | 型 | 必須 |
|---|---|---|
| `elf` | `string | null` |  |
| `linker_script` | `string | null` |  |
| `map_file` | `string | null` |  |
| `regions` | `Array<Record<string, unknown>> | null` |  |
| `top` | `number` |  |
| `section` | `string | null` |  |
| `region` | `string | null` |  |
| `kind` | `"function" | "object" | null` |  |
| `name` | `string | null` |  |

## debug.stack.static

種類: `read`。静的な最大のスタック（Static Stack Analyzer）: 関数ごとの枠（frame。コンパイラの .su（-fstack-usage）があればそれ、無ければ逆アセンブルの SP の動き = frame_source）と、呼び先を含む最大（max）・最深の経路（path）・関数の番地（address）・直接の呼び先（calls。呼び出しの木）。再帰（cycles）・関数ポインタの呼び出し・可変長の確保（alloca）・ELF に無い呼び先は bounded=false と理由（reasons。max は分かる分の下限、unknown_from は上限不明の元の関数）。根（roots: reset / exception = ベクタ表の処理、total は例外の枠を足したもの / uncalled / address_taken = 関数ポインタの呼び先の候補 / user）と MSP の見積もり（msp: スレッド + 例外 1 段 = one_level、全部が入れ子 = all_nested）。su_paths を省くと ELF のディレクトリの下の .su。indirect = {関数: [呼び先の候補]} で関数ポインタを解決、assume_address_taken で番地を取られた関数のどれかとみなす。exception_frame を省くと FPU の命令があれば 108、無ければ 36 バイト。機器に触らない。

| 引数 | 型 | 必須 |
|---|---|---|
| `su_paths` | `Array<string> | null` |  |
| `indirect` | `Record<string, unknown> | null` |  |
| `assume_address_taken` | `boolean` |  |
| `roots` | `Array<string> | null` |  |
| `entry` | `string | null` |  |
| `exception_frame` | `number | null` |  |
| `function` | `string | null` |  |
| `top` | `number` |  |

## target.stack.paint

種類: `control`。ベアメタルの MSP のスタックを fill（既定 0xCD）で塗る（止めて。リセット直後が最適。走らせた後に `target_stack_usage`）。範囲は ELF のリンカのシンボル（_estack・_Min_Stack_Size・__StackLimit 等、無ければベクタ表の初期 SP と最後の RAM の区画の終わり）から。nominal=false なら名目の大きさでなく上端の下の RAM の空き全部。bottom・top で直接。いまの MSP − margin より上は塗らない。RTOS のタスクのスタックは `rtos_tasks`。

| 引数 | 型 | 必須 |
|---|---|---|
| `fill` | `number` |  |
| `bottom` | `number | null` |  |
| `top` | `number | null` |  |
| `margin` | `number` |  |
| `nominal` | `boolean` |  |

## target.stack.usage

種類: `control`。MSP のスタックの使用量の実測（`target_stack_paint` の後）: 底から塗りが残るバイト数（untouched）から使った最大の深さ（used）・割合（fraction。threshold 以上で warning）、底まで使っていれば overflow、止まっていれば いまの深さ（current = 上端 − MSP）。走行中でも読める（止めない）。範囲・塗りの値は塗ったときのもの（bottom・top・fill で上書き）。

| 引数 | 型 | 必須 |
|---|---|---|
| `threshold` | `number` |  |
| `fill` | `number | null` |  |
| `bottom` | `number | null` |  |
| `top` | `number | null` |  |

## profile.aggregate

種類: `control`。プロファイル（取っている最中ならいままでの分）を時間の区間に限って集計する（IAR の Timeline で選んだ区間の Function Profiler）。t0_s / t1_s は最初のサンプルからの秒、方法 pcsr はタイムベース（記録の時間軸）の t0 / t1 も（itm はローカルのタイムスタンプが要る = profile_start の timestamps）。by: function / line / instruction / exception は `profile_result` と同じ表、file / directory / prefix（名前の型: HAL_GPIO_Init → HAL、ns::Cls::f → ns::Cls）/ groups はグループ（groups = {名前: [関数名の glob か "file:" + ファイルの glob]}、上から順に最初に合ったもの、残りは "(other)"）ごとのサンプル・割合（信頼区間）と多く使った関数。window に区間とサンプル数。

| 引数 | 型 | 必須 |
|---|---|---|
| `by` | `"function" | "line" | "instruction" | "exception" | "file" | "directory" | "prefix" | "groups"` |  |
| `groups` | `Record<string, unknown> | null` |  |
| `t0_s` | `number | null` |  |
| `t1_s` | `number | null` |  |
| `t0` | `number | null` |  |
| `t1` | `number | null` |  |
| `top` | `number` |  |
| `function` | `string | null` |  |

## profile.timeline

種類: `control`。CPU 使用率とグループの割合の推移（Core Benchmark）: bucket_s 秒ごとの load（眠り = WFI / WFE 以外の割合）・sleep と、全体で多い top 個のグループ（by・groups は `profile_aggregate` と同じ）の割合（残りは "(other)"）。t0_s / t1_s で範囲。サンプルの時刻が要る（pcsr、または itm の timestamps）。

| 引数 | 型 | 必須 |
|---|---|---|
| `bucket_s` | `number` |  |
| `by` | `"function" | "file" | "directory" | "prefix" | "groups"` |  |
| `groups` | `Record<string, unknown> | null` |  |
| `top` | `number` |  |
| `t0_s` | `number | null` |  |
| `t1_s` | `number | null` |  |

## profile.export

種類: `control`。プロファイルをほかのツールの形式で書き出す（t0_s / t1_s・t0 / t1 で区間）。gmon = gprof の gmon.out（OpenOCD の profile と同じ番地の度数。low / high で範囲、率は時刻からの実測。16 ビットを超える数は全体を割って率も割る）/ callgrind = KCacheGrind（関数・行ごとの自分のサンプル。呼び出しの関係は PC サンプリングでは分からない）/ lcov = 近似のカバレッジの .info（SF は source_root を前に付ける。回数 0 の行が実行されなかったとは限らない）/ gcov = .gcov のテキスト（path はディレクトリ。source_root の下にソースがあれば行の中身も）/ dot = Graphviz（関数の割合。static_calls なら逆アセンブルの静的な呼び出しを破線で、min_fraction 未満は省く）。

| 引数 | 型 | 必須 |
|---|---|---|
| `format` | `"gmon" | "callgrind" | "lcov" | "gcov" | "dot"` | ○ |
| `path` | `string` | ○ |
| `t0_s` | `number | null` |  |
| `t1_s` | `number | null` |  |
| `t0` | `number | null` |  |
| `t1` | `number | null` |  |
| `source_root` | `string | null` |  |
| `test_name` | `string` |  |
| `low` | `number | null` |  |
| `high` | `number | null` |  |
| `min_fraction` | `number` |  |
| `static_calls` | `boolean` |  |

## testpoint.aggregate

種類: `control`。テストポイントの関数の時間を区間（最初の印からの t0_s〜t1_s 秒。`testpoint_calls` と同じ）に限って集計: inclusive_s（子を含む。区間で切る。再帰は外側だけ）・exclusive_s（自分だけ）・started（区間で始まった回数）・active（区間に掛かった回数）。by が function 以外なら file / directory / prefix / groups のグループごとの exclusive_s と started。持っている最近の呼び出し（65536 件）だけ（dropped・kept_from_s）。

| 引数 | 型 | 必須 |
|---|---|---|
| `t0_s` | `number | null` |  |
| `t1_s` | `number | null` |  |
| `by` | `"function" | "file" | "directory" | "prefix" | "groups"` |  |
| `groups` | `Record<string, unknown> | null` |  |
| `top` | `number` |  |

## testpoint.export

種類: `control`。テストポイントの結果をほかのツールの形式で書き出す: callgrind = KCacheGrind（呼び出しの木。calls = 回数と含む時間、自分の時間。単位はタイムベースの刻み）/ lcov = .info（C0 の行 DA・関数 FN / FNDA・C1 の分岐 BRDA。判定まで来なかった組は "-"。SF は対応表の相対の道に source_root を前に付ける）/ gcov = .gcov のテキスト（path はディレクトリ。関数の呼ばれた・戻った回数と塊の割合、枝の回数）/ dot = Graphviz の呼び出しグラフ（含む・自分の割合と回数）/ gmon = gprof の呼び出しの組（arc）だけ（番地は読み込んだ ELF から。無い関数は missing）。

| 引数 | 型 | 必須 |
|---|---|---|
| `format` | `"callgrind" | "lcov" | "gcov" | "dot" | "gmon"` | ○ |
| `path` | `string` | ○ |
| `source_root` | `string | null` |  |
| `test_name` | `string` |  |
| `min_fraction` | `number` |  |

## 通知

- `activity`: 操作の記録（番号、時刻、利用者、メソッド、引数・結果の要約か誤り、所要時間 seconds、一時停止で保留された時間 held_s）
- `control`: 操作権の状態（paused、paused_by、保留の数 held）
- `target.state`: ターゲットの実行状態（target.status の結果。未接続は state = DISCONNECTED。致命的エラーで回り続けていれば fatal。Primer の USB が切れて開き直した後は、ターゲットにつなぎ直すまで link_lost = {at: UNIX 秒, changed: {機能: [前, 後]}}（changed が空でなければ Flash の古い版で立ち上がったかもしれない））。変化したときだけ送る
- `uart.data`: ターゲットの UART から受け取ったバイト
- `swo.data`: SWO から受け取ったバイト
- `clients`: つながっている利用者の一覧（id、name、role、via = api（WebSocket）/ gdb（GDB サーバの接続）/ tcp（流れの TCP の口の書き込み））。利用者が名乗った・去った・GDB がつながった・切れたとき
- `flash.progress`: Flash の操作の進捗（operation = program / erase / verify / read、phase = erase / program / verify / read、fraction 0..1、elapsed_s）。間引いて送る（段の始まりと終わりは必ず）
- `hook`: フックの実行（hooks.log の 1 件: seq・time・hook・name・event・phase・method・client・results・seconds）
- `elf.changed`: 読み込んだ ELF が更新された（path、time、reloaded、image_changed = 書き込む中身が変わった、target_image = current / stale / unknown、error、message）
- `ui.state`: 画面の状態が変わった（rev、origin = 変えた窓の印、changed = 変わった項目、state = 状態の全体。ui.state.get と同じ形。pointers = 窓・利用者ごとのマウスのカーソル。窓・利用者が切れたらその pointers は消える）
- `recording`: だれか（AI・別の窓も）が記録を開いた・始めた・止めた（action = open / start / stop、name = 記録の名前、client = 変えた利用者 {id、name、role}）。画面は時間軸と記録のビューを読み直す
