<!-- 自動生成ファイル。直接編集しないこと（python -m fprobe.api gen で再生成）。生成元: host/api/fprobe_api.json -->
# fprobe API

JSON-RPC 2.0 over WebSocket（`ws://127.0.0.1:7820/api`）。API の版 2。仕様: docs/superpowers/specs/2026-10-17-host-daemon-design.md

Python から: `c = fprobe.api.client.DaemonClient(url, 名前, 役割, token=…)` → `c.call("メソッド", 引数=値, …)`（引数はキーワードで。戻り値は dict）。

## session.hello

種類: `read`。名乗る（最初に必ず呼ぶ）。api_version が合わなければ断られる。 automated: 自動操作か（画面は navigator.webdriver を申告する。操作の記録に残す。省略で申告しない）。

| 引数 | 型 | 必須 |
|---|---|---|
| `name` | `string` | ○ |
| `role` | `"human" | "ai" | "script"` | ○ |
| `api_version` | `number` | ○ |
| `token` | `string | null` |  |
| `automated` | `boolean` |  |

応答:

```ts
{ api_version: number; boot: string; client_id: number; device: DeviceAddress; control: ControlState }
```

## session.status

種類: `read`。デーモンの状態（API の版、機器、利用者の一覧（WebSocket の利用者は、読まないので捨てた通知の数 notifications_dropped と送り待ちの上限 notify_limit も）、一時停止の状態、保留の数、見張りが最後に見たターゲットの状態、GDB サーバの待ち受けのポート gdb.port（開いていなければ null））。

応答:

```ts
{ api_version: number; boot: string; device: DeviceAddress; control: ControlState; clients: Array<SessionClient>; target: WatchedTargetState | null; gdb: { port: number | null } }
```

## session.subscribe

種類: `read`。受け取る通知の種類を設定する。

| 引数 | 型 | 必須 |
|---|---|---|
| `topics` | `Array<"activity" | "control" | "target.state" | "uart.data" | "swo.data" | "clients" | "hook" | "elf.changed" | "flash.progress" | "ui.state" | "recording">` | ○ |

応答:

```ts
{ topics: Array<string> }
```

## session.activity.list

種類: `read`。このデーモンの起動以降の操作の記録を、番号の小さい順に返す（before より前の、最後の limit 件）。

| 引数 | 型 | 必須 |
|---|---|---|
| `before` | `number` |  |
| `limit` | `number` |  |

応答:

```ts
{ entries: Array<ActivityEntry> }
```

## session.permissions

種類: `read`。このデーモンの許可の表（PC で実行してよいファイル exec・書いてよい場所 write）。利用者が起動の前に permissions.json に書き、デーモンが起動時に読む（API からは変えられない）。checked = デーモンがパスを確かめるか、file = 表のファイル、loaded = そのファイルがあって読んだか、created = この起動で初めての表として作ったか（表のファイルが無かった: <FPROBE_HOME>/out だけに書いてよく、実行は何も許さない表。既にある表は書き換えない）。表に無いパスを渡す呼び出しは E_NOT_ALLOWED（data.reason = path_not_permitted）。

応答:

```ts
{ checked: boolean; file: string | null; loaded: boolean; created: boolean; exec: Array<string>; write: Array<string> }
```

## session.control.pause

種類: `read`。一時停止（human だけ）。以後 human 以外の control の要求は保留される。

応答:

```ts
ControlState
```

## session.control.resume

種類: `read`。再開（human だけ）。保留していた要求を受けた順に処理する。

応答:

```ts
ControlState
```

## ui.state.get

種類: `read`。画面の状態（人の画面・別の窓・AI が共有する「いま見ているところ」）: view（時間軸の視点 source・t0・t1。単位は捕捉 = サンプル番号、記録 = タイムベース）、cursors（selection・mouse・reference の 3 種。同じ単位）、rows（開いている段の id: wave:<ch> = ロジアナ、ほかは記録の段の名前。表示の順。null = まだ知らない）、event（選んだ出来事 stream・t・t_end・kind・text・row）、frame（呼び出し履歴で選んだフレーム）、sampling（データ サンプリングの式の一覧 expressions = [{expr, plot, axis}] と周期 rate_hz。取っているかは watch.status）、dashboard（操作パネルの部品の配置 {version: 1, widgets: [{id, kind, expr, label, min, max, unit, digits, value, w, h}]}）、rev（変わるたびに増える）。 pointers は窓・利用者ごとのマウスのカーソル [{key, t, source, name, role, time}]。

応答:

```ts
UiState
```

## ui.state.set

種類: `read`。画面の状態を変える（渡したものだけ。人の画面がその視点・カーソル・段・出来事・フレームに移る）。cursors は渡したカーソルだけ（null で消す）。sampling はデータ サンプリングの式の一覧と周期（{expressions: [{expr, plot, axis}], rate_hz}。人の画面の一覧がそれになる）。dashboard は操作パネルの部品の配置（{version: 1, widgets: [{id, kind = gauge / number / bar / chart / button / slider / toggle / lamp, expr, label, min, max, unit, digits, value, w, h}]}。64 個・64 KiB まで。人の画面・遠隔の画面の操作パネルがそれになる。足りない・範囲の外の値は画面が直す）。system はシステム解析の MCU のメモリの表と FPGA の条件（{watches: [{name, address}], conditions: [{name, expr}]}。名前は重ならない。人の画面・遠隔の画面の表がそれになる）。clear に挙げた項目（view / cursors / rows / event / frame / sampling / dashboard / system）は既定に戻す。origin は変えた窓の印（通知 ui.state にそのまま載る）。結果は状態と changed（変わった項目）。AI が人に場所を見せる（例: 異常の区間に view と cursors を置く）のに使う。 cursors.mouse を渡すと、その窓・利用者のマウスのカーソル（pointers。名前・役割はデーモンが付ける）も変わる（人の画面がほかの窓・AI の指す時刻を時間軸に描く。null で消える）。 panel を渡すと、人の画面がそのパネルを開いて前に出す（View メニューと同じ）。

| 引数 | 型 | 必須 |
|---|---|---|
| `view` | `{ source?: "capture" | "recording"; t0: number; t1: number } | null` |  |
| `cursors` | `{ selection?: number | null; mouse?: number | null; reference?: number | null } | null` |  |
| `rows` | `Array<string> | null` |  |
| `event` | `{ stream: string; t: number; t_end?: number | null; kind?: string | null; text?: string | null; seq?: number | null; row?: string | null } | null` |  |
| `frame` | `number | null` |  |
| `clear` | `Array<"view" | "cursors" | "rows" | "event" | "frame" | "sampling" | "dashboard" | "system"> | null` |  |
| `origin` | `string | null`（MCP の道具には出さない） |  |
| `sampling` | `{ expressions: Array<{ expr: string; plot?: boolean; axis?: "left" | "right" }>; rate_hz?: number | null } | null` |  |
| `dashboard` | `{ version: 1; widgets: Array<{ id: string; kind: string; expr: string }> } | null` |  |
| `fpga_signals` | `Record<string, unknown> | null` |  |
| `system` | `Record<string, unknown> | null` |  |
| `panel` | `"source" | "disassembly" | "registers" | "watch" | "locals" | "callStack" | "breakpoints" | "memory" | "peripherals" | "threads" | "fault" | "timeline" | "terminal" | "log" | "linux" | "linuxHalt" | "activity" | "swoTrace" | "eventLog" | "recordings" | "system" | "dataSampling" | "power" | "cpuLoad" | "rtosObjects" | "rtosTiming" | "testpoints" | "heap" | "profile" | "interrupts" | "callGraph" | "logicAnalyzer" | "crossTrigger" | "verify" | "fpgaObs" | "dashboard" | "flash" | "lowLevel" | "api" | "favorites" | null` |  |

応答:

```ts
{ rev: number; view: UiView; cursors: UiCursors; rows: Array<string> | null; event: UiEvent; frame: number; sampling: UiSampling; dashboard: UiDashboard; fpga_signals: FpgaSignalDefs | null; system: UiSystem; pointers: Array<UiPointer>; changed: Array<"view" | "cursors" | "rows" | "event" | "frame" | "sampling" | "dashboard" | "fpga_signals" | "system" | "panel" | "pointers">; panel: { id: string; seq: number } | null }
```

## probe.info

種類: `read`。プローブのケイパビリティ・タイムベースの現在値・ターゲット接続の有無・個体（device = つなぎ方と番地、name = 個体名、version = ビットストリームの版: 入力の要約・ビルドの日時・git）。link_open = 開いたときの識別の READ の事実（identify_wait_s = 開く側が明示した待つ秒数、identify_resends = 時間切れで送り直した回数。測れない機器では null）。

応答:

```ts
{ capabilities: Capabilities; link_open: { identify_wait_s: number; identify_resends: number } | null; time: number; target_connected: boolean; device: DeviceAddress | null; name: string | null; version: string | null }
```

## probe.name.set

種類: `control`。今の機器（USB のシリアル番号・COM ポート・IP）に個体名を付ける（~/.fprobe/probes.json）。name を省くと名前を外す。以後 fprobe-daemon / fprobe-mcp を --name で起動でき、`probe_info` が名前を返す。

| 引数 | 型 | 必須 |
|---|---|---|
| `name` | `string | null` |  |

応答:

```ts
{ device: DeviceAddress; name: string | null }
```

## probe.sampler.status

種類: `read`。DAP サンプラ（変数を止めずに読む FPGA の回路。1 つだけ）をいま使っている道具（機器には触れない）: user（watch = 変数のライブ観測 / rtos = タスクの時間軸 / profile = PC のサンプリング / rtt = RTT の見張り / clock = CPU クロックの実測。空きなら null）、release（止める API。clock は自分で終わるので null）、since（使い始めた時刻、UNIX 秒）、client（使い始めた呼び出し元 {id, name, role}。デーモンが記録した事実。分からなければ null）。ほかの道具が使っている間に watch.set・rtos.timeline.start（sampled）・profile.start（pcsr）・rtt.start（sampler）を呼ぶと誤り。

応答:

```ts
{ user: "watch" | "rtos" | "profile" | "rtt" | "clock" | null; release: string | null; since: number | null; client: Caller | null }
```

## probe.design.get

種類: `read`。Primer の構成（ビットストリーム）: 今のデザイン・線（SWD / JTAG）と機能（サンプラ・故障注入・専用の入力・ロジアナの流し続けにイベントも載せる la_stream_events）の有無・切り替えられる構成（線・ビットストリームの場所と有無）・自動の書き替えの有無。`target_connect` は要る線の構成でなければ自動で書き替える（JTAG は jtag_chain か Cortex-A・ARM11 のターゲット）。

応答:

```ts
{ design: string | null; board: string | null; features: { swd: boolean; jtag: boolean; sampler: boolean; inject: boolean; la_inputs: number; la_stream_events: boolean }; switchable: boolean; auto_switch: boolean; designs: Array<{ design: string; protocols: Array<"swd" | "jtag">; for: Array<string>; bitstream: string; available: boolean; current: boolean }> }
```

## probe.design.switch

種類: `control`。Primer の構成を書き替える（ターゲット未接続のときだけ）。design（"fprobe" = 標準: SWD・サンプラ・故障注入、"fprobe_jtag" = JTAG と SWD、"fprobe_la" = SWD とロジアナの専用の入力 6 本（J14）・ロジアナの流し続けにイベントも載せる（サンプラ・故障注入なし））か protocol（"swd" / "jtag"）で指定する。今と同じなら何もしない。書き替えるとロジアナ・記録・流れの状態も消える。 書き替えると FPGA の出力の設定は消えるので、出力に使っていた端子は無効（駆動しない）で取り戻す（結果の outputs = {restored, failed}）。 ビットストリームを書くとタイムベースが 0 に戻るので、記録中なら書く前に記録を止める（meta の ended = {reason: design_switch, before, after}。結果の recording_stopped = 止めた記録の名前、記録していなければ null）。

| 引数 | 型 | 必須 |
|---|---|---|
| `design` | `"fprobe" | "fprobe_jtag" | "fprobe_la" | null` |  |
| `protocol` | `"swd" | "jtag" | null` |  |

応答:

```ts
{ switched: false; design: string } | { switched: true; before: string; after: string; bitstream: string; seconds: number; outputs: { restored: Array<number>; failed: { [key: string]: string } }; recording_stopped: string | null; release_failures: Array<{ step: string; error: string }> }
```

## probe.csr.read

種類: `read`。CSR を読む（バイトアドレス、32 ビット語 count 個）。レジスタ表は docs/registers.md。

| 引数 | 型 | 必須 |
|---|---|---|
| `address` | `number` | ○ |
| `count` | `number` |  |

応答:

```ts
Array<number>
```

## probe.csr.write

種類: `control`。CSR に 32 ビット語の列を書く。

| 引数 | 型 | 必須 |
|---|---|---|
| `address` | `number` | ○ |
| `words` | `Array<number>` | ○ |

応答:

```ts
null
```

## probe.voltage

種類: `read`。ターゲットの電源電圧（補助マイコン Pico の ADC、V）。live = window_s 秒（既定 10 ms。Pico の計測の間隔 1 ms より長く）待つ間に計測値の数が増えた（samples_before → samples）。
over_range が True なら測れる上限を超えている（volts は None。配線の分圧を確認）。

応答:

```ts
VoltageReading
```

## probe.voltage_history

種類: `read`。デーモンが読んだ電圧（だれの probe.voltage でも）の履歴（機器には触れない）: since（UNIX 秒）より後の読み readings = [{time（UNIX 秒）, volts, over_range, live}] と stats = {count, min, max, mean（live で範囲の中の読みだけ、V）, readings, over_range, not_live（それぞれの読みの数）}。残すのは limit 件まで（超えて捨てた古い読みは dropped）。

| 引数 | 型 | 必須 |
|---|---|---|
| `since` | `number | null` |  |

応答:

```ts
{ readings: Array<{ time: number; volts: number | null; over_range: boolean; live: boolean }>; stats: { count: number; min: number | null; max: number | null; mean: number | null; readings: number; over_range: number; not_live: number }; dropped: number; limit: number }
```

## probe.pins.read

種類: `control`。デバッグコネクタのピンの状態（swclk = SWCLK/TCK、swdio = SWDIO/TMS、nreset）。変えない。fprobe は TDI・TDO・nTRST を返さない（not_reported）。接続中も読める。

応答:

```ts
{ swclk: 0 | 1; swdio: 0 | 1; nreset: 0 | 1; not_reported: Array<string> }
```

## probe.pins.write

種類: `control`。ピンを個別に High（1）/ Low（0）にする（省いたピンは変えない）。ターゲット未接続のときだけ（接続中は断る）。hold_us を与えるとその時間の後で元に戻す（例 nreset=0, hold_us=1000 で 1 ms のリセットの脈）。書いた後の状態を返す。脈のときは、戻した線が戻るまで最大 settle_timeout_us 待ち、settle_us（戻るまで・打ち切るまでの時間）・settled（戻ったか。false = 上限で打ち切った）・settle_timeout_us（使った上限）も返す。

| 引数 | 型 | 必須 |
|---|---|---|
| `swclk` | `0 | 1 | null` |  |
| `swdio` | `0 | 1 | null` |  |
| `nreset` | `0 | 1 | null` |  |
| `hold_us` | `number` |  |
| `settle_timeout_us` | `number` |  |

応答:

```ts
{ swclk: 0 | 1; swdio: 0 | 1; nreset: 0 | 1; not_reported: Array<string>; settle_us?: number; settled?: boolean; settle_timeout_us?: number }
```

## target.connect

種類: `control`。ターゲットに接続する。既定は SWD。jtag_chain（JTAG チェーンの IR 長、TDO 側から。例: STM32 は [4, 5]）を
指定すると JTAG。connect_mode: halt / attach / under-reset。target="auto" なら先に部品を判別して pyOCD のターゲット名を選ぶ（結果の auto に判別の内容。JTAG だけのターゲットなら jtag_chain も補う）。
script: 接続・リセット等の手順を上書きする pyOCD のユーザスクリプト（"builtin:<名前>" かファイル。target_script_check で検査できる）。
core: コアごとに DP を持つ複数ドロップの部品（RP2040）のコアの番号。target="rp2040" と組む（pyOCD のターゲット rp2040_core0 / rp2040_core1 を選ぶ）。
RISC-V: target="rp2350_riscv" で RP2350 の Hazard3 に（pyOCD を使わず、APB-AP の向こうの Debug Module を直接）。core はハートの番号、connect_mode は halt / attach。使えるのは状態・停止・再開・ステップ・レジスタ（pc・x0〜x31・ABI の名前・主な CSR）・メモリ（システムバス）・ブレークポイント（トリガ）。リセット・Flash・SWO・RTOS 等は断る。
Cortex-A・ARM11（JTAG で直接。pyOCD を使わない）: target="bcm2836"（Raspberry Pi 2、Cortex-A7 × 4。core はコアの番号）/ "cortex_a"（ARMv7-A、コアは ROM テーブルから） / "zynq7000"（Zynq-7000、Cortex-A9 × 2。PS の DAP は independent の起動で PJTAG から）/ "bcm2835"（Pi Zero・1、ARM1176）/ "arm11"。チェーンの装置は IDCODE で探す（jtag_chain で IR 長の列を与えてもよい）。connect_mode は halt / attach。使えるのは状態・停止・再開・ステップ・レジスタ（r0〜r15・sp・lr・pc・cpsr）・メモリ（コアを通す。番地はコアの見る番地 = MMU が有効なら仮想番地。書いた範囲はキャッシュを揃える）・ハードのブレークポイント / ウォッチポイント。止めるのは選んだコアだけ。リセット・ベクタキャッチ・Flash・SWO・RTOS 等は断る。

| 引数 | 型 | 必須 |
|---|---|---|
| `target` | `string` |  |
| `frequency_hz` | `number` |  |
| `connect_mode` | `string` |  |
| `jtag_chain` | `Array<number> | null` |  |
| `script` | `string | null` |  |
| `core` | `number | null` |  |

応答:

```ts
{ state: CoreState; target?: string; frequency_hz?: number; wire?: "swd" | "jtag"; core?: number; arch?: string; hart?: number; debug_module?: DebugModuleInfo; architecture?: "armv7-a" | "armv6"; debug_version?: number; breakpoints?: number; watchpoints?: number; core_count?: number; debug_base?: number; apb_ap?: number; discovery?: "ahb_system_map" | "rom_table" | "part_table"; jtag_chain?: Array<number>; tap?: number; pc?: number; where?: string; function?: string; file?: string; line?: number; halt_reason?: "request" | "breakpoint" | "bkpt_instruction" | "watchpoint" | "vector_catch" | "external"; halt_bits?: Array<"HALTED" | "BKPT" | "DWTTRAP" | "VCATCH" | "EXTERNAL">; debug_mode?: "monitor"; fatal?: FatalState; rescue_dp?: number; cores?: Array<CoreEntry>; flash_breakpoints_recovered?: { restored: Array<number>; skipped: Array<number>; unreadable?: { journal: string; reason: string } }; script?: ScriptCheck; auto?: { part: Part | null; core: string | null; protocol: "swd" | "jtag"; frequency_hz: number; jtag_chain: Array<number> | null; target_source: "part" | "generic"; target: string }; design_switch?: DesignSwitch | Array<DesignSwitch> }
```

## target.disconnect

種類: `control`。ターゲットから切断する。

応答:

```ts
null
```

## target.status

種類: `read`。コアの状態（RUNNING / HALTED 等）と、停止中なら PC・場所（where、分かれば function・file・line）。Cortex-M の停止中は halt_reason（1 語: request / breakpoint / bkpt_instruction / watchpoint / vector_catch / external）と halt_bits（DFSR の立っていたビット全部: HALTED / BKPT / DWTTRAP / VCATCH / EXTERNAL。複数立っていても halt_reason は 1 つを選んだ語）。つないでいる先（target・frequency_hz・wire = swd / jtag）も。走っているのに次の事実があれば fatal = {kind（lockup = DHCSR.S_LOCKUP / pc_in = 読めた PC の見本がすべて `target.liveness.set` で使う側が渡した関数の中 / tick_stopped = ティックの変数が前の見張りから変わっていない）, function, since（最初に見た時刻、UNIX 秒）, evidence（手がかり: clue = lockup / pc_in・pc_range（function、valid = 読めた見本の数、pc_samples = 読んだ数）/ tick_stopped）}。事実だけで、何秒続いたら等の経験則の判定や、名前の表による「致命的エラーの関数」の判定はしない。材料は止めずに読む DHCSR・DWT_PCSR・ティックの変数。複数ドロップの部品（RP2040）なら core（接続中のコア）、部品の表に救済の DP があれば rescue_dp（その TARGETSEL。`target_reset` の kind=rescue が使える）、cores = [{core, selected, state, pc・where（止まっていれば）} または {core, selected, error}]。接続していないコアは TARGETSEL で DP を一時的に選び直して DHCSR を読む（DAP サンプラが動いている間は error）。RISC-V なら arch = "riscv"・hart・debug_module（版・プログラムバッファ・システムバス・ハート）。Cortex-A・ARM11 なら arch = "armv7-a" / "armv6"・core・cores・breakpoints・watchpoints・jtag_chain・tap（Cortex-A は debug_base・apb_ap・discovery = rom_table / part_table も）。

応答:

```ts
TargetStatus
```

## target.core.select

種類: `control`。接続するコアを替える（複数ドロップの部品（RP2040）。同じ速度で、既定は止めずに attach でつなぎ直す）。以後の道具（ブレークポイント・レジスタ・ステップ等）はそのコアに効く。target.status と同じものを返す。

| 引数 | 型 | 必須 |
|---|---|---|
| `core` | `number` | ○ |
| `connect_mode` | `string` |  |

応答:

```ts
{ state: CoreState; target?: string; frequency_hz?: number; wire?: "swd" | "jtag"; core?: number; arch?: string; hart?: number; debug_module?: DebugModuleInfo; architecture?: "armv7-a" | "armv6"; debug_version?: number; breakpoints?: number; watchpoints?: number; core_count?: number; debug_base?: number; apb_ap?: number; discovery?: "ahb_system_map" | "rom_table" | "part_table"; jtag_chain?: Array<number>; tap?: number; pc?: number; where?: string; function?: string; file?: string; line?: number; halt_reason?: "request" | "breakpoint" | "bkpt_instruction" | "watchpoint" | "vector_catch" | "external"; halt_bits?: Array<"HALTED" | "BKPT" | "DWTTRAP" | "VCATCH" | "EXTERNAL">; debug_mode?: "monitor"; fatal?: FatalState; rescue_dp?: number; cores?: Array<CoreEntry>; flash_breakpoints_recovered?: { restored: Array<number>; skipped: Array<number>; unreadable?: { journal: string; reason: string } }; script?: ScriptCheck; auto?: { part: Part | null; core: string | null; protocol: "swd" | "jtag"; frequency_hz: number; jtag_chain: Array<number> | null; target_source: "part" | "generic"; target: string }; design_switch?: DesignSwitch | Array<DesignSwitch> }
```

## target.fault

種類: `control`。止まったコアの故障の解析: 原因（CFSR・HFSR の各ビットの意味と手がかり。例 NOCP = FPU が無効）、故障の番地（MMFAR・BFAR）、故障した命令の場所（関数・ファイル:行）とその時のレジスタ。故障のハンドラで止まると debug.run_until_halt の結果にも要約が付く。locations は読んだ番地と大きさ（故障のレジスタは locations、積まれたレジスタは frame.locations。`target_memory_write` で書ける）、write_one_to_clear は 1 を書いたビットが消えるレジスタ（CFSR・HFSR・DFSR）。

応答:

```ts
FaultReport
```

## target.halt

種類: `control`。コアを停止する。core: 複数ドロップの部品（RP2040）で接続していないコアを止める（そのコアの状態を返す）。

| 引数 | 型 | 必須 |
|---|---|---|
| `core` | `number | null` |  |

応答:

```ts
TargetStatus | CoreEntry
```

## target.resume

種類: `control`。コアの実行を再開する。core: 複数ドロップの部品（RP2040）で接続していないコアを再開する（そのコアの状態を返す）。

| 引数 | 型 | 必須 |
|---|---|---|
| `core` | `number | null` |  |

応答:

```ts
TargetStatus | CoreEntry
```

## target.step

種類: `control`。1 命令ずつ count 回ステップ実行する。

| 引数 | 型 | 必須 |
|---|---|---|
| `count` | `number` |  |

応答:

```ts
{ state: CoreState; target?: string; frequency_hz?: number; wire?: "swd" | "jtag"; core?: number; arch?: string; hart?: number; debug_module?: DebugModuleInfo; architecture?: "armv7-a" | "armv6"; debug_version?: number; breakpoints?: number; watchpoints?: number; core_count?: number; debug_base?: number; apb_ap?: number; discovery?: "ahb_system_map" | "rom_table" | "part_table"; jtag_chain?: Array<number>; tap?: number; pc?: number; where?: string; function?: string; file?: string; line?: number; halt_reason?: "request" | "breakpoint" | "bkpt_instruction" | "watchpoint" | "vector_catch" | "external"; halt_bits?: Array<"HALTED" | "BKPT" | "DWTTRAP" | "VCATCH" | "EXTERNAL">; debug_mode?: "monitor"; fatal?: FatalState; rescue_dp?: number; cores?: Array<CoreEntry>; steps_done: number; linux?: LinuxTargetFacts }
```

## target.reset

種類: `control`。ターゲットをリセットする（halt=True ならリセット直後（リセットのベクタキャッチ）で停止）。kind: default（pyOCD の既定）/ system（AIRCR.SYSRESETREQ。周辺も）/ core（AIRCR.VECTRESET。コアだけ、ARMv7-M だけ）/ hardware（nRESET の線）/ emulated（デバッガがレジスタを初期値にする）/ rescue（RP2040 の救済の DP でチップ全体（USB 等の周辺も）をリセットし、同じ設定でつなぎ直して起動させる。ファームウェアが固まって普通のリセットで戻らないとき）。コアに無い種類は断る（黙って替えない）。

| 引数 | 型 | 必須 |
|---|---|---|
| `halt` | `boolean` |  |
| `kind` | `"default" | "system" | "core" | "hardware" | "emulated" | "rescue"` |  |

応答:

```ts
{ state: CoreState; target?: string; frequency_hz?: number; wire?: "swd" | "jtag"; core?: number; arch?: string; hart?: number; debug_module?: DebugModuleInfo; architecture?: "armv7-a" | "armv6"; debug_version?: number; breakpoints?: number; watchpoints?: number; core_count?: number; debug_base?: number; apb_ap?: number; discovery?: "ahb_system_map" | "rom_table" | "part_table"; jtag_chain?: Array<number>; tap?: number; pc?: number; where?: string; function?: string; file?: string; line?: number; halt_reason?: "request" | "breakpoint" | "bkpt_instruction" | "watchpoint" | "vector_catch" | "external"; halt_bits?: Array<"HALTED" | "BKPT" | "DWTTRAP" | "VCATCH" | "EXTERNAL">; debug_mode?: "monitor"; fatal?: FatalState; rescue_dp?: number; cores?: Array<CoreEntry>; kind: "default" | "system" | "core" | "hardware" | "emulated" | "rescue" }
```

## target.vector_catch

種類: `control`。ベクタキャッチ（例外の入口でコアを止める。DEMCR の VC_*）を catch の集合にする（"all" ですべて、[] で無し）。名前: core_reset / mem_manage / no_coprocessor / check_error / state_error / bus_fault / interrupt_error / hard_fault / secure_fault。結果の enabled はコアから読み直したもの、コアに無いもの（ARMv6-M は hard_fault・core_reset だけ）は unsupported。読むだけなら target.vector_catch.get。止まると debug.run_until_halt・debug.step の reason が vector_catch になり、故障なら target.fault の要約が付く。

| 引数 | 型 | 必須 |
|---|---|---|
| `catch` | `Array<"core_reset" | "mem_manage" | "no_coprocessor" | "check_error" | "state_error" | "bus_fault" | "interrupt_error" | "hard_fault" | "secure_fault" | "all">` | ○ |

応答:

```ts
{ enabled: Array<string>; available: Array<string>; unsupported?: Array<string> }
```

## target.vector_catch.get

種類: `control`。いまのベクタキャッチ（DEMCR の VC_*）を読む: enabled（入っているもの）・available（名前の一覧）。状態を変えない。

応答:

```ts
{ enabled: Array<string>; available: Array<string> }
```

## target.debug_mode.get

種類: `control`。いまのデバッグの方式（U37。J-Link の Monitor Mode Debugging 相当）。mode: halt（停止方式。ブレークポイント・ステップでコア全体を止める）/ monitor（モニタ方式。デバッグ用の例外 DebugMon の中で止まり、DebugMon より優先度の高い割り込みは止まっている間も動き続ける）。available: ファームにモニタ（host/firmware/monitor）があるか（無ければ reason）。あれば box（箱の番地）・priority（DebugMon の優先度。数が小さいほど高い）・prio_bits（実装された優先度のビット）・keeps_running（止まっている間も動く例外・割り込み: exception・name・priority）・fpu・stack_size・stack_overflow（モニタのスタックが溢れた）、モニタ方式なら state（stopped / running）・stops（止まった回数）も。

応答:

```ts
DebugMode
```

## target.debug_mode.set

種類: `control`。デバッグの方式を切り替える（U37）。mode: halt / monitor。止まっていれば同じ PC で止まったまま切り替わる。monitor ではターゲットのファームに組み込んだモニタ（host/firmware/monitor の DebugMon_Handler と fprobe_monitor_init）を通して、target.halt・resume・step、debug.breakpoint.*、debug.step、レジスタ、呼び出し履歴、debug.call、GDB サーバがそのまま使える。止まっている間も DebugMon より優先度の高い割り込みは動く。モニタ方式でできないこと: ベクタキャッチ、止めてのリセット（target.reset の halt・target.program_ram）、Flash の書き込み・消去、Flash の BKPT のブレークポイント（比較器だけ）、DebugMon 以上の優先度で走る処理の中のブレークポイント（HardFault になる）。クロストリガの停止の書き込みは DEMCR.MON_PEND に替わる。priority: DebugMon の優先度（0〜255、実装のビットに丸める）。結果は target.debug_mode.get と同じ。 monitor → halt では left = {stop: breakpoint_at_pc（同じ PC で止めた）/ halt_request（PC に置けない・戻らないので停止要求で止めた。PC は近くだが同じとは限らない）/ running, pc_before, pc_after}。

| 引数 | 型 | 必須 |
|---|---|---|
| `mode` | `"halt" | "monitor"` | ○ |
| `priority` | `number | null` |  |

応答:

```ts
DebugMode
```

## target.liveness.get

種類: `read`。走らせたままの見張り（`target.status` の fatal）の条件: functions・patterns（PC の見本がその中にあれば fatal.kind = pc_in にする関数の名前・正規表現。既定は空）、pc_samples（見張りの 1 回に読む DWT_PCSR の数。条件が無ければ null = PC を読まない）、tick_variable（ティックの変数の名前。null = ティックを読まない）。

応答:

```ts
{ functions: Array<string>; patterns: Array<string>; pc_samples: number | null; tick_variable: string | null }
```

## target.liveness.set

種類: `control`。走らせたままの見張りの条件を置き換える: functions（関数の名前。完全一致）・patterns（正規表現。re.search）= 読めた PC の見本がすべてその関数の中なら `target.status` の fatal を kind = pc_in にする（渡さなければ空。本体は「致命的エラーの関数」「故障の処理」の名前の表を持たない。慣習の表はプラグイン fprobe.plugins.heuristics の liveness_conventions）、tick_variable（ティックの変数の名前。前の見張りから値が変わっていなければ fatal を kind = tick_stopped にする。本体は xTickCount・uwTick 等の名前の慣習で探さない: 渡さなければティックは読まない。ELF に無い名前は誤り）、pc_samples（見張りの 1 回に読む DWT_PCSR の数。1 以上。functions・patterns・tick_variable を渡すときは必須で既定は無い。使った数は fatal の evidence の pc_samples に出る）。結果は `target.liveness.get` と同じ。

| 引数 | 型 | 必須 |
|---|---|---|
| `functions` | `Array<string>` |  |
| `patterns` | `Array<string>` |  |
| `pc_samples` | `number | null` |  |
| `tick_variable` | `string | null` |  |

応答:

```ts
{ functions: Array<string>; patterns: Array<string>; pc_samples: number | null; tick_variable: string | null }
```

## target.registers.read

種類: `control`。コアレジスタを読む（省略時 r0..r12, sp, lr, pc, xpsr）。（RISC-V は pc と x1〜x31 の ABI の名前、Cortex-A・ARM11 は xpsr の代わりに cpsr）

| 引数 | 型 | 必須 |
|---|---|---|
| `names` | `Array<string> | null` |  |

応答:

```ts
{ [key: string]: number }
```

## target.registers.write

種類: `control`。コアレジスタに書く（停止中）。

| 引数 | 型 | 必須 |
|---|---|---|
| `name` | `string` | ○ |
| `value` | `number` | ○ |

応答:

```ts
null
```

## target.memory.read

種類: `control`。ターゲットのメモリを読み、16 進文字列（アドレス順のバイト）で返す。

| 引数 | 型 | 必須 |
|---|---|---|
| `address` | `number` | ○ |
| `length` | `number` | ○ |

応答:

```ts
string
```

## target.memory.write

種類: `control`。ターゲットのメモリ（RAM・周辺）に 16 進文字列のバイト列を書く。Flash は `target_flash` を使う。

| 引数 | 型 | 必須 |
|---|---|---|
| `address` | `number` | ○ |
| `data_hex` | `string` | ○ |

応答:

```ts
null
```

## target.dp.read

種類: `control`。ターゲットの DP のレジスタを直接読む。register は名前（DPIDR / CTRL/STAT / SELECT / RDBUFF / TARGETID / DLPIDR / EVENTSTAT / BASEPTR0）か番地（bit 7:4 が DPBANKSEL。例 0x24 = TARGETID）。接続中は pyOCD を通し、未接続なら DP だけを起こして読む（target_diagnose の後はその線と速度、ほかは SWD 1 MHz）。path に経路。

| 引数 | 型 | 必須 |
|---|---|---|
| `register` | `number | string` | ○ |

応答:

```ts
{ register: number; value: number; path: string }
```

## target.dp.write

種類: `control`。ターゲットの DP のレジスタに書く（例 ABORT = 0x0 に 0x1E で誤りの印を消す、CTRL/STAT で電源を求める）。経路は target_dp_read と同じ。

| 引数 | 型 | 必須 |
|---|---|---|
| `register` | `number | string` | ○ |
| `value` | `number` | ○ |

応答:

```ts
{ register: number; value: number; path: string }
```

## target.ap.read

種類: `control`。ターゲットの AP のレジスタを直接読む。ap は ADIv5 なら APSEL（0〜255）、ADIv6 なら AP の番地。register は AP の中の番地（ADIv5 の MEM-AP: 0x00 CSW / 0x04 TAR / 0x0C DRW / 0xF8 BASE / 0xFC IDR。ADIv6 は 0xD00 足す）。経路は target_dp_read と同じ。

| 引数 | 型 | 必須 |
|---|---|---|
| `ap` | `number` | ○ |
| `register` | `number` | ○ |

応答:

```ts
{ ap: number; register: number; value: number; path: string }
```

## target.ap.write

種類: `control`。ターゲットの AP のレジスタに書く（ap・register は target_ap_read と同じ）。

| 引数 | 型 | 必須 |
|---|---|---|
| `ap` | `number` | ○ |
| `register` | `number` | ○ |
| `value` | `number` | ○ |

応答:

```ts
{ ap: number; register: number; value: number; path: string }
```

## target.speed_test

種類: `control`。ターゲットのメモリの読みの速度（write なら書きも）を測る（接続中）。書きは RAM の番地でコアを止めてから: 試験の模様を書いて読み比べ（verified）、元の中身を書き戻す（restored）。bytes_per_s を返す。

| 引数 | 型 | 必須 |
|---|---|---|
| `address` | `number` | ○ |
| `length` | `number` |  |
| `write` | `boolean` |  |

応答:

```ts
{ address: number; bytes: number; read: { seconds: number; bytes_per_s: number | null }; frequency_hz: number | null; write?: { seconds: number; bytes_per_s: number | null; verified: boolean; restored: boolean } }
```

## target.diagnose

種類: `control`。ターゲットにつながらない理由を順に調べ、段ごとに読めた事実を返す（未接続のときだけ）: 電圧 → ピン → SWD（複数ドロップ・JTAG→SWD・休眠からの起こし。どの ACK か）→ JTAG のチェーン → DP（デバッグ電源）→ AP・ROM テーブル → コア（止まっている・眠っている・ロックアップ）→ 部品の判別（pyOCD のターゲット名の候補）→ 保護（STM32 の RDP・nRF の APPROTECT）。frequencies_hz = 試す速さ（この順に試し、最初に応答した速さで続ける。暗黙に下げない）。返すもの: reached（DP に届きデバッグ電源の要求に応答があった）・frequencies_hz（試した速さ）・protocol・frequency_hz（応答した速さ）・dp・aps・core・part・jtag・steps（name・status = ok 読めた / fail 読めなかった / skip 測る手段が無い・message・data）。良し悪しの判定（warn）・結論の文・接続の引数のおすすめは本体ではしない（プラグイン fprobe.plugins.heuristics.diagnose_verdict）。

| 引数 | 型 | 必須 |
|---|---|---|
| `frequencies_hz` | `Array<number>` |  |
| `max_aps` | `number` |  |
| `jtag` | `boolean` |  |

応答:

```ts
Diagnosis
```

## target.script.check

種類: `read`。接続の手順のスクリプト（pyOCD のユーザスクリプト。"builtin:<名前>" かファイル）を実行せずに調べる: 定義された手順（will_connect / did_init_target / will_reset / unlock_device 等）と行、pyOCD の手順の名前でない関数（functions。pyOCD は呼ばない。名前・行）、引数の誤り（pyOCD が渡さない引数の名前。problems）、構文の誤り。似た名前の手順の提案は本体ではしない（プラグイン fprobe.plugins.heuristics.connect_script_name_hints）。target_connect の script に渡す前に。

| 引数 | 型 | 必須 |
|---|---|---|
| `script` | `string` | ○ |

応答:

```ts
ScriptCheck
```

## target.script.list

種類: `read`。内蔵の接続の手順（target_connect の script に "builtin:<名前>"。例 builtin:stm32_debug_low_power = 低消費電力の間もデバッグ、builtin:reset_with_nreset = リセットを nRESET の線で）と、pyOCD の手順の名前・引数の一覧。

応答:

```ts
{ builtin: Array<{ name: string; summary: string; hooks: Array<string> }>; hooks: Array<{ name: string; params: Array<string>; summary: string }> }
```

## target.option_bytes.read

種類: `control`。STM32 のオプションバイトを名前で読む（F4・L4・G4・G0。RDP と rdp_level、BOR、ウォッチドッグ、リセット、ブート、F4 は nWRP）。部品は DBGMCU の DEV_ID で判別する。変えない。

応答:

```ts
{ part: Part; family: "f4" | "l4" | "g4" | "g0"; address: number; raw: number; fields: { [key: string]: number }; rdp_level: 0 | 1 | 2; descriptions: { [key: string]: string } }
```

## target.option_bytes.write

種類: `control`。STM32 のオプションバイトを名前で書く（fields 例 {"nWRP": 255, "IWDG_SW": 1}。書かないビットは保つ）。RDP を Level 1 → 0 に戻す（Flash の全消去）には confirm_mass_erase、Level 0 → 1 には confirm_protect が要る（無ければ何もせず断る）。Level 2 は書かない。コアを止めてから書く。reset: F4 は書いた後でリセットして止める、L4 / G4 / G0 は OBL_LAUNCH で反映して切断する（reconnect_required）。

| 引数 | 型 | 必須 |
|---|---|---|
| `fields` | `{ [key: string]: number }` | ○ |
| `confirm_mass_erase` | `boolean` |  |
| `confirm_protect` | `boolean` |  |
| `reset` | `boolean` |  |
| `timeout_s` | `number` |  |

応答:

```ts
{ part: Part; before: OptionBytes; notes: Array<string>; log: Array<string>; reconnect_required: boolean; planned?: OptionBytes; after?: OptionBytes }
```

## target.unlock

種類: `control`。保護を全消去で解除する（confirm_mass_erase が無ければ何もせず断る）。接続中: STM32 の RDP を Level 0 に（target_option_bytes_write と同じ手順）。未接続: nRF52 の CTRL-AP の ERASEALL（APPROTECT の解除）。既に解除されていれば何もしない（already_unlocked）。

| 引数 | 型 | 必須 |
|---|---|---|
| `confirm_mass_erase` | `boolean` |  |
| `reset` | `boolean` |  |
| `timeout_s` | `number` |  |

応答:

```ts
{ kind: "stm32_rdp"; already_unlocked: true; part: Part; option_bytes: OptionBytes } | { kind: "stm32_rdp"; already_unlocked: false; part: Part; before: OptionBytes; notes: Array<string>; log: Array<string>; reconnect_required: boolean; planned?: OptionBytes; after?: OptionBytes } | { kind: "nrf_ctrl_ap"; already_unlocked: true; ap: number } | { kind: "nrf_ctrl_ap"; already_unlocked: false; ap: number; erased: true; unlocked: boolean; notes: Array<string> }
```

## jtag.scan

種類: `control`。JTAG のチェーンを走査する（未接続のときだけ）: 装置の数・IDCODE（設計者・名前）・IR 長（既知の表か Capture-IR から。ir_length_source = table / measured / ambiguous。ambiguous は境目が一つに決まらず推測しない: ir_candidates に候補）。jtag_chain は target_connect にそのまま渡せる（全部決まったときだけ）。TDO の張り付きも分かる。

| 引数 | 型 | 必須 |
|---|---|---|
| `frequency_hz` | `number` |  |
| `max_devices` | `number` |  |

応答:

```ts
JtagChainScan
```

## jtag.reset

種類: `control`。すべての TAP を Test-Logic-Reset に（TMS = 1 を 6 回）して Run-Test/Idle へ（未接続のときだけ）。

応答:

```ts
{ state: "run-test/idle" }
```

## jtag.shift

種類: `control`。チェーンの device 番目に IR を入れ（ほかの装置は BYPASS）、DR を dr_length ビット シフトする（未接続のときだけ）。chain は IR 長の列（TDO 側から。省略時は最後の jtag_scan）。ir 省略時は IR を変えない。取り出した ir_capture・dr_capture を返す。

| 引数 | 型 | 必須 |
|---|---|---|
| `device` | `number` |  |
| `ir` | `number | null` |  |
| `dr_value` | `number` |  |
| `dr_length` | `number` |  |
| `chain` | `Array<number> | null` |  |

応答:

```ts
{ device: number; chain: Array<number>; ir_capture?: number; dr_capture?: number }
```

## jtag.clock

種類: `control`。生のビット列を TCK ごとに出す（未接続のときだけ）。tms・tdi は時間順の "0" / "1" の文字列（tdi 省略時は 0）。TDO を同じ形で返す。TAP の状態は呼び手が管理する（ほかの JTAG の道具は Run-Test/Idle を前提にする）。

| 引数 | 型 | 必須 |
|---|---|---|
| `tms` | `string` | ○ |
| `tdi` | `string | null` |  |

応答:

```ts
{ tdo: string; clocks: number }
```

## target.svd.load

種類: `control`。周辺レジスタの定義（CMSIS-SVD）を読む。path（ファイル）か device（SVD のファイル名。拡張子を除いて同じ名前のファイルを同梱・~/.fprobe/svd・FPROBE_SVD_PATH・pyOCD の内蔵から探す。大文字小文字は区別しない）のどちらかが要る。ターゲット名やファイル名の前方一致から推して選ぶことはしない（周辺の道具は読み込んでいなければ誤り）。

| 引数 | 型 | 必須 |
|---|---|---|
| `device` | `string | null` |  |
| `path` | `string | null` |  |
| `from_target` | `boolean` |  |

応答:

```ts
{ name: string; source: string; origin: string; peripherals: number }
```

## target.svd.peripherals

種類: `read`。SVD の周辺の一覧（名前・番地・グループ・説明・レジスタの数）。filter は名前・グループの一部。

| 引数 | 型 | 必須 |
|---|---|---|
| `filter` | `string | null` |  |

応答:

```ts
{ device: string; peripherals: Array<{ name: string; base_address: number; group: string | null; description: string; registers: number }> }
```

## target.svd.registers

種類: `read`。周辺のレジスタの定義（番地・幅・アクセス・リセット値・読むと変わるか・フィールド・列挙）。

| 引数 | 型 | 必須 |
|---|---|---|
| `peripheral` | `string` | ○ |

応答:

```ts
{ peripheral: string; base_address: number; registers: Array<{ path: string; name: string; address: number; size: number; access: string; reset_value: number; description: string; read_side_effects: boolean; fields: Array<{ name: string; bit_offset: number; bit_width: number; access: string; description: string; enums: Array<{ name: string; value: number | null }> }> }> }
```

## target.peripheral.read

種類: `control`。周辺レジスタを読んでフィールドに解読する（レジスタごとに path・address・size = SVD の大きさのビット・value と、フィールドの名前・ビット・値・列挙の名前）。path は RCC.CR（1 つ）か GPIOA（周辺の全部）。読むと状態が変わるもの（SVD の readAction）は allow_side_effects が無ければ読まず skipped に入れる。readAction の無いレジスタ（ST の SVD 等）はレジスタの名前から推さずに読む: 周辺の全部を読むとき、ほかに読まないもの（データ・FIFO の口等）は skip（"周辺.レジスタ" の glob。大文字小文字を区別しない。例 ["*.DR"]）で渡す（skipped の reason は "skip <パターン>"）。

| 引数 | 型 | 必須 |
|---|---|---|
| `path` | `string` | ○ |
| `allow_side_effects` | `boolean` |  |
| `skip` | `Array<string> | null` |  |

応答:

```ts
{ registers: Array<SvdRegisterValue>; skipped: Array<{ path: string; reason: string }> }
```

## target.peripheral.write

種類: `control`。周辺レジスタに書く。fields（例 {"HSEON": 1, "MODER5": "Output"}。読み・変更・書き込み）か value（全体）。書く前と後の解読した値を返す。

| 引数 | 型 | 必須 |
|---|---|---|
| `path` | `string` | ○ |
| `fields` | `Record<string, unknown> | null` |  |
| `value` | `number | null` |  |
| `allow_side_effects` | `boolean` |  |

応答:

```ts
{ path: string; written: number; before: SvdRegisterValue | null; after: SvdRegisterValue | null }
```

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

応答:

```ts
{ memory: "target" | "snapshot" | "elf"; address: number; length: number; unit: number; rows: Array<{ address: number; values: Array<number>; text: string; changed: Array<boolean> }>; dump: string; names: Array<{ address: number; name: string; kind: "symbol" | "register" }>; compared?: boolean; changes?: Array<{ address: number; before: string; after: string }> }
```

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

応答:

```ts
{ state: CoreState; target?: string; frequency_hz?: number; wire?: "swd" | "jtag"; core?: number; arch?: string; hart?: number; debug_module?: DebugModuleInfo; architecture?: "armv7-a" | "armv6"; debug_version?: number; breakpoints?: number; watchpoints?: number; core_count?: number; debug_base?: number; apb_ap?: number; discovery?: "ahb_system_map" | "rom_table" | "part_table"; jtag_chain?: Array<number>; tap?: number; pc?: number; where?: string; function?: string; file?: string; line?: number; halt_reason?: "request" | "breakpoint" | "bkpt_instruction" | "watchpoint" | "vector_catch" | "external"; halt_bits?: Array<"HALTED" | "BKPT" | "DWTTRAP" | "VCATCH" | "EXTERNAL">; debug_mode?: "monitor"; fatal?: FatalState; rescue_dp?: number; cores?: Array<CoreEntry>; image: FlashImage; program: ProgramReport; verify: FlashComparison | null; seconds: number }
```

## target.program_ram

種類: `control`。ELF を RAM に読み込み、起動できる状態で止める（Flash には書かない。RAM で動く試験用のプログラム向け）。
vector_table（ベクタ表の番地。使う側が明示する）を渡せばその表の初期の MSP・リセットの処理から（VTOR も設定）、
渡さなければ ELF の入口（e_entry。リンカが書いた事実）から（SP・VTOR は変えない。ベクタ表の場所を推定しない）。
ターゲットはリセットされる。続けて `debug_run_until_halt` / `target_resume` で走らせる。結果の start = {from:
vector_table / entry, entry, vector_table}。

| 引数 | 型 | 必須 |
|---|---|---|
| `path` | `string` | ○ |
| `vector_table` | `number | null` |  |

応答:

```ts
{ reason: string; pc: number; where: string; function?: string; file?: string; line?: number; elapsed_s: number; time_source: "host"; halt_bits?: Array<string>; start: { from: "entry" | "vector_table"; entry: number; vector_table: number | null } }
```

## debug.elf.load

種類: `control`。ELF のシンボルと行番号を読み込む（以後、場所を関数名・変数名・"ファイル:行" で指定でき、止まった場所が
関数名と行で返る）。`target_flash` で ELF を書いたときは自動で読む。

| 引数 | 型 | 必須 |
|---|---|---|
| `path` | `string` | ○ |

応答:

```ts
ElfLoaded
```

## debug.elf.load_demo

種類: `control`。同梱のデモの ELF（host/firmware/sram_demo/sram_demo.elf。RAM で動く STM32 の小さなプログラム）を読む（画面の Load Demo。機器・ELF が無くても各パネルの働きを見る）。結果は debug.elf.load と同じ。

応答:

```ts
ElfLoaded
```

## debug.elf.upload

種類: `control`。画面で選んだ ELF の中身（data_base64）をデーモンの PC の FPROBE_HOME/uploads/<name> に置いて読む（ブラウザはファイルの場所を渡せないため。遠隔の画面でも使える）。64 MB まで。結果は debug.elf.load と同じ。

| 引数 | 型 | 必須 |
|---|---|---|
| `name` | `string` | ○ |
| `data_base64` | `string` | ○ |

応答:

```ts
ElfLoaded
```

## debug.symbol.info

種類: `read`。場所（関数名・変数名・"ファイル:行"・"0x..."）の番地と、その番地の関数名・ファイル:行（where の文字と、欄 function・offset・file・line。分からない欄は null）。

| 引数 | 型 | 必須 |
|---|---|---|
| `location` | `string` | ○ |

応答:

```ts
{ address: number; where: string; function: string | null; offset: number | null; file: string | null; line: number | null; kind?: "function" | "object"; size?: number }
```

## debug.source.list

種類: `read`。読み込んだ ELF の行番号の情報が参照するソース（name・path・この PC にあるか exists）。同じ名前の別のファイルは files には最初のものだけで、same_name = {名前: [場所]} に全部（"ファイル:行" は名前だけでは選ばず、ディレクトリを添えて選ぶ。例 "drivers/util.c:10"）。 ビルドした場所に無く ELF の隣の同じ名前のファイルを使ったものは built_path（行表の場所）も（同じ名前なら同じソースとみなす。中身は確かめていない）。

応答:

```ts
SourceList
```

## debug.source.read

種類: `read`。ソースの本文（text）と命令のある行（lines、ブレークポイントを置ける行）。name は `debug_source_list` の名前。
ELF が参照するファイルだけ読める。

| 引数 | 型 | 必須 |
|---|---|---|
| `name` | `string` | ○ |

応答:

```ts
{ name: string; path: string; text: string; lines: Array<number>; fetched?: LinuxSourceFetched }
```

## debug.breakpoint.set

種類: `control`。ブレークポイントを置く。location: 関数名・"ファイル:行"・"0x..."。condition: C の式（偽なら止まらない。ローカル変数・比較・論理演算も使える）、ignore_count: 最初の n 回は止まらない、log: 当たるたびに読む式の一覧（[] なら式なしで message だけ。止まらずに debug.breakpoint.log と記録の stream "debug" に残す。message は添える文）。動作のあるブレークポイントは当たるたびにコアを数 ms 止める（速い頻度の観測は watch・RTT・テストポイントで）。

| 引数 | 型 | 必須 |
|---|---|---|
| `location` | `string` | ○ |
| `condition` | `string | null` |  |
| `ignore_count` | `number` |  |
| `log` | `Array<string> | null` |  |
| `message` | `string | null` |  |

応答:

```ts
{ address: number; where: string; function?: string; file?: string; line?: number }
```

## debug.breakpoint.clear

種類: `control`。ブレークポイントを外す（location 省略ですべて。ウォッチポイントも含めて外す）。

| 引数 | 型 | 必須 |
|---|---|---|
| `location` | `string | null` |  |

応答:

```ts
BreakpointList
```

## debug.breakpoint.enable

種類: `control`。ブレークポイントを有効・無効にする（location は `debug_breakpoint_set` と同じ）。無効にすると機器から外し（比較器・Flash の BKPT を空ける）、場所と動作（condition・ignore_count・log・message・hits）は覚えておく。有効に戻すと同じ場所に置き直す。置き直し（`debug_breakpoint_set`）でも有効になる。返すのは `debug_breakpoint_list` と同じ。

| 引数 | 型 | 必須 |
|---|---|---|
| `location` | `string` | ○ |
| `enabled` | `boolean` | ○ |

応答:

```ts
BreakpointList
```

## debug.breakpoint.list

種類: `read`。置いているブレークポイントとウォッチポイント。各ブレークポイントは address・location・enabled（false = `debug_breakpoint_enable` で無効にしたもの。機器には置いていないので kind は無い）・場所（where・function・file・line）・kind（hw / flash / sw。接続中）、動作があれば action（condition・ignore_count・log・message・hits・last_error）、道具が置いたものは tool。

応答:

```ts
BreakpointList
```

## debug.breakpoint.log

種類: `read`。ログ・ブレークポイントの記録（当たった場所・回数・式の値）。seq が since より後のものを古い順に limit 件。next を次の since に渡す。記録中に当たった項は記録のタイムベースの時刻 t を持つ（時間軸のその時刻へ飛べる）。 more = limit で切って続きがある、lost = since より後で、持つ数の上限（kept_limit 件）を超えて捨てた件数。last = いちばん新しい seq（since が last より後なら読み手の位置はこのログに無い: 0 から読み直す）。 values の式の値は `debug_evaluate` と同じ形（kind を持つ object。評価できない式は kind = unavailable と reason）。 host_s は PC が止まりを処理した PC の時計の秒（time_source = host_received: 対象が止まったのはそれより前）。

| 引数 | 型 | 必須 |
|---|---|---|
| `since` | `number` |  |
| `limit` | `number` |  |

応答:

```ts
{ entries: Array<{ seq: number; host_s: number; time_source: "host_received"; address: number; where: string; function?: string; file?: string; line?: number; hit: number; message: string | null; values: { [key: string]: Value }; t?: number }>; next: number; more: boolean; lost: number; kept_limit: number; last: number }
```

## debug.breakpoint.flash

種類: `control`。Flash 上の無制限ブレークポイント（J-Link の Unlimited Flash Breakpoints 相当）の様子と設定。ハードの比較器が尽きたら、Flash の命令を BKPT に書き換えてブレークポイントにする（debug.breakpoint.set・GDB の Z0・行単位のステップがそのまま使う）。書くのは再開の直前だけ、セクタごとにまとめて 1 回。外したものは同じセクタを書くついでに戻し（それまでは当たっても黙って踏み越える）、ブレークポイントの上からの再開・ステップは元の命令を Flash の外で実行する（書き換えない）。切断・終了で元に戻し、異常終了で残ったものは次の接続で戻す（target.connect の flash_breakpoints_recovered）。enabled: 使うか（既定 true）。dormant_limit: 外した BKPT に何回当たったら戻すか。scratch: 元の命令を実行する RAM の番地（8 バイト、止めている間だけ借りる。省略時は Flash の書き込みアルゴリズムの RAM の先頭）。返す: hardware（比較器の番地）、flash（番地・state written / pending・場所）、dormant（外したが Flash に残る BKPT）、stats（commits・sector_writes・emulated・displaced・rewrites・dormant_hits・restored）、journal（書き換えの記録のファイル）。

| 引数 | 型 | 必須 |
|---|---|---|
| `enabled` | `boolean | null` |  |
| `dormant_limit` | `number | null` |  |
| `scratch` | `number | null` |  |

応答:

```ts
{ enabled: boolean; dormant_limit: number; scratch: number | null; available: false } | { enabled: boolean; dormant_limit: number; scratch: number | null; hardware: Array<number>; flash: Array<{ address: number; state: "written" | "pending"; where: string; function?: string; file?: string; line?: number }>; dormant: Array<number>; stats: { commits: number; sector_writes: number; emulated: number; displaced: number; rewrites: number; dormant_hits: number; restored: number }; last_rewrite: string | null; journal: string | null; available: true }
```

## debug.breakpoint.flash_restore

種類: `control`。いま Flash に書いてある BKPT をすべて元の命令に戻す（Flash を別のツールに渡す前等）。置いたブレークポイントは残り、次の再開で書き直す。返す restored: 戻した数。

応答:

```ts
{ restored: number }
```

## debug.watchpoint.set

種類: `control`。ウォッチポイントを置く（変数名か "0x..."）。kind: write / read / access。size は変数名なら省略で変数の大きさ、番地なら必須。

| 引数 | 型 | 必須 |
|---|---|---|
| `location` | `string` | ○ |
| `kind` | `string` |  |
| `size` | `number | null` |  |

応答:

```ts
Watchpoint
```

## debug.watchpoint.clear

種類: `control`。ウォッチポイントを外す。

| 引数 | 型 | 必須 |
|---|---|---|
| `location` | `string` | ○ |

応答:

```ts
BreakpointList
```

## debug.run_until_halt

種類: `control`。実行を再開し、止まるまで（ブレークポイント・ウォッチポイント等）か timeout_s まで待つ。時間切れなら止める。
返す reason: breakpoint / watchpoint / step / user / timeout / unknown（止まった理由の印が無い・表に無い値。外からの停止とみなさない）等、where: 止まった関数名 + オフセットとファイル:行。 結果の elapsed_s は PC の時計（time_source = host）で、呼んでから止まったのを見つけて読み終えるまで（対象が走った時間ではない）。

| 引数 | 型 | 必須 |
|---|---|---|
| `timeout_s` | `number` |  |

応答:

```ts
Stop
```

## debug.step

種類: `control`。ソース行単位のステップ（ELF の行表と CFI）。mode: into（呼び出しに入り、前置きの後で止まる。行の情報の無い関数は踏み越える）/ over（呼び出しは踏み越える）/ out（いまの関数から出る。return_value に戻り値。value は `debug_evaluate` と同じ形）/ until（over と同じだが後ろへ戻るループでは止まらない。location（関数名・"ファイル:行"・"0x..."）を指定するとそこまで、いまの関数から戻ればそこでも止まる）。mask_interrupts: 1 命令ずつ進める間は割り込みを止める（DHCSR.C_MASKINTS。呼び出しを踏み越えて走る間は割り込みも動く）。False なら割り込みに入っても踏み越える。返す reason: step（終えた）か、途中で止まった理由（breakpoint / watchpoint / vector_catch / fault / timeout / exit）と、止まった関数名・ファイル:行。halt_bits = 最後の停止の DFSR の立っていたビット全部（reason は 1 語）。故障なら fault に要約。 結果の elapsed_s は PC の時計（time_source = host）で、呼んでから止まったのを見つけて読み終えるまで（対象が走った時間ではない）。 結果の steps = ホストが発行した 1 命令ステップの数、runs = 踏み台・ブレークポイントまで走らせた回数（測った事実。踏み越えた呼び出しから同じ行に戻っても、進んだと分かる）。

| 引数 | 型 | 必須 |
|---|---|---|
| `mode` | `"into" | "over" | "out" | "until"` |  |
| `location` | `string | null` |  |
| `timeout_s` | `number` |  |
| `mask_interrupts` | `boolean` |  |

応答:

```ts
{ reason: string; pc: number; where: string; function?: string; file?: string; line?: number; elapsed_s: number; time_source: "host"; halt_bits?: Array<string>; steps?: number; runs?: number; return_value?: ReturnValue; exit_code?: number; fault?: FaultSummary }
```

## debug.call

種類: `control`。止まったコアで関数を引数付きで実行し、戻り値を得る（AAPCS。DWARF の型で引数を置き、戻り値を解読する）。function: 関数名か "0x..."。arguments の各要素: 数、C の式（文字列。いまのフレームで評価。例 "&sensors[1]"・"MODE_RUN"）、{"string": 文字列}・{"bytes": 16 進}（スタックの下に置いてその番地を渡す。呼んだ後の中身を buffers に返す）。戻り先はリセットの入口に置く踏み台のブレークポイント。呼んだ関数の中のブレークポイントでは止まらない。終われば（timeout_s の時間切れ・故障で打ち切っても）レジスタとスタックの下のメモリを元に戻す（故障の例外に入ったままなら restored = false）。戻らなければ reason = 止まった理由（timeout / vector_catch 等）・ipsr = 止まった時の例外の番号・stopped_at、故障の中なら fault に解析。disable_interrupts: 呼ぶ間は PRIMASK = 1。単体テスト・AI の試行に使う。 return は type と value（値は `debug_evaluate` と同じ形（kind を持つ object））。DWARF の型の分からない関数は value でなく r0・r1（レジスタのまま）。

| 引数 | 型 | 必須 |
|---|---|---|
| `function` | `string` | ○ |
| `arguments` | `Array<number | string | { string: string } | { bytes: string }> | null` |  |
| `timeout_s` | `number` |  |
| `disable_interrupts` | `boolean` |  |

応答:

```ts
{ function: string; address: number; arguments: Array<{ name?: string | null; type?: string; value: number | { index: number } }>; returned?: boolean; reason?: string; return?: ReturnValue; ipsr?: number; stopped_at?: { pc: number; where?: string }; fault?: FaultReport; buffers?: Array<{ index: number; address: number; after: string; string?: string }>; restored: boolean; note?: string; elapsed_s: number; exit_code?: number }
```

## debug.variable.read

種類: `control`。変数を名前で読む（1 / 2 / 4 / 8 バイトは整数、それ以外は 16 進文字列）。index は 4 バイト要素の配列の添字。

| 引数 | 型 | 必須 |
|---|---|---|
| `name` | `string` | ○ |
| `index` | `number | null` |  |

応答:

```ts
{ name: string; value: number | string }
```

## debug.variable.write

種類: `control`。変数を名前で書く（1 / 2 / 4 / 8 バイトの整数、または 4 バイト要素の配列の index 番目）。

| 引数 | 型 | 必須 |
|---|---|---|
| `name` | `string` | ○ |
| `value` | `number` | ○ |
| `index` | `number | null` |  |

応答:

```ts
null
```

## debug.backtrace

種類: `control`。呼び出し履歴（ELF に CFI があれば CFI で巻き戻し、例外のフレームも越える）。各段は index・pc・where・note（exception = 例外で中断された位置、lr_without_cfi = assume_leaf の仮定で出した段）・cfa・function・offset・file・line。CFI で巻き戻したときは最後の段に stop（その先へ巻き戻さなかった理由 reason: max_frames（使った上限 max_frames も）/ no_cfi / not_available / return_address / cfa_below_sp / pc_zero / repeated_frame / stop_pc と、その値）。assume_leaf なら CFI の無い最内の関数を葉（何も積んでいない）と仮定して LR から呼び出し元を出す（関数が LR を積んでいれば偽の呼び出し元になる。既定は仮定せず stop.reason = no_cfi で止める）。variables なら各段の引数とローカル変数（arguments・locals、型付きの値。値は `debug_evaluate` と同じ形（kind を持つ object））も。停止中に呼ぶ。

| 引数 | 型 | 必須 |
|---|---|---|
| `variables` | `boolean` |  |
| `assume_leaf` | `boolean` |  |
| `max_frames` | `number` |  |
| `max_elements` | `number` |  |

応答:

```ts
Array<{ index: number; pc: number; where: string; note: "" | "exception" | "lr_without_cfi"; cfa: number | null; function: string | null; offset: number | null; file: string | null; line: number | null; stop?: UnwindStop; arguments?: Array<Variable>; locals?: Array<Variable> }>
```

## debug.evaluate

種類: `control`。C の式（変数・メンバー・添字・ポインタ・& と *・+ と -）を DWARF の型で評価して読む。名前は関数::名前 で関数の中の static。外部結合が無く同じ名前の static が複数あれば 1 つを選ばずに誤り（候補のファイル:行の一覧）で、"ファイル"::名前・"ファイル"::関数::名前 で選ぶ。frame は `debug_backtrace` の index（0 = いまの関数。その関数の引数・ローカル変数も使える）。live = true なら走らせたまま読む（ライブ ウォッチ。コアを止めず、レジスタを使わずに大域・静的の変数とメモリを読む。ローカル変数は誤り）。結果は expression・type・address（メモリ上なら）・value。value はどれも kind（値の種類。DWARF の型で決まる）を持つ object: int・float・bool → value（float の NaN・無限大は "nan" "inf" "-inf"）、bytes（符号の種類の分からない基本型・void 等）→ hex、enum → name（列挙子に無い値は null）・value、pointer → address・to（+ symbol、char * の先の文字列は string で、NUL の前で読むのをやめたら string_end = limit と使った上限 string_limit（256 バイト）か string_end = unreadable）、struct → members（名前 → 値。無名のメンバーは外側に展開）、array → length（要素数）・items（max_elements 個まで。len(items) < length なら切った。文字の配列は string も）、unavailable → reason。

| 引数 | 型 | 必須 |
|---|---|---|
| `expression` | `string` | ○ |
| `frame` | `number` |  |
| `max_elements` | `number` |  |
| `live` | `boolean` |  |

応答:

```ts
Evaluation
```

## debug.expression.check

種類: `read`。C の式を ELF だけで調べる（ターゲットは使わない。止まっていても・走っていても・つないでいなくても同じ）。式の誤り・名前の無い変数・型の誤りは誤り（名前が無いときは、ローカル変数なら止まっているときに読む旨を添える）。通れば expression・type（ポインタをたどる式は読むまで分からないので null）・address（番地の決まる式）・live（走らせたまま読める）・needs_memory（番地がメモリの値で決まる）。

| 引数 | 型 | 必須 |
|---|---|---|
| `expression` | `string` | ○ |

応答:

```ts
{ expression: string; type: string | null; address?: number; live: boolean; needs_memory: boolean }
```

## debug.frame_variables

種類: `control`。フレームの引数とローカル変数（型付きの値）。frame は `debug_backtrace` の index。 値は `debug_evaluate` と同じ形（kind を持つ object）。

| 引数 | 型 | 必須 |
|---|---|---|
| `frame` | `number` |  |
| `max_elements` | `number` |  |

応答:

```ts
{ function: string | null; arguments: Array<Variable>; locals: Array<Variable> }
```

## debug.write_expression

種類: `control`。C の式の指す値（メモリ上の基本型・列挙（名前でも）・ポインタ・ビットフィールド）に書き、読み直した値を返す。整数は型の範囲を確かめる。道はターゲットの状態でホストが選ぶ: 止まっていれば frame のフレームで（via = halted）、走行中（RUNNING・SLEEPING）なら止めずに `watch_write` と同じ道で（via = running。大域・静的の式だけ、frame は 0 だけ、atomic = 1 回の書き込みで書いたか）。ほかの状態は誤り。 値は `debug_evaluate` と同じ形（kind を持つ object）。

| 引数 | 型 | 必須 |
|---|---|---|
| `expression` | `string` | ○ |
| `value` | `number | string` | ○ |
| `frame` | `number` |  |

応答:

```ts
ExpressionWrite
```

## debug.type_info

種類: `read`。型の名前（struct Sensor・typedef 名等）の大きさ・種類・メンバー（オフセット・ビットフィールド）・列挙子。ELF だけを使う（ターゲット不要）。

| 引数 | 型 | 必須 |
|---|---|---|
| `name` | `string` | ○ |

応答:

```ts
{ name: string; size: number | null; kind: "void" | "base" | "pointer" | "struct" | "array" | "enum" | "function"; members?: Array<{ name: string | null; type: string; offset: number | null; bit_size?: number; bit_offset?: number | null }>; enumerators?: { [key: string]: number } }
```

## debug.disasm.range

種類: `control`。番地から length バイト（4096 まで）を逆アセンブルする（Thumb / Thumb-2。関数名・ファイル:行・分岐先とリテラルの名前付き）。接続中はターゲットのメモリ、未接続か from_elf なら ELF。命令は offset 番目から limit 件（total が全体）。branch_tables = [{address: tbb / tbh の命令の番地, entries: 表の項目の数}]（使う側が明示する。表は命令の直後で、そのバイトをデータとして区切る。渡さない表の大きさは推さない）。データの各項目の data_basis は区切った根拠（mapping = ELF の $d / branch_table = branch_tables / given = data_ranges（使う側が渡した区間。$t・$d の無い所だけ）/ undecodable = 解読できない）。ldr [pc] の読む先をデータとみなす推定は本体ではしない（読む先 literal と値 literal_value は返す。区間を作るのはプラグイン fprobe.plugins.heuristics.literal_pool_ranges）。

| 引数 | 型 | 必須 |
|---|---|---|
| `address` | `number` | ○ |
| `length` | `number` | ○ |
| `from_elf` | `boolean` |  |
| `offset` | `number` |  |
| `limit` | `number` |  |
| `branch_tables` | `Array<{ address: number; entries: number }> | null` |  |
| `data_ranges` | `Array<{ address: number; size: number }> | null` |  |

応答:

```ts
Listing
```

## debug.disasm.function

種類: `control`。関数の全体（関数名か 0x... の関数の中の番地）を逆アセンブルする。branch_tables = [{address: tbb / tbh の命令の番地, entries: 表の項目の数}]（使う側が明示する。表は命令の直後で、そのバイトをデータとして区切る。渡さない表の大きさは推さない）。データの各項目の data_basis は区切った根拠（mapping = ELF の $d / branch_table = branch_tables / given = data_ranges（使う側が渡した区間。$t・$d の無い所だけ）/ undecodable = 解読できない）。ldr [pc] の読む先をデータとみなす推定は本体ではしない（読む先 literal と値 literal_value は返す。区間を作るのはプラグイン fprobe.plugins.heuristics.literal_pool_ranges）。

| 引数 | 型 | 必須 |
|---|---|---|
| `location` | `string` | ○ |
| `from_elf` | `boolean` |  |
| `offset` | `number` |  |
| `limit` | `number` |  |
| `branch_tables` | `Array<{ address: number; entries: number }> | null` |  |
| `data_ranges` | `Array<{ address: number; size: number }> | null` |  |

応答:

```ts
Listing
```

## debug.disasm.around

種類: `control`。番地（省略時は PC）の前 before 命令・その命令・後ろ after 命令（前は関数の先頭・$t から揃える。そこから PC まで sync_limit バイトより遠い・PC に着地しない・読めない・PC がデータの区間なら揃えずに PC から読み、sync = start と sync_skipped = {from, kind, reason（too_far / not_aligned / unreadable / pc_in_data）, distance, limit}）。pc_index がその命令の位置。branch_tables = [{address: tbb / tbh の命令の番地, entries: 表の項目の数}]（使う側が明示する。表は命令の直後で、そのバイトをデータとして区切る。渡さない表の大きさは推さない）。データの各項目の data_basis は区切った根拠（mapping = ELF の $d / branch_table = branch_tables / given = data_ranges（使う側が渡した区間。$t・$d の無い所だけ）/ undecodable = 解読できない）。ldr [pc] の読む先をデータとみなす推定は本体ではしない（読む先 literal と値 literal_value は返す。区間を作るのはプラグイン fprobe.plugins.heuristics.literal_pool_ranges）。

| 引数 | 型 | 必須 |
|---|---|---|
| `address` | `number | null` |  |
| `before` | `number` |  |
| `after` | `number` |  |
| `from_elf` | `boolean` |  |
| `branch_tables` | `Array<{ address: number; entries: number }> | null` |  |
| `data_ranges` | `Array<{ address: number; size: number }> | null` |  |
| `sync_limit` | `number` |  |

応答:

```ts
{ pc: number; memory: "target" | "snapshot" | "elf"; sync: "function" | "mapping" | "start"; sync_skipped: SyncSkipped; total: number; offset: number; pc_index: number | null; instructions: Array<Instruction> }
```

## debug.disasm.text

種類: `control`。ソースの行を挟んだ逆アセンブルの文字列（AI 向け。PC の命令には => の印）。location なら関数、address と length なら範囲、どちらも無ければ address（省略時は PC）の前後（sync・sync_skipped・sync_limit は debug.disasm.around と同じ）。branch_tables = [{address: tbb / tbh の命令の番地, entries: 表の項目の数}]（使う側が明示する。表は命令の直後で、そのバイトをデータとして区切る。渡さない表の大きさは推さない）。データの各項目の data_basis は区切った根拠（mapping = ELF の $d / branch_table = branch_tables / given = data_ranges（使う側が渡した区間。$t・$d の無い所だけ）/ undecodable = 解読できない）。ldr [pc] の読む先をデータとみなす推定は本体ではしない（読む先 literal と値 literal_value は返す。区間を作るのはプラグイン fprobe.plugins.heuristics.literal_pool_ranges）。

| 引数 | 型 | 必須 |
|---|---|---|
| `location` | `string | null` |  |
| `address` | `number | null` |  |
| `length` | `number | null` |  |
| `before` | `number` |  |
| `after` | `number` |  |
| `source` | `boolean` |  |
| `from_elf` | `boolean` |  |
| `branch_tables` | `Array<{ address: number; entries: number }> | null` |  |
| `data_ranges` | `Array<{ address: number; size: number }> | null` |  |
| `sync_limit` | `number` |  |

応答:

```ts
{ memory: "target" | "snapshot" | "elf"; sync: "function" | "mapping" | "start"; sync_skipped: SyncSkipped; text: string }
```

## debug.semihosting.configure

種類: `control`。セミホスティング（BKPT 0xAB）の処理の設定。既定で有効: 止まった理由が BKPT 0xAB なら処理して実行を続け（`debug_run_until_halt` の中と `target_resume` の後）、出力は `rtt_read` の channel semihosting、SYS_EXIT は reason exit と exit_code。ファイル操作は file_root を指定したときだけ、その下に限る（file_root・writable は MCP の道具には出さない）。

| 引数 | 型 | 必須 |
|---|---|---|
| `enabled` | `boolean` |  |
| `file_root` | `string | null`（MCP の道具には出さない） |  |
| `writable` | `boolean`（MCP の道具には出さない） |  |
| `cmdline` | `string` |  |

応答:

```ts
{ enabled: boolean; file_root: string | null; writable: boolean; requests: number; exit: { code: number | null; reason: number | null } | null; watching: boolean }
```

## debug.semihosting.status

種類: `read`。セミホスティングの設定・処理した要求の数・最後の終了（exit: code・reason）・背景の見張りの有無。

応答:

```ts
{ enabled: boolean; file_root: string | null; writable: boolean; requests: number; exit: { code: number | null; reason: number | null } | null; watching: boolean }
```

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
| `external_only` | `boolean` |  |

応答:

```ts
{ trigger_index: number; trigger_time: number; trigger_time_span: number; trigger_fine_sample: number | null; xtrig_source_time: number | null; sample_period_ns: number; samples: number; channel_period_ns: { [key: string]: number }; channels: { [key: string]: { initial: number; edges: number; first_edges_ns: Array<number>; first_edges_truncated: boolean } }; first_edges_limit: number; constant_low: Array<number>; skew_unknown: Array<number>; channel_timing: Array<LaChannelTiming>; vcd: string | null; filter?: { channels: Array<number>; min_width_ns: number; delay_ns: number; delay_cycles: number }; pulse?: { channel: number; level: number; timeout: boolean; min_ns: number; max_ns: number | null }; pretrigger: number }
```

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
| `external_only` | `boolean` |  |

応答:

```ts
{ sample_period_ns: number; depth: number; pretrigger: number; filter?: { channels: Array<number>; min_width_ns: number; delay_ns: number; delay_cycles: number }; pulse?: { channel: number; level: number; timeout: boolean; min_ns: number; max_ns: number | null } }
```

## la.collect

種類: `control`。完了を待って読み出し、チャンネルごとの要約 channels = {ch: {initial, edges, first_edges_ns（トリガ = 0 ns からの先頭 first_edges_limit 個。ch ごとの時刻のずれを足したもの）, first_edges_truncated}} と、常に 0 で省いたチャンネル constant_low、ch ごとの時刻 channel_timing = [{channel, kind, skew_ticks（ch の変化 = ピンの時間軸 + これ。null = 分からない）, span_ticks, from}] を返す。vcd_path があれば VCD も書く（ch のずれで置き直す）。
channel_period_ns = {ch: ns}: sample_period_ns より粗い間隔で取った ch（1 周期に複数のサンプルの構成で sys で取る ch。値は sys の周期ごとで、その間の変化の時刻は分からない）。trigger_fine_sample = ピンが変わった細かいサンプルの番号（取り込みが sys の整数倍の構成だけ。ほかは null）。xtrig_source_time = このトリガをクロストリガの源（ロジアナのトリガ）にしたときの源のパルスの周期（タイムベース。TLV TIMING の la_xtrig 等。申告の無い古い回路は null）。skew_unknown = ch のずれが分からず first_edges_ns に足さなかった ch（channel_timing の skew_ticks が null の ch）。VCD は時刻を丸めない最も粗い単位（10 ns の格子は ns、800 MS/s は 10 ps）で、ずらさなかった ch を $comment に書く。

| 引数 | 型 | 必須 |
|---|---|---|
| `timeout_s` | `number` |  |
| `vcd_path` | `string | null` |  |

応答:

```ts
{ trigger_index: number; trigger_time: number; trigger_time_span: number; trigger_fine_sample: number | null; xtrig_source_time: number | null; sample_period_ns: number; samples: number; channel_period_ns: { [key: string]: number }; channels: { [key: string]: { initial: number; edges: number; first_edges_ns: Array<number>; first_edges_truncated: boolean } }; first_edges_limit: number; constant_low: Array<number>; skew_unknown: Array<number>; channel_timing: Array<LaChannelTiming>; vcd: string | null }
```

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
| `external_only` | `boolean` |  |

応答:

```ts
{ trigger_index: number; trigger_time: number; trigger_time_span: number; xtrig_source_time: number | null; sample_period_ns: number; samples: number; duration_s: number; before_beats: number; channels: { [key: string]: { initial: number; edges: number; first_edges_ns: Array<number> } }; skew_unknown: Array<number>; channel_timing: Array<LaChannelTiming>; vcd: string | null; filter?: { channels: Array<number>; min_width_ns: number; delay_ns: number; delay_cycles: number }; pulse?: { channel: number; level: number; timeout: boolean; min_ns: number; max_ns: number | null }; after_beats: number }
```

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
| `external_only` | `boolean` |  |

応答:

```ts
{ sample_period_ns: number; capacity_beats: number; after_beats: number; filter?: { channels: Array<number>; min_width_ns: number; delay_ns: number; delay_cycles: number }; pulse?: { channel: number; level: number; timeout: boolean; min_ns: number; max_ns: number | null } }
```

## la.deep.collect

種類: `control`。深いバッファの完了を待って、トリガ前 before_beats 拍から後ろを読み、要約を返す（first_edges_ns は ch ごとの時刻のずれを足したもの。ずれは channel_timing、ずれが分からず足さなかった ch は skew_unknown。xtrig_source_time も la_collect と同じ。la_collect と同じ形）。

| 引数 | 型 | 必須 |
|---|---|---|
| `timeout_s` | `number` |  |
| `before_beats` | `number` |  |
| `vcd_path` | `string | null` |  |

応答:

```ts
{ trigger_index: number; trigger_time: number; trigger_time_span: number; xtrig_source_time: number | null; sample_period_ns: number; samples: number; duration_s: number; before_beats: number; channels: { [key: string]: { initial: number; edges: number; first_edges_ns: Array<number> } }; skew_unknown: Array<number>; channel_timing: Array<LaChannelTiming>; vcd: string | null }
```

## la.stream

種類: `control`。ロジアナを止めずに duration_s 秒流し続け、VCD に書く（トリガなし。記録長は保存先で決まる）。
RAW の速さは、ボードの受け取りの速さまで（PYNQ の PS 直読み: 50 MB/s = 16 ch で 25 MS/s。USB・シリアルの Primer: 25 MB/s 程度 = 12.5 MS/s）。pack_channels（8・4・2。rle = false のときだけ。ケイパビリティ la_pack）は、使うチャンネル（ch 0〜n − 1）だけを詰めて流し、データ量が 1/2・1/4・1/8 になる（8 ch で 50 MS/s、4 ch で 100 MS/s が 50 MB/s）。選んだチャンネルは欠けず、選ばなかった上位のチャンネルは VCD に出ない。rle = true（変化点圧縮）は遅い信号ならそれ以上。
overrun = true はホストが遅れて途中で止まったこと（それまでの分は正しい）。VCD は ch ごとの時刻のずれで ch を置き直し（分からない ch はずらさず $comment に書く）、時刻を丸めない最も粗い単位で書く（800 MS/s は 10 ps）。
gaps = true（ケイパビリティ la_stream_gaps。PYNQ の深いバッファ）: 溢れても止めず、回路が捨てた拍の区間（欠け）を記録して流し続ける。結果の gaps = 欠けの区間 [{start, samples, beat}]（流れのサンプルの位置 start から samples サンプルは値が無い。埋めない。VCD では全信号が x）、gap_samples = その和、unplaced_beats = 欠けの記録が届かず位置が決まらないので書かなかった拍（ふつう 0）。gap_hold_bytes（gaps = true のとき必ず指定。既定は無い）= 欠けの記録が届くまで位置の決まらない拍を PC に持つ上限（バイト）。超えたら推定で置かずに止める（誤り StreamHoldExceeded）。gap_held_peak_bytes = 持った量の最大。link_buffer の dropped_frames・lost_frames = この流れの間にリンク（UDP・UART）が捨てた・落とした数（欠けたら流れは誤り StreamLinkLoss で止まる。それまでの分は正しい）。samples は値のあるサンプルの数、duration_s は欠けを含む長さ。false（既定）は今までどおり溢れたら止まる。
relay = 出し切った後の PYNQ の中継の状態（zc_state = ゼロコピーの道の状態、tcp_path = 使った送りの道、copied 等の数。事実の読み）。中継が無い・返事が無ければ null。
control_read_retries（既定 0）= 流している間の見回りの CSR の READ（読みに副作用が無い）の応答が時間切れになったとき、送り直してよい回数（使う側が明示する。0 なら送り直さず止まる。止める WRITE は送り直さない）。control = この流れの間に送り直した READ と読み捨てた遅れた応答の数（事実）。
raw_path（既定 null）= 受け取った拍をそのまま保存する生の保存ファイル（.fplaraw）の置き場所。指定するとその場所に残り、結果の raw に返る（後から la.raw_to_vcd で VCD にできる）。null なら VCD の隣の一時ファイルに取り込み、VCD にした後に消す（raw は null）。取り込み（受け取り）と VCD への書き出しは分かれていて、受け取りは VCD の書き出しの速さに律速されない。途中で誤り（StreamLinkLoss 等）が出ても、取り込めた分は VCD になってから誤りが返る。

| 引数 | 型 | 必須 |
|---|---|---|
| `vcd_path` | `string` | ○ |
| `duration_s` | `number` |  |
| `rate_hz` | `number | null` |  |
| `rle` | `boolean` |  |
| `pack_channels` | `8 | 4 | 2 | null` |  |
| `gaps` | `boolean` |  |
| `gap_hold_bytes` | `number | null` |  |
| `control_read_retries` | `number` |  |
| `raw_path` | `string | null` |  |

応答:

```ts
{ samples: number; duration_s: number; sample_period_ns: number; elapsed_s: number; mb_per_s: number; overrun: boolean; link_buffer: StreamBuffer | null; vcd: string; usage: Array<LaStreamUsage>; first_channel: number; gaps: Array<{ start: number; samples: number; beat: number }>; gap_samples: number; unplaced_beats: number; gap_held_peak_bytes: number; relay: RelayStatus | null; control: ControlCounters; raw: string | null }
```

## la.raw_to_vcd

種類: `control`。ロジアナの生の保存ファイル（la.stream の raw_path で残したもの）を VCD に書き出す。取り込み（機器からの受け取り）とは別に、あとから行う（機器には触れない）。VCD のバイト列は、la.stream が直接書いたものと同じ。names は信号の名前（{"5": "rx"}）で、ファイルに保存した名前に重ねる。complete = false は、取り込みの途中で止まった（索引の無い）ファイルで、読めた所までを書いた（最後の切れたかたまりは捨てた）こと。samples は値のあるサンプルの数、chunks は読んだかたまりの数、sample_period_ns・channels・first_channel・rle・pack はファイルのヘッダ（取り込みの性質）の事実。

| 引数 | 型 | 必須 |
|---|---|---|
| `raw_path` | `string` | ○ |
| `vcd_path` | `string` | ○ |
| `names` | `{ [key: string]: string } | null` |  |

応答:

```ts
{ raw: string; vcd: string; complete: boolean; chunks: number; samples: number; sample_period_ns: number; channels: number; first_channel: number; rle: boolean; pack: number }
```

## la.view

種類: `read`。最後の捕捉（誰が取ったものでも）を width 画素に縮めた波形（画面の表示用）。levels[ch][i] は画素 i の区間が
'0'（ずっと 0）/ '1'（ずっと 1）/ 'x'（区間内で変化）。start・end はサンプル番号（end 省略で最後まで）。

| 引数 | 型 | 必須 |
|---|---|---|
| `start` | `number` |  |
| `end` | `number | null` |  |
| `width` | `number` |  |

応答:

```ts
{ capture: LaCaptureInfo; start: number; end: number; width: number; levels: Array<string> }
```

## la.trigger_status

種類: `read`。ロジアナのトリガの条件と状態（la のレジスタの読み戻し。la.arm・la.deep.arm が書いたもの）: mask・value・rise・fall、external_only（パターンが決して成り立たない形 = クロストリガでだけ捕捉。la.arm の external_only が書く形そのもの）、rate_hz・pretrigger、filter（ケイパビリティ la_filter）・pulse（la_pulse）、internal（内蔵バッファの捕捉: capturing・triggered・done・trigger_time）・deep（深いバッファの同じもの。外部メモリがあれば）。trigger_time はトリガした後だけ（それまで null）。トリガの回数を数えるレジスタは無い。trigger_time はトリガのサンプルのピンの時間軸の時刻（`la_collect` の trigger_time と同じ意味。ピンの変化は trigger_time 以上 trigger_time_hi 未満。深いバッファの詰め pack ≠ 0 では語の分だけ広い）、trigger_time_raw は CSR の生の値（回路がトリガを受けた周期。段数を引いていない）、trigger_fine_sample は取り込みが sys の整数倍の構成の細かいサンプルの番号、xtrig_source_time はこのトリガをクロストリガの源にしたときの源のパルスの周期（TLV TIMING。申告が無ければ null で trigger_time_unknown に名前）、trigger_time_from は根拠。rate_hz は取り込みの速さ（1 周期に複数のサンプルの内蔵バッファは rate を使わない: rate_used = false。深いバッファの N 並列は 2^step × (rate + 1) 細かいサンプルごと）、rate_raw はレジスタの値。filter・pulse の ns は取り込みのクロック（la_sample_hz。取り込みの領域の構成は cap_clk）で数えた値。

応答:

```ts
{ mask: number; value: number; rise: number; fall: number; external_only: boolean; rate_hz: number; rate_raw: number; rate_used: boolean; pretrigger: number; filter?: { channels: number; cycles: number; min_width_ns: number | null }; pulse?: { mode: "none" } | { mode: "width" | "timeout"; level: number; channel: number; min_ns: number; max_ns: number | null }; internal?: { capturing: boolean; triggered: boolean; done: boolean; trigger_time: number | null; trigger_time_raw: number | null; trigger_time_hi: number | null; trigger_fine_sample: number | null; xtrig_source_time: number | null; trigger_time_from: Array<string> | null; trigger_time_unknown: Array<string> }; deep?: { capturing: boolean; triggered: boolean; done: boolean; trigger_time: number | null; trigger_time_raw: number | null; trigger_time_hi: number | null; trigger_fine_sample: number | null; xtrig_source_time: number | null; trigger_time_from: Array<string> | null; trigger_time_unknown: Array<string> } }
```

## timeline.tiles

種類: `read`。応答はバイナリのフレーム（WebSocket の利用者だけ。MCP の道具には無い）。共通タイムラインのタイル（画面の時間軸が毎フレームの間引きに使う材料。応答は WebSocket のバイナリのフレーム）。source = capture（最後の捕捉、時刻はサンプル番号）/ recording（読む記録、時刻はタイムベース）の範囲 [t0, t1) の、波形の生の変化点（かたまりごとに頭の値と変化点）と出来事の段（stream ごとに t・t_end・種類と、段の規則（fprobe.timeline_rows）で分けた段・ラベル・値・入れ子の深さ・色の意味・飛び先の番地。状態・値の段は t0 の前の最後の出来事も。前を探すのは範囲の幅の 16 倍・256 倍・4096 倍・記録の始めと広げ、何か見つかった所で止める: 探した始めを段の carry_from、探すべき所を探し切ったかを carry_complete に。false なら carry_from より前に変わったきりの段の始めの値は探していない）。生データが max_edges（全チャンネルの和）・max_events（stream ごと）を超える部分は、幅 bin 程度（そろえた区間）の要約（波形の状態、段 × 区間の件数、値の段の最小・最大）。AI は la.view / recording.waveform / recording.density を使う。

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

応答:

```ts
TimelineTile
```

## la.decode

種類: `read`。最後の捕捉の通信を解読する。protocol は解読器の名前（la.decoders で一覧。組み込み: uart / spi / i2c / swd / can（CAN FD 含む。sample_point は必須）/ onewire / jtag / i2s / parallel / manchester と、積み重ねる jtag_arm（jtag の上）/ spiflash（spi）/ eeprom24（i2c）/ modbus（uart）、la.decoder_load で読み込んだ利用者の解読器）。settings はその設定。通信の設定（プロトコルの枠の値・線の割り当て）は既定を持たない（必須。つながない線は null を明示。足りなければ要る設定の名前を挙げた誤り）: uart: channel・baud（波形から推定しない）・data_bits・parity・stop_bits・invert・lsb_first、spi: sck・mosi・miso・cs・mode・msb_first・word_bits・cs_active_low、i2c: scl・sda、swd: swclk・swdio・drive・turnaround・orundetect（捕捉の先頭の値）、can: channel・bitrate・sample_point・invert（CAN FD は data_bitrate と data_sample_point を一緒に）、onewire: channel・overdrive（timing の既定は規格の値）、jtag: tck・tms・tdi・tdo・trst（initial_state・ir_names は省ける）、i2s: sck・ws・sd・format・word_bits（null = スロットの全ビット）・signed、parallel: data（列）・clock・enable（edge は clock を、enable_level は enable を与えたとき必須）、manchester: channel・bitrate・tolerance（1 ビットに対する許容のずれ 0〜0.25）・convention・word_bits・msb_first、jtag_arm: ir_offset・dr_offset、spiflash: address_bytes、eeprom24: device・address_bytes・block_bits、modbus: なし（区切りの既定は規格の t3.5。要求か応答か決まらないフレームは kind frame・direction null）。積み重ねる解読器は下の解読の設定を下の名前のキーに入れる（例 modbus: {"uart": {uart の設定}}、何段でも）。start / end（サンプル）・kinds・errors_only で絞り、offset から limit 件。counts は種類ごとの件数、errors は誤りの数。kinds を省くと解読器の既定の種類（swd は transfer、jtag は ir・dr・reset 等）。記録中なら注釈を共通の時間軸にも入れる。結果の kinds は絞った種類（null = 全部）、total・counts は絞る前の件数。

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

応答:

```ts
{ protocol: string; params: { [key: string]: Json }; capture: { id: number; length: number; sample_period_ns: number; trigger_index: number; channels: number; source: string }; total: number; errors: number; counts: { [key: string]: number }; kinds: Array<string> | null; matched: number; offset: number; annotations: Array<{ start: number; end: number; start_s: number; end_s: number; start_rel_s: number; end_rel_s: number; kind: string; text: string; value: number | null; error: string | null; fields: { [key: string]: Json } | null }> }
```

## la.decoders

種類: `read`。la.decode に使える解読器の一覧。各要素: name・lower（積み重ねる解読器が受け付ける下の解読。空なら波形を読む）・summary・params（name・required（既定を持たない設定）・nullable（null を渡せる。つながない線等）・type（解読器の引数の型の申告: channel = ロジアナのチャンネルの番号 / channels = その並び / integer / number / boolean / string / object。注釈が無い・1 つの型に決まらなければ null）・default）・default_kinds・origin（builtin / 利用者のファイルの絶対パス）。

応答:

```ts
{ decoders: Array<DecoderInfo> }
```

## la.decoder_load

種類: `control`。利用者の解読器のファイル（Python）を読み込んで登録する。ファイルの中で `from fprobe.decode import Annotation, decoder` とし、`@decoder("名前", lower=("uart",))` を付けた関数 f(lower: Decoding, **設定) -> Decoding を書く（結果は lower.derive(名前, 注釈の列, 設定) で作る。lower を省くと f(tr: Transitions, **設定) で波形を読む）。同じファイルを読み直すと前の登録を置き換える。組み込みの名前は使えない。

| 引数 | 型 | 必須 |
|---|---|---|
| `path` | `string` | ○ |

応答:

```ts
{ loaded: Array<string>; origin: string; decoders: Array<DecoderInfo> }
```

## la.export

種類: `read`。最後の捕捉をファイルに書く。format: sr（sigrok のセッション。PulseView・sigrok-cli で開き、sigrok の約 100 種のデコーダを使える）/ vcd。channels は書くチャンネル（sr のみ、既定は全部）、names はチャンネルの名前（{"5": "rx"}）。

| 引数 | 型 | 必須 |
|---|---|---|
| `path` | `string` | ○ |
| `format` | `"sr" | "vcd"` |  |
| `channels` | `Array<number> | null` |  |
| `names` | `{ [key: string]: string } | null` |  |

応答:

```ts
{ path: string; format: "vcd"; capture: CaptureInfo; samples: number; channels: Array<number> } | { path: string; samples: number; channels: Array<number>; samplerate_hz: number; unitsize: number; data_files: number; format: "sr"; capture: CaptureInfo }
```

## la.load

種類: `control`。sigrok の .sr（他のロジアナ・PulseView で保存したもの）を最後の捕捉として読み込む。以後 la.decode・la.measure・la.view がそれを読む。トリガは先頭のサンプル。

| 引数 | 型 | 必須 |
|---|---|---|
| `path` | `string` | ○ |

応答:

```ts
{ capture: CaptureInfo; path: string; names: { [key: string]: string } }
```

## la.measure

種類: `read`。最後の捕捉の範囲 [start, end)（サンプル番号）を測る。チャンネルごと（channels 省略で変化のあるもの）: rises・falls・high_ratio（high の時間の割合）・frequency_hz・duty（平均・最小・最大）と、period・high・low の幅の統計（件数・min/max/mean/stdev_ns・min_at/max_at = サンプル番号）。範囲の端で切れたパルスは数えない。relations でチャンネル間: {"type": "delay", "from", "from_edge", "to", "to_edge"}（rise / fall / any。from のエッジから次の to のエッジまで）、{"type": "setup_hold", "clock", "clock_edge", "data"（番号か列）, "setup_min_ns", "hold_min_ns"}（違反の一覧）。分解能は resolution_ns（1 サンプル）。ch ごとの時刻のずれを入れて測る。skew_unknown = ずれが分からず、ずらさずに置いた ch（測った ch・relations の ch のうち。その ch の位置とほかの ch との時間差は、そのずれの分だけ分からない）。

| 引数 | 型 | 必須 |
|---|---|---|
| `start` | `number` |  |
| `end` | `number | null` |  |
| `channels` | `Array<number> | null` |  |
| `relations` | `Array<Record<string, unknown>> | null` |  |
| `capture_id` | `number | null` |  |

応答:

```ts
{ capture: CaptureInfo; at_unit: "sample" | "timebase"; resolution_ns: number; channels: { [key: string]: MeasureChannel }; relations: Array<MeasureRelation>; skew_unknown: Array<number>; start: number; end: number }
```

## events.start

種類: `control`。DAP 転送のイベント記録を始める（外部メモリのリング。古いものから上書き）。

応答:

```ts
{ capacity: number }
```

## events.stop

種類: `control`。イベント記録を止める（記録は残り、`events_read` で読める）。

応答:

```ts
{ written: number; lost: number }
```

## events.read

種類: `control`。イベントを古い順に返す。since が無ければ最新の last 件。since（前回の結果の next）があれば、その番号から last 件まで（続けて読めば取りこぼさない。読む前にリングが上書きした数は skipped）。first = 返した最初の番号、next = 次に since に渡す番号（記録を始めてから書いた数で数える）。raw = False ならアクセス（ポストされた読みを対応付けた後）、True なら線上の転送そのもの。time_us は記録開始とは無関係なタイムベースの時刻（µs）。ポストされた AP の読み（値が後の転送で返ったもの）は、対象が読んだ瞬間の範囲 [time_lo_us, time_us] を持つ: time_us = 値を返した転送、requested_us = 読みを頼んだ転送、time_lo_us = その前に線上で終わった転送（確かな下限。返した中に見えていなければ null）。

| 引数 | 型 | 必須 |
|---|---|---|
| `last` | `number` |  |
| `raw` | `boolean` |  |
| `since` | `number | null` |  |

応答:

```ts
{ written: number; lost: number; first: number; next: number; skipped?: number; items: Array<{ time_us: number; kind: string; address: string; value: string; ack: number; transfers?: number | null; requested_us?: number; time_lo_us?: number | null; time_hi_us?: number | null; waits?: number | null; detail?: DapConfigDetail | { index: number; words: number; ack: number; sticky: boolean; dropped: boolean; basis: Basis }; line?: ByteLine } | { time_us: number; kind: number; ap: boolean; read: boolean; register: number; ack: number; parity_error: boolean; address: string; data: string; transfers?: number | null; waits?: number; config?: { time: number; half_period: number; idle_cycles: number; turnaround: number; data_phase: boolean; swd_sample_delay: number; jtag_sample_delay: number; jtag: boolean; enable: boolean; dr_before: number; dr_after: number; ir_before: number; ir_after: number; ir_len: number }; gap?: { end: number; index: number; words: number; ack: number; sticky: boolean; dropped: boolean; end_us: number } }> }
```

## fpga_obs.open

種類: `control`。対象の FPGA の JTAG で観測の回路 v2（target_ip/fprobe_obs）を読み始め、申告（id・mode = stream（記録型）/ snapshot（標本型）・version・width・groups・fifo_depth・clock_hz・sync_delay・sample_delay・lost_bits）を返す。値を記録に残すには events.start も要る。auto_reopen = 観測の回路が答えなくなって止まったら（対象の FPGA を書き直した等）1 秒ごとに同じ設定で開き直す（記録中なら印。回数は status の reopened。利用者が選んだときだけ）。 id_scans = 始めに ID の応答を待つスキャンの上限（使う側が渡す。0 = 上限なし）。上限まで来なければ止まり、`fpga_obs_status` の stuck・id_timeout。 borrow_ratio = 印が 1 つしか無い鎖の時刻に、前の鎖の印の組から求めた傾きを使う（前の鎖と時計の比が同じという仮定。使う側が明示したときだけ。既定 false は申告の clock_hz の比、無ければ時刻なし。値の time_basis が決め方: pairs / previous_ratio / nominal_ratio）。 tck_hz = 求める TCK（Hz）: 超えない最も速い半周期をホストが選ぶ（観測の回路も jtag_phy なので TCK = sys / (2 × 半周期 + extra)、extra はデバイスの申告）。half_period と同時には渡せない。実際の TCK は `fpga_obs_status` の tck_hz（config は半周期で覚え、そのまま渡せば同じ設定で開き直せる）。

| 引数 | 型 | 必須 |
|---|---|---|
| `chain` | `{ ir_len?: number; ir_before?: number; ir_after?: number; dr_before?: number; dr_after?: number } | null` |  |
| `user_ir` | `number` |  |
| `half_period` | `number` |  |
| `words` | `number` |  |
| `sync_period` | `number` |  |
| `auto_reopen` | `boolean` |  |
| `id_scans` | `number` |  |
| `borrow_ratio` | `boolean` |  |
| `tck_hz` | `number` |  |

応答:

```ts
FpgaObsDeclared
```

## fpga_obs.close

種類: `control`。観測をやめる（対象の TAP を Test-Logic-Reset に戻し、線を手放す。auto_reopen も止める）。

応答:

```ts
Record<string, never>
```

## fpga_obs.status

種類: `read`。観測の状態（running・id_ok・stuck・cmd_pending・overflow_seen・cmd_dropped・scans・id・mode）と、最後に開いた設定 config・申告 declared・auto_reopen・自動で開き直した回数 reopened。

応答:

```ts
{ running: boolean; id_ok: boolean; stuck: boolean; cmd_pending: boolean; overflow_seen: boolean; cmd_dropped: boolean; target_running: boolean; driving: boolean; id_timeout: boolean; scans: number; id: number; mode: "stream" | "snapshot"; config: { chain: { ir_len?: number; ir_before?: number; ir_after?: number; dr_before?: number; dr_after?: number } | null; user_ir: number; half_period: number; words: number; sync_period: number; id_scans: number; borrow_ratio: boolean } | null; declared: FpgaObsDeclared | null; auto_reopen: boolean; reopened: number; tck_hz: number | null }
```

## fpga_obs.trigger_set

種類: `control`。FPGA の条件（システム解析と同じ書き方。例 "restart changed and state=BUSY"・"state=BUSY"（入った瞬間）・"flag rose"。瞬間の項は 1 つまで）を fprobe の観測のエンジンで比べ、成り立ったらハードのクロストリガ（源 obs_match）: halt = 割り込み書き込み（既定は MCU の停止）、la = ロジアナのトリガ（クロストリガだけで捕捉するには la.arm / la.deep.arm の external_only = true。指定しないとパターンが空なので、すぐ捕捉する）。MCU を止める遅れは、観測のスキャン 1 回分（標本型・TCK 2.5 MHz で約 15 µs）+ SWD の書き込み（ホストの CSW・TAR の控えがあれば 4 転送 = 約 190 SWCLK、無ければ読んで戻す転送が足される。2026-10-22 の Shrike-Lite の実測（条件の値の立ち上がり → DHCSR の書き込み）: SWD 4 MHz で 101 µs、1 MHz で 206〜362 µs）。信号の名前は組 group の定義（fpga_obs.signals_get）から。ケイパビリティ obs_match が要る。

| 引数 | 型 | 必須 |
|---|---|---|
| `condition` | `string` | ○ |
| `halt` | `boolean` |  |
| `la` | `boolean` |  |
| `group` | `number` |  |

応答:

```ts
{ mask: number; value: number; change: number; condition: string; halt: boolean; la: boolean }
```

## fpga_obs.trigger_status

種類: `read`。ハードの FPGA の条件のトリガ（fpga_obs.trigger_set）: 条件・halt・la・成り立った回数 count（無ければ condition = null）。

応答:

```ts
{ condition: string; halt: boolean; la: boolean; restored?: true; count: number } | { condition: null; orphan: true; mask: number; value: number; change: number; halt: boolean; la: boolean; count: number } | { condition: null }
```

## fpga_obs.trigger_clear

種類: `control`。ハードの FPGA の条件のトリガをやめる（クロストリガの経路の obs_match を外す。ほかの源の経路は触らない）。

応答:

```ts
{ condition: null }
```

## fpga_obs.register_read

種類: `control`。観測の回路のレジスタ（16 ビット）を読む（0・1 ID、2 VERSION、3 GROUPS、4 FIFO_DEPTH、5・6 CLOCK_HZ、7 SYNC_DELAY、8 SAMPLE_DELAY、9 LOST_BITS、14 CONTROL、15 SELECT）。標本型には無い。

| 引数 | 型 | 必須 |
|---|---|---|
| `addr` | `number` | ○ |

応答:

```ts
{ value: number }
```

## fpga_obs.register_write

種類: `control`。観測の回路のレジスタに書く（14 CONTROL の bit0 = 記録する、15 SELECT = 観測する組）。

| 引数 | 型 | 必須 |
|---|---|---|
| `addr` | `number` | ○ |
| `value` | `number` | ○ |

応答:

```ts
Record<string, never>
```

## fpga_obs.select

種類: `control`。観測する 16 ビットの組を選ぶ（0〜groups − 1）。

| 引数 | 型 | 必須 |
|---|---|---|
| `group` | `number` | ○ |

応答:

```ts
Record<string, never>
```

## fpga_obs.start

種類: `control`。観測の回路が値の変化を記録し始める（始め直すと絶対の時刻と今の値から）。

応答:

```ts
Record<string, never>
```

## fpga_obs.stop

種類: `control`。観測の回路の記録を止める（読み出しは続く）。

応答:

```ts
Record<string, never>
```

## fpga_obs.read

種類: `control`。イベントの記録から観測した値を fprobe の時刻（time_us）で返す。窓は events.read と同じ（続けて読むなら since）。value = null は溢れの切れ目。 各項目の time_basis = 時刻の決め方（pairs = 印の組 2 つ以上の直線 / previous_ratio = 印 1 つと前の鎖の傾き / nominal_ratio = 印 1 つと申告の CLOCK_HZ の比 / null = 時刻なし）。

| 引数 | 型 | 必須 |
|---|---|---|
| `last` | `number` |  |
| `since` | `number | null` |  |

応答:

```ts
{ source: "recording"; items: Array<{ time_us: number | null; time_lo_us: number | null; time_hi_us: number | null; copy: boolean; time_basis: "pairs" | "previous_ratio" | "nominal_ratio" | "assumption_broken" | "hold_limit" | null; target_cycles: number; kind: "value" | "gap" | "group" | "hidden"; group: number; value: number | null; lost: number; fields?: { [key: string]: number | string } }>; untimed_by_limit: number; signals_error?: string } | { written: number; lost: number; first: number; next: number; skipped?: number; items: Array<{ time_us: number | null; time_lo_us: number | null; time_hi_us: number | null; copy: boolean; time_basis: "pairs" | "previous_ratio" | "nominal_ratio" | "assumption_broken" | "hold_limit" | null; target_cycles: number; kind: "value" | "gap" | "group" | "hidden"; group: number; value: number | null; lost: number; fields?: { [key: string]: number | string } }>; signals_error?: string }
```

## fpga_obs.count

種類: `control`。観測した値の最後の last 件（fpga_obs.read の窓。使う側が明示する。既定の値は無い）で、FPGA の信号の条件（restart changed and state=BUSY の形。瞬間の項 changed / rose / fell が要る）ごとに成り立った回数を数える。条件ごとに count と、数えた範囲の事実: values（数えた値の数）・t0_us / t1_us（最初と最後の値の fprobe の時刻。値が無ければ null）・span_s（その間の秒）・gaps（切れ目の数）・hidden（標本型の見えない変化の数）・lost（失った変化の数）。「変化」は同じ組の直前の値と比べる。読めない条件は error に理由。

| 引数 | 型 | 必須 |
|---|---|---|
| `conditions` | `Array<string>` | ○ |
| `last` | `number` | ○ |

応答:

```ts
{ last: number; conditions: Array<{ condition: string; count: number; values: number; t0_us: number | null; t1_us: number | null; span_s: number | null; gaps: number; hidden: number; lost: number; untimed: number; unnamed: number } | { condition: string; error: string }> }
```

## fpga_obs.signals_get

種類: `read`。FPGA の観測の組に付ける信号の定義（signals = 画面の状態の既定、recording = 開いている記録の定義）。

応答:

```ts
{ signals: FpgaSignalDefs | null; recording: FpgaSignalDefs | null; recording_error: string | null }
```

## fpga_obs.signals_set

種類: `control`。信号の定義（組ごとの名前・ビット・書式・列挙）を置く。画面の状態と、記録中・開いている記録に（後から変えても過去の記録の段の名前が変わる）。

| 引数 | 型 | 必須 |
|---|---|---|
| `signals` | `Record<string, unknown>` | ○ |

応答:

```ts
{ signals: FpgaSignalDefs; recording: boolean }
```

## system.status

種類: `read`。MCU と FPGA の両方の今を 1 回で返す（mcu: コアの状態と addresses の 32 ビットの語を止めずに読んだ値・connect（最後に成功した target.connect の引数。対象のリセットで SWD がはずれたら同じ引数でつなぎ直す）、fpga: 観測の状態と各組の最新の値・信号。値はイベントの記録の最後の fpga_last 件の窓から（窓から消えた組は最後に分かった値。time_us で古さが分かる）。見た窓は window = {last, items}）。システム解析の画面・AI の見回り用。

| 引数 | 型 | 必須 |
|---|---|---|
| `addresses` | `Array<number> | null` |  |
| `fpga_last` | `number` |  |

応答:

```ts
{ time_us: number; mcu: { connected: true; state: CoreState; target?: string; frequency_hz?: number; wire?: "swd" | "jtag"; core?: number; arch?: string; hart?: number; debug_module?: DebugModuleInfo; architecture?: "armv7-a" | "armv6"; debug_version?: number; breakpoints?: number; watchpoints?: number; core_count?: number; debug_base?: number; apb_ap?: number; discovery?: "ahb_system_map" | "rom_table" | "part_table"; jtag_chain?: Array<number>; tap?: number; pc?: number; where?: string; function?: string; file?: string; line?: number; halt_reason?: "request" | "breakpoint" | "bkpt_instruction" | "watchpoint" | "vector_catch" | "external"; halt_bits?: Array<"HALTED" | "BKPT" | "DWTTRAP" | "VCATCH" | "EXTERNAL">; debug_mode?: "monitor"; fatal?: FatalState; rescue_dp?: number; cores?: Array<CoreEntry>; memory: { [key: string]: number }; connect: LastConnect; linux?: LinuxTargetFacts } | { connected: false; connect: LastConnect; linux_attaching?: LinuxAttaching } | { connected: true; error: string; connect: LastConnect }; fpga: { available: false } | { available: true; running: boolean; id_ok: boolean; stuck: boolean; cmd_pending: boolean; overflow_seen: boolean; cmd_dropped: boolean; target_running: boolean; driving: boolean; id_timeout: boolean; scans: number; id: number; mode: "stream" | "snapshot"; config: { chain: { ir_len?: number; ir_before?: number; ir_after?: number; dr_before?: number; dr_after?: number } | null; user_ir: number; half_period: number; words: number; sync_period: number; id_scans: number; borrow_ratio: boolean } | null; declared: FpgaObsDeclared | null; auto_reopen: boolean; reopened: number; latest?: Array<{ time_us: number | null; time_lo_us: number | null; time_hi_us: number | null; copy: boolean; time_basis: "pairs" | "previous_ratio" | "nominal_ratio" | "assumption_broken" | "hold_limit" | null; target_cycles: number; kind: "value" | "gap" | "group" | "hidden"; group: number; value: number | null; lost: number; fields: { [key: string]: number | string }; age_us: number | null }>; gaps?: number; window?: { last: number; items: number }; latest_error?: string; tck_hz: number | null } }
```

## watch.set

種類: `control`。変数を止めずに周期的に読む。expressions: 変数名・C の式・番地（"0x20000000" か "0x20000000:バイト数"）。決まった番地の式は FPGA の DAP サンプラが rate_hz で読む（pyOCD 等のデバッグと同時に使える。合わせて 8 か所・各 16 語まで。値はイベント記録と、記録中なら stream "sample" に入る）。ポインタをたどる式（"head->pos.x"・"*p"）はホストが chase_rate_hz ごとに番地を引き直して読む（method = host）。誤った式（名前が無い・メモリ上に無い等）は rejected（[{expression, error}]）に外して残りを取る（全部が誤りなら始めずに誤り）。jitter（0〜15）: サンプラの周期のゆらぎ（周期的な処理との噛み合いを防ぐ）。names: 式ごとの名前（値・時間軸の段の名前。記録中なら記録にも残す）。

| 引数 | 型 | 必須 |
|---|---|---|
| `expressions` | `Array<string>` | ○ |
| `rate_hz` | `number` |  |
| `ap` | `number` |  |
| `chase_rate_hz` | `number` |  |
| `jitter` | `number` |  |
| `names` | `Array<string> | null` |  |

応答:

```ts
{ watches: Array<WatchedVariable>; rate_hz: number; chase_rate_hz: number; slots: Array<{ address: string; words: number }>; rejected: Array<{ expression: string; error: string }> }
```

## watch.stop

種類: `control`。周期的な読みを止める（読んだ値はイベント記録・記録に残る）。

応答:

```ts
{ stopped: true } | SamplerStatus
```

## watch.status

種類: `control`。観測している変数・周期と、サンプラの状態（巡回の数・誤り・飛ばした周期 = watch.set からの数。FPGA の起動からの数は device。最後の ACK）、watch.set で外した誤った式（rejected）、始めてからの秒（since_start_s）。ポインタをたどる式の読みの誤り（chase_errors）と条件つきの取得の状態（capture）、最後に watch.set した呼び出し元（client: {id, name, role}。デーモンが記録した事実。分からなければ null）も。 loss_ratio = 飛ばした周期 ÷ (巡回 + 飛ばした周期)、measured_hz = 巡回 ÷ since_start_s（数が無い・0 なら null）。stats = 変数ごとの watch.set から読んだ値すべての統計 {count, min, max, mean, not_numeric}（サンプラの値はイベント記録を続きから読んで数える。読めずに一周された・上限を超えたイベントの数は stats_lost_events）。

応答:

```ts
{ watches: Array<WatchedVariable>; rejected: Array<{ expression: string; error: string }>; stats: { [key: string]: { count: number; min: number | null; max: number | null; mean: number | null; not_numeric: number } }; stats_lost_events: number | null; running: boolean; since_start_s: number | null; rate_hz: number | null; client: { id: number; name: string; role: string } | null; chase_rate_hz: number | null; chase_errors: { [key: string]: string }; capture: { state: "armed" | "capturing" | "done"; reason: "stop" | "post" | "max_samples" | "cancelled" | null } | null; rounds?: number; errors?: number; overruns?: number; last_ack?: number; busy?: boolean; held?: boolean; device?: { rounds: number; errors: number; overruns: number }; loss_ratio: number | null; measured_hz: number | null }
```

## watch.values

種類: `control`。観測している変数の最新の値（型で解釈）と、その時刻（タイムベースの µs）。 値は `debug_evaluate` と同じ形（kind を持つ object）。番地の式（"0x20000000:4"）の値は kind = int（符号なし整数）。 time_point = サンプラの値の時刻の意味: read_request（DAP の時刻が線上の点の回路 dap_line_time。値を読みに行った転送の線上の点 = 値を読んだ瞬間の窓の始め。各値に time_lo_us・time_hi_us = 値を読んだ瞬間の範囲、分からない端は null）/ value_returned（古い回路。値が返った周期）。

応答:

```ts
{ time_point: "read_request" | "value_returned"; values: Array<{ name: string; time_us: number | null; value: Value | null; time_lo_us?: number | null; time_hi_us?: number | null }> }
```

## watch.series

種類: `control`。変数 1 つの値の列（古い順。時刻はタイムベースの µs）。last はイベント記録から読む件数（最大 4096）。長い時間は recording_find({"stream": "sample", "a": 番地}) で。 値は `debug_evaluate` と同じ形（kind を持つ object）。番地の式（"0x20000000:4"）の値は kind = int（符号なし整数）。 time_point と各値の time_lo_us・time_hi_us は `watch_values` と同じ（線上の点の回路の最後の値の time_hi_us は次のサンプラの件が来るまで null）。

| 引数 | 型 | 必須 |
|---|---|---|
| `name` | `string` | ○ |
| `last` | `number` |  |

応答:

```ts
{ name: string; time_point: "read_request" | "value_returned"; samples: Array<{ time_us: number; value: Value; time_lo_us?: number | null; time_hi_us?: number | null }>; from_us: number | null }
```

## watch.xy

種類: `control`。2 つの変数の XY の組（古い順）。x の各値に、その時刻以前で最新の y の値を組む。2 つは同時に測っていないので、点ごとに x_time_us・y_time_us を出し、差の最大 dt_max_us を返す。unpaired = それ以前に y が無い x の数。max_dt_us を渡したときだけ、差がそれを超える組を外して excluded に数える（既定では外さない）。last・from_us（変数ごと）は watch_series と同じ。

| 引数 | 型 | 必須 |
|---|---|---|
| `x` | `string` | ○ |
| `y` | `string` | ○ |
| `last` | `number` |  |
| `max_dt_us` | `number` |  |

応答:

```ts
{ x: string; y: string; points: Array<{ x_time_us: number; x: Value; y_time_us: number; y: Value }>; dt_max_us: number | null; unpaired: number; excluded: number; max_dt_us: number | null; from_us: { [key: string]: number | null } }
```

## watch.measure

種類: `control`。カーソル A・B の測定（時刻はタイムベースの µs）。変数ごとに a・b = その時刻以前で最後の値 {time_us（その値の時刻。測った時刻とは別）, value}（無ければ null）、delta = b − a（どちらも数のとき）、stats = A〜B（両端を含む）の値の {count, min, max, mean, not_numeric}、first_us = デーモンが持っている最初の値の時刻（それより前は数えていない）。dt_us = b_us − a_us。source: live（イベント記録の最新の分。watch_series と同じ）か capture（watch_capture の結果）。names を省くと観測しているすべて。

| 引数 | 型 | 必須 |
|---|---|---|
| `a_us` | `number` | ○ |
| `b_us` | `number` | ○ |
| `names` | `Array<string>` |  |
| `source` | `"live" | "capture"` |  |

応答:

```ts
{ a_us: number; b_us: number; dt_us: number; rows: Array<{ name: string; a: { time_us: number; value: Value } | null; b: { time_us: number; value: Value } | null; delta: number | null; stats: { count: number; min: number | null; max: number | null; mean: number | null; not_numeric: number }; first_us: number | null }> }
```

## rtos.info

種類: `control`。ELF の RTOS の種類（freertos / zephyr / rtx5 / threadx / ucos3 / nuttx / null）と、接続していればスケジューラの状態・ティック・実行中のタスク（止まっていればタスクの数）。trace_hook はトレースフック（方法 itm の時間軸）の有無、timeline は取っている時間軸。FreeRTOS・ThreadX は port（保存された文脈の積み方）と port_basis（決めた根拠: vPortEnableVFP / explicit。決まらなければ port・port_basis は null）。

応答:

```ts
{ rtos: string | null; elf: string; trace_hook: boolean; timeline: RtosTimelineState | null; connected: boolean; name?: string; halted?: boolean; scheduler?: string; tick?: number | null; current?: { handle: number; name: string | null }; port?: string; port_basis?: string | null; tasks?: number }
```

## rtos.tasks

種類: `control`。タスクの一覧（名前・優先度・状態・待っているもの（キュー名・種類・ミューテックスの持ち主・残りのティック）・スタックの大きさ・いま／最大の使用量・あふれの印）。止まっているときだけ（`target_halt`）。live=true なら走行中にも読む（止めずに読むので一貫しない: consistent=false。実行中のタスクの SP は分からない）。構造体の位置は DWARF から。locations は値を読んだメモリの場所（priority・base_priority → {address, size, member}。`target_memory_write` で書ける）。

| 引数 | 型 | 必須 |
|---|---|---|
| `live` | `boolean` |  |

応答:

```ts
{ rtos: string; version?: string | null; port?: string | null; port_basis?: string | null; scheduler?: string; tick?: number | null; current: string | null; tasks: Array<RtosTask>; warnings: Array<string>; halted: boolean; consistent: boolean; notes?: Array<string> }
```

## rtos.objects

種類: `control`。カーネルのオブジェクトの一覧: FreeRTOS はキュー・セマフォ（数・最大）・ミューテックス（持ち主・継承中か）・タイマ（周期・動いているか・残りのティック・呼ぶ関数）・イベントグループ（ビット・待つビット）・ストリーム / メッセージバッファ（使用量・メッセージの数）、Zephyr は k_sem・k_mutex・k_msgq・k_event・k_timer・k_fifo 等、RTX5 は mutex・semaphore・event_flags・memory_pool・message_queue・timer、ThreadX は mutex・semaphore・queue・event_flags・block_pool・byte_pool・timer、µC/OS-III は semaphore・mutex・queue・event_flags・memory_pool・timer・cond、NuttX は semaphore・mutex・message_queue。それぞれ待っているタスク付き。登録簿に無い・名前の無いものも、変数の型（ハンドル）・待っているタスクから番地で出す（source: registry / kernel / variable / waiter）。ヒープの残量・最小残量（heap）と、止めた瞬間の優先度の逆転（継承の有無・間に動けるタスク）とデッドロック（contention）。kinds で種類を絞る（"semaphore" は binary・counting とも）。数えるのを辿る上限で止めたときは fields の *_truncated（items_truncated・messages_truncated・remaining_truncated）と、オブジェクトの notes に上限の値。変数の型で探すときに上限（入れ子の深さ・配列の要素・読むハンドルの数）・読めないことで見ていない所は warnings。止まっているときだけ（live=true で走行中にも。一貫しない: consistent=false）。

| 引数 | 型 | 必須 |
|---|---|---|
| `kinds` | `Array<string> | null` |  |
| `live` | `boolean` |  |

応答:

```ts
{ rtos: string; objects: Array<RtosObject>; heap: RtosHeap | null; contention: { inversions: Array<{ mutex: string; handle: number; holder: { task: string | null; handle: number; priority: number | null; base_priority: number | null }; waiter: { task: string; handle: number; priority: number | null }; inherited: boolean; interferers: Array<{ task: string; handle: number; priority: number; state: "running" | "ready" | "blocked" | "suspended" | "deleted" }> }>; deadlocks: Array<Array<{ task: string; handle: number; waits_for: string; held_by: string }>> }; priority_order: "higher_is_more_urgent" | "lower_is_more_urgent"; warnings: Array<string>; halted: boolean; consistent: boolean; notes?: Array<string> }
```

## rtos.backtrace

種類: `control`。タスク（名前か TCB の番地。"0x..." も可）の呼び出し履歴。止まっているタスクは保存された文脈から、実行中のタスクは実レジスタから巻き戻す。variables で各段の引数とローカル変数も（値は `debug_evaluate` と同じ形（kind を持つ object））。止まっているときだけ。port は保存された文脈の積み方（FreeRTOS: armv7m = ARM_CM3 等 / armv7m_fp = ARM_CM4F・ARM_CM7、ThreadX: armv6m / armv7m）。FreeRTOS は省けば ELF の事実（ARM_CM4F・ARM_CM7 の port の関数 vPortEnableVFP がある → armv7m_fp）だけで決め、決まらなければ止まっているタスクの文脈は誤りで port を渡すよう言う（`rtos_info` の port・port_basis が決めた積み方と根拠）。

| 引数 | 型 | 必須 |
|---|---|---|
| `task` | `string | number` | ○ |
| `variables` | `boolean` |  |
| `max_elements` | `number` |  |
| `port` | `"armv6m" | "armv7m" | "armv7m_fp"` |  |

応答:

```ts
{ task: string; handle: number; state: "running" | "ready" | "blocked" | "suspended" | "deleted"; frames: Array<{ index: number; pc: number; function: string | null; offset: number | null; file: string | null; line: number | null; cfa: number | null; note: "" | "exception" | "lr_without_cfi"; stop?: UnwindStop | { reason: "stop_pc"; pc: number }; arguments?: Array<Variable>; locals?: Array<Variable> }> }
```

## rtos.read

種類: `control`。タスクの文脈で C の式を読む（frame は `rtos_backtrace` の index。そのフレームの引数・ローカル変数も使える）。止まっているときだけ。port は保存された文脈の積み方（FreeRTOS: armv7m = ARM_CM3 等 / armv7m_fp = ARM_CM4F・ARM_CM7、ThreadX: armv6m / armv7m）。FreeRTOS は省けば ELF の事実（ARM_CM4F・ARM_CM7 の port の関数 vPortEnableVFP がある → armv7m_fp）だけで決め、決まらなければ止まっているタスクの文脈は誤りで port を渡すよう言う（`rtos_info` の port・port_basis が決めた積み方と根拠）。 値は `debug_evaluate` と同じ形（kind を持つ object）。

| 引数 | 型 | 必須 |
|---|---|---|
| `task` | `string | number` | ○ |
| `expression` | `string` | ○ |
| `frame` | `number` |  |
| `max_elements` | `number` |  |
| `port` | `"armv6m" | "armv7m" | "armv7m_fp"` |  |

応答:

```ts
{ task: string; expression: string; type: string; address?: number; value: Value }
```

## rtos.timeline.start

種類: `control`。タスクの時間軸を取り始める（止めずに見る）。method: sampled（組み込み無し。DAP サンプラが rate_hz で実行中のタスクの変数を読む。周期より短い実行は見えないことがあり、回数は下限。`watch_set` とサンプラを取り合う）/ itm（ファームのトレースフック fprobe_trace.h が ITM のポート 31 に書く語から。正確）。itm は swo_baud で SWO の受信も始め、core_clock_hz も渡すと TPIU の速度も設定する（SWO のピンはチップ固有: ファームか `target_peripheral_write`）。一緒に読むもの: memory=true（タスクの保存された SP とヒープの残量 → `rtos_timeline_memory`。tasks で絞る）、mutexes（true か名前の列: 持ち主と待ちの先頭 → `rtos_timeline_inversions`）、exceptions=true（itm だけ: 例外トレースも SWO に流す → 割り込みとタスクの合成 `rtos_timeline_profile`）。memory・mutexes は DAP サンプラの 8 か所に収める（sampled は同じサンプラ、itm は memory_rate_hz で）。記録中（`recording_start`）なら止めたときに記録の stream "rtos" に書く。 kernel=false（itm だけ）でカーネルの出来事（ポート 30・29）を止め、SWO の帯域をタスクの切り替えと例外に回す（overflows が出るとき）。 vector_table: ベクタ表の番地（使う側が明示する。大きさは vector_table_size か ELF のその番地の変数）。渡せば割り込みの区間に処理の関数の名前を付ける。渡さなければ番号の名前だけ（表の場所を推定しない）。

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
| `vector_table` | `number | null` |  |
| `vector_table_size` | `number | null` |  |

応答:

```ts
RtosTimelineState
```

## rtos.timeline.stop

種類: `control`。時間軸を取るのを止め、要約（タスクごとの合計・CPU の使用率・実行の回数・最長／最短・平均、切り替えの回数、分解能、回数が下限か、例外トレースの回数）を返す。記録中なら記録の stream "rtos" に run・switch・gap（itm は task_in 等の生の出来事も、exceptions なら割り込みの区間 isr も）を書く。止めた後も解析の道具（`rtos_timeline_load` 等）は最後の結果を使う。

応答:

```ts
RtosTimeline
```

## rtos.timeline.get

種類: `control`。時間軸（取っている最中ならいままでの分、止めた後なら最後の結果）: 要約と、区間（タスク・始め・終わり・不確かさ。時刻はタイムベース）の offset から limit 件と抜け（gap）。長い時間の区間は記録の `recording_find` で。 区間（runs）の uncertainty は切り替えで決まった端の不確かさ（両端とも観測の端・抜けで切った・分解能が分からなければ null）。stats の max_run・min_run・mean_run は両端が切り替えの区間だけ（無ければ null）、cut_runs・max_cut_run（切った区間の数と観測した長さの最大 = 下限）。resolution は分からなければ null（SWO の速さ・方式が分からない itm）。記録の stream rtos の run の本体に start_cut・end_cut。

| 引数 | 型 | 必須 |
|---|---|---|
| `offset` | `number` |  |
| `limit` | `number` |  |

応答:

```ts
RtosTimeline
```

## rtos.timeline.load

種類: `control`。窓（window_us）ごとの CPU 負荷（1 − アイドルの割合。割り込みは負荷に入る）・タスクごとの割合・割り込みの割合・その他（other = 負荷のうちタスクにも割り込みにも入らない分）の推移と、最大の窓（いつ）・平均。窓が limit を超えたら先頭の limit 個（windows_total = 全体の数、limit = 使った上限）。情報の無い時間（抜け）は除く。割り込みをトレースフックの isr_enter / isr_exit から取ったときは、対にできなかった数を isr_unpaired（missing_exit・missing_enter・lost_open）に。取っている最中ならいままでの分、止めた後なら最後の結果。

| 引数 | 型 | 必須 |
|---|---|---|
| `window_us` | `number` |  |
| `limit` | `number` |  |

応答:

```ts
{ running: boolean; method: string; timebase_hz: number; t0: number; t1: number; resolution_us: number | null; notes: Array<string>; isr_unpaired?: { missing_exit: number; missing_enter: number; lost_open: number }; window: number; window_us: number; windows: Array<{ t0: number; t1: number; covered: number; load: number | null; isr: number | null; other: number | null; tasks: { [key: string]: number } }>; peak: null | { t0: number; t1: number; load: number }; mean_load: number | null; idle_tasks?: Array<string>; windows_total: number; limit: number }
```

## rtos.timeline.jobs

種類: `control`。tasks: カーネルの出来事（方法 itm のトレースフックの ready / block）で区切ったジョブ（起きてから待つまでの 1 回の仕事）ごとの実行時間（横取り・割り込みを除く）・経過時間・周期・応答時間の分布（最小・最大・平均・σ・p90・p99）と最悪の回（いつ）。出来事の無いタスクのジョブは作らない（次に動いたタスクの優先度から横取りか待ちかを推定しない）。runs: タスクごとの区間（続けて動いた間）の回数・自分の時間（割り込みを除く）・周期の分布、最長の回、次に動いたタスクの回数（followed_by。抜けは (gap)、観測の端は (end)）。task を渡すとそのタスクのジョブ（jobs）と区間（task_runs）の一覧（自分の時間の長い順に limit 件）も。

| 引数 | 型 | 必須 |
|---|---|---|
| `task` | `string | null` |  |
| `limit` | `number` |  |

応答:

```ts
{ running: boolean; method: string; timebase_hz: number; t0: number; t1: number; resolution_us: number | null; notes: Array<string>; isr_unpaired?: { missing_exit: number; missing_enter: number; lost_open: number }; tasks: { [key: string]: { handle: number; jobs: number; truncated: number; preemptions: number; exec: Distribution; wall: Distribution; period: Distribution; response: Distribution | null; worst_exec?: { start: number; end: number; exec: number; wall: number }; worst_wall?: { start: number; end: number; exec: number; wall: number }; worst_response?: { release: number; end: number; response: number }; exec_us?: Distribution | null; wall_us?: Distribution | null; period_us?: Distribution | null; response_us?: Distribution | null } }; runs: { [key: string]: { handle: number; runs: number; exec: Distribution; period: Distribution; worst_exec: { start: number; end: number; exec: number }; followed_by: { [key: string]: number }; exec_us?: Distribution | null; period_us?: Distribution | null } }; jobs_total?: number; jobs?: Array<{ task: string; handle: number; start: number; end: number; exec: number; wall: number; preemptions: number; end_reason: "blocked" | "truncated"; release?: number; response?: number }>; task_runs_total?: number; task_runs?: Array<{ start: number; end: number; exec: number }> }
```

## rtos.timeline.interval

種類: `control`。記録の 2 点（start / end は `recording_find` と同じ条件。`timing_measure` と同じ組の規則）の各組で、タスク（省けば開始の時刻に動いていたタスク。task は名前か TCB の番地 "0x..."。同じ名前のタスクが複数なら誤りで番地を挙げる）が実際に CPU を使った時間（横取り・割り込みを除く）と経過時間の分布。記録（`recording_start` か `recording_open`）が要る。

| 引数 | 型 | 必須 |
|---|---|---|
| `start` | `Record<string, unknown>` | ○ |
| `end` | `Record<string, unknown>` | ○ |
| `task` | `string | null` |  |
| `limit` | `number` |  |

応答:

```ts
{ running: boolean; method: string; timebase_hz: number; t0: number; t1: number; resolution_us: number | null; notes: Array<string>; isr_unpaired?: { missing_exit: number; missing_enter: number; lost_open: number }; count: number; incomplete: number; wall: Distribution; own: Distribution; intervals: Array<{ t: number; task: string; wall: number; own: number; preempted: number; incomplete?: true }>; wall_us?: Distribution | null; own_us?: Distribution | null; intervals_total?: number }
```

## rtos.timeline.memory

種類: `control`。タスクごとのスタックの使用量（保存された SP から: 切り替えのたびの深さ。最大・最小・最後・あふれの印）とヒープの残量（最小・最後）の推移。points 個の窓ごとに、その窓のサンプルの最悪の値（サンプルの無い窓には点が無い。前の値を続けない）。`rtos_timeline_start` の memory=true が要る。

| 引数 | 型 | 必須 |
|---|---|---|
| `points` | `number` |  |

応答:

```ts
{ running: boolean; method: string; timebase_hz: number; t0: number; t1: number; resolution_us: number | null; notes: Array<string>; isr_unpaired?: { missing_exit: number; missing_enter: number; lost_open: number }; stacks: { [key: string]: { handle: number | null; size: number | null; max_used: number; min_used: number; last_used: number; max_at: number; points: Array<[number, number]>; overflow: boolean } }; heap: { total: number | null; min_free: number; max_free: number; last_free: number; min_at: number; points: Array<[number, number]> } | null; points_width?: number; points_width_us?: number | null }
```

## rtos.timeline.profile

種類: `control`。実行のプロファイル: 情報のある時間に対するスレッド（タスクごと。割り込みの時間を除く）・割り込み（例外ごとの回数・自分の時間）・アイドル・どれでもない時間の割合。割り込みは方法 itm の exceptions=true のとき（それ以外は interrupts = null で、割り込みはタスクの時間に含まれる）。

応答:

```ts
{ running: boolean; method: string; timebase_hz: number; t0: number; t1: number; resolution_us: number | null; notes: Array<string>; isr_unpaired?: { missing_exit: number; missing_enter: number; lost_open: number }; covered: number; threads: { [key: string]: { handle: number; time: number; idle: boolean; share: number | null } }; interrupts: { [key: string]: { count: number; time: number; share: number | null } } | null; share: { thread: number; interrupt: number; idle: number; unattributed: number } | null; covered_us?: number | null; exception_trace?: { inconsistencies: number; overflows: number } }
```

## rtos.timeline.inversions

種類: `control`。優先度の逆転の区間: 持ち主の元の優先度より高いタスクがミューテックスを待っていた区間ごとに、持ち主・待ち手・長さ・持ち主が動いた時間・その間に動いた中間の優先度のタスクの時間（interference。interfered はそれが正か、要約の interfered はその回数。継承が効いたか・際限が無いかの判定は付けない）。方法 itm でカーネルの出来事があれば、それで正確に（source = trace。継承の時刻 inherited も）。無ければ `rtos_timeline_start` の mutexes で読んだ語から（source = sampled。サンプラの周期より短い逆転は見えないことがある。読んでいなければ available=false。推測しない）。

| 引数 | 型 | 必須 |
|---|---|---|
| `limit` | `number` |  |

応答:

```ts
{ running: boolean; method: string; timebase_hz: number; t0: number; t1: number; resolution_us: number | null; notes: Array<string>; isr_unpaired?: { missing_exit: number; missing_enter: number; lost_open: number }; available: false; episodes: Array<{ mutex: string; holder: string; waiter: string; t0: number; t1: number; duration: number; holder_time: number; interference: { [key: string]: number }; interfered: boolean; truncated?: true; inherited?: { t: number; priority: number; latency: number } | null; opened_reads?: { holder_t: number; head_t: number }; pair_read_span?: number; pair_read_span_us?: number }>; count: 0; reason: string; source?: "sampled" } | { running: boolean; method: string; timebase_hz: number; t0: number; t1: number; resolution_us: number | null; notes: Array<string>; isr_unpaired?: { missing_exit: number; missing_enter: number; lost_open: number }; available: true; count: number; interfered: number; total: number; duration: Distribution; episodes: Array<{ mutex: string; holder: string; waiter: string; t0: number; t1: number; duration: number; holder_time: number; interference: { [key: string]: number }; interfered: boolean; truncated?: true; inherited?: { t: number; priority: number; latency: number } | null; opened_reads?: { holder_t: number; head_t: number }; pair_read_span?: number; pair_read_span_us?: number }>; max: { mutex: string; holder: string; waiter: string; t0: number; t1: number; duration: number; holder_time: number; interference: { [key: string]: number }; interfered: boolean; truncated?: true; inherited?: { t: number; priority: number; latency: number } | null; opened_reads?: { holder_t: number; head_t: number }; pair_read_span?: number; pair_read_span_us?: number } | null; duration_us?: Distribution | null; source: "trace" | "sampled"; episodes_total?: number }
```

## rtos.timeline.events

種類: `control`。カーネルの出来事の一覧（方法 itm でファームの fprobe_trace.h のフックがあるとき）: キュー・セマフォ・ミューテックスの送り・受け（give・take）と失敗・待ち（block、wait = receive/send/peek/notify）・準備完了（ready）・delay・suspend・優先度の継承と変更（prio_*）・通知・イベントグループ・ストリームバッファ・割り込みの出入り。actor は行為者のタスク（割り込みの中なら isr）、object はオブジェクトの名前、arg は操作の前のメッセージ数等。kinds・task（行為者か対象のタスク）・object で絞り、offset から limit 件。

| 引数 | 型 | 必須 |
|---|---|---|
| `kinds` | `Array<string> | null` |  |
| `task` | `string | null` |  |
| `object` | `string | null` |  |
| `offset` | `number` |  |
| `limit` | `number` |  |

応答:

```ts
{ running: boolean; method: string; timebase_hz: number; t0: number; t1: number; resolution_us: number | null; notes: Array<string>; isr_unpaired?: { missing_exit: number; missing_enter: number; lost_open: number }; kernel: { events: number; lost_records: number; gaps: number }; total: number; offset: number; events: Array<{ t: number; kind: "block" | "delay" | "event_set" | "isr_enter" | "isr_exit" | "notify" | "object_create" | "peek" | "prio_disinherit" | "prio_inherit" | "prio_set" | "ready" | "receive" | "receive_failed" | "send" | "send_failed" | "stream_receive" | "stream_send" | "suspend"; actor: string | null; isr: string | null; object?: string | null; object_handle?: number; class?: string | null; task?: string; task_handle?: number; arg?: number; wait?: "receive" | "send" | "peek" | "notify"; length?: number | null; type?: string | null; saturated?: true }> }
```

## rtos.timeline.flow

種類: `control`。Communication Flow: タスク・割り込み・オブジェクトの有向グラフ（カーネルの出来事から）。辺は send・give（行為者 → オブジェクト）、receive・take・peek（オブジェクト → 行為者）、notify（行為者 → タスク）、set（イベントグループ）、wait（待った）と回数（count）・失敗の回数（failed）・ストリームのバイト数。dot は Graphviz の形。

応答:

```ts
{ running: boolean; method: string; timebase_hz: number; t0: number; t1: number; resolution_us: number | null; notes: Array<string>; isr_unpaired?: { missing_exit: number; missing_enter: number; lost_open: number }; kernel: { events: number; lost_records: number; gaps: number }; nodes: Array<{ id: string; type: "task"; name: string; handle: number } | { id: string; type: "isr"; name: string; number: number } | { id: string; type: "object"; name: string; handle: number; kind: string } | { id: string; type: "unknown"; name: string }>; edges: Array<{ from: string; to: string; op: "give" | "send" | "take" | "receive" | "peek" | "set" | "notify" | "wait"; count: number; failed: number; bytes?: number }>; dot: string; lost: number }
```

## rtos.timeline.blocking

種類: `control`。ブロック時間（カーネルの出来事から）: 待ちの区間（block = オブジェクト・通知を待った / delay / suspend から ready まで）と outcome（起きたタスクの次の操作が同じオブジェクトへの成功 = ok、失敗 = failed。次の操作が別のもの・無ければ null。失敗の理由は推定しない）。タスクごと・オブジェクトごとの回数・合計・平均・最長（いつ）・失敗の回数。task で区間の一覧を絞る。

| 引数 | 型 | 必須 |
|---|---|---|
| `task` | `string | null` |  |
| `limit` | `number` |  |

応答:

```ts
{ running: boolean; method: string; timebase_hz: number; t0: number; t1: number; resolution_us: number | null; notes: Array<string>; isr_unpaired?: { missing_exit: number; missing_enter: number; lost_open: number }; kernel: { events: number; lost_records: number; gaps: number }; tasks: { [key: string]: { count: number; total: number; mean: number; max: { t0: number; t1: number; duration: number; on: string; task: string; duration_us?: number }; failed: number; by_on: { [key: string]: { count: number; total: number } }; total_us?: number } }; objects: { [key: string]: { count: number; total: number; mean: number; max: { t0: number; t1: number; duration: number; on: string; task: string; duration_us?: number }; failed: number; by_task: { [key: string]: { count: number; total: number } }; total_us?: number } }; open: number; forgotten: number; waits_total: number; waits: Array<{ task: string; handle: number; on: string; object_handle: number | null; wait: string | null; t0: number; t1: number; duration: number; outcome: "ok" | "failed" | null; truncated?: true }> }
```

## rtos.timeline.queues

種類: `control`。キュー使用率（カーネルの出来事から）: キュー・セマフォごとのメッセージ数（数）の推移（points 点まで、山を落とさない）・最大・時間で重みを付けた平均・長さに対する割合（max_usage・mean_usage）、満杯（send_failed）・空（receive_failed）・待ち（waits_send・waits_receive）の回数。長さは始めに読んだオブジェクトの一覧から。

| 引数 | 型 | 必須 |
|---|---|---|
| `points` | `number` |  |

応答:

```ts
{ running: boolean; method: string; timebase_hz: number; t0: number; t1: number; resolution_us: number | null; notes: Array<string>; isr_unpaired?: { missing_exit: number; missing_enter: number; lost_open: number }; kernel: { events: number; lost_records: number; gaps: number }; objects: { [key: string]: { handle: number; kind: string; length: number | null; sends: number; receives: number; send_failed: number; receive_failed: number; waits_send: number; waits_receive: number; saturated?: true; max: number | null; min: number | null; last: number | null; mean: number | null; mean_t1: number | null; points: Array<[number, number]>; points_total: number; max_usage: number | null; mean_usage: number | null } }; lost: number }
```

## rtos.timeline.overhead

種類: `control`。トレースフックの負荷: 語の数（ポート 31・見出し 30・中身 29）・出来事の種類ごとの数・SWO のバイト数と帯域の使用率（swo_share = 送ったビット ÷ 帯域）。数えた事実だけで、CPU の負荷は推定しない（実測はトレースの有無でアイドルの回る数を比べる）。

応答:

```ts
{ running: boolean; method: string; timebase_hz: number; t0: number; t1: number; resolution_us: number | null; notes: Array<string>; isr_unpaired?: { missing_exit: number; missing_enter: number; lost_open: number }; kernel: { events: number; lost_records: number; gaps: number }; words: number; words_by_port: { task: number; kernel: number; payload: number }; bytes: number; seconds: number; words_per_s: number | null; by_kind: { [key: string]: number }; lost: number; swo_share: number | null }
```

## profile.start

種類: `control`。PC サンプリングのプロファイルを始める。method: itm（DWT の PC サンプリング → SWO。core_clock_hz が要る。exception_trace で割り込みの時間も。DWT・ITM・TPIU を設定して SWO の受信も始める。SWO のピンはチップ固有: ファームか `target_peripheral_write`）/ pcsr（DAP サンプラで DWT_PCSR を読む。SWO 不要・Cortex-M0+ も可・遅い。`watch_set` とサンプラを取り合う）。timestamps は省略で exception_trace と同じ。exception_rate_hz = SWO の帯域の見積もりに入れる 1 秒あたりの例外の回数（使う側が明示する。省略 = 見積もりに入れず、計画の warnings に書く。計画の exception_rate_hz が使った値）。計画（実際のサンプル率・SWO の使用率・警告・書いたレジスタ）を返す。pcsr の速さは SWD の速さで決まる（4 MHz で 1 巡回 約 110〜120 µs = 約 8〜9 kHz）: 計画の swd に 1 巡回の見積もり（swd_hz・swclk_hz・round_us・max_rate_hz）と、求めた rate_hz に要る SWD の速さ（swd_hz_for_rate。target_connect の frequency_hz に渡す。null は最も速い SWD でも届かない）。rate_hz は出せる見込み、requested_rate_hz は求めた値。 vector_table: ベクタ表の番地（使う側が明示する。大きさは vector_table_size か ELF のその番地の変数）。渡せば割り込みに処理の関数の名前を付ける（"IRQ1 (usart_irq)"）。渡さなければ番号の名前だけ（表の場所を推定しない）。

| 引数 | 型 | 必須 |
|---|---|---|
| `method` | `"itm" | "pcsr"` |  |
| `rate_hz` | `number` |  |
| `core_clock_hz` | `number | null` |  |
| `swo_baud` | `number` |  |
| `exception_trace` | `boolean` |  |
| `exception_rate_hz` | `number | null` |  |
| `timestamps` | `boolean | null` |  |
| `ap` | `number` |  |
| `vector_table` | `number | null` |  |
| `vector_table_size` | `number | null` |  |

応答:

```ts
{ running: true; method: "itm" | "pcsr"; plan: SamplingPlan }
```

## profile.stop

種類: `control`。PC サンプリングを止めてプロファイルにする（DWT_CTRL を元に戻す）。要約（分類: code / sleep / unknown、上位 top の関数のサンプル数と割合、割り込みごとの回数）を返す。詳しくは `profile_result` / `profile_coverage` / `profile_exceptions`。pcsr なら sampling（requested_hz・expected_hz・achieved_hz = 読みの時刻からの実際の速さ・SWD の速さ・swd_hz_for_rate）が付き、追いつかなければ notes に要る SWD の速さ。

| 引数 | 型 | 必須 |
|---|---|---|
| `top` | `number` |  |

応答:

```ts
ProfileSummary
```

## profile.result

種類: `control`。プロファイルの上位 top（取っている最中ならいままでの分）。by: function / line / instruction / exception。line・instruction は function で絞れる。行はサンプル数 samples と割合 fraction（自分の時間）。信頼区間は付けない（独立な二項の試行という仮定の上の推定で、周期の抽出では成り立たない。仮定を承知で付けるならプラグイン plugins.heuristics.sampling_intervals）。 itm の例外は先頭 top 件（exceptions_total = 切る前の数）。

| 引数 | 型 | 必須 |
|---|---|---|
| `by` | `"function" | "line" | "instruction" | "exception"` |  |
| `top` | `number` |  |
| `function` | `string | null` |  |
| `order` | `"samples" | "line"` |  |

応答:

```ts
ProfileSummary
```

## profile.coverage

種類: `control`。PC サンプルが当たった命令・行からの、関数ごとの実行された行の割合と当たらなかった行（近似: 当たらなかった行が実行されなかったとは限らない）。functions を省くと当たった関数（all_functions なら ELF の全関数）。

| 引数 | 型 | 必須 |
|---|---|---|
| `functions` | `Array<string> | null` |  |
| `all_functions` | `boolean` |  |

応答:

```ts
{ approximate: true; notes: Array<string>; hit_addresses: number; lines: number; lines_hit: number; line_fraction: number; functions: Array<{ function: string; address: number; size: number; instructions: number; instructions_hit: number; instruction_fraction: number; lines: number; lines_hit: number; line_fraction: number; hit_lines: Array<[string, number]>; missed_lines: Array<[string, number]> }> }
```

## profile.exceptions

種類: `control`。割り込みごとの回数・時間（含む／自分。最小・最大・平均・σ）・最大の回の時刻・負荷（ITM のローカルの時刻 = コアのクロック数。clock_hz で秒にも）。method itm と exception_trace が要る。

応答:

```ts
{ span: number | null; clock_hz: number | null; overflows: number; inconsistencies: number; exceptions: Array<ExceptionStats>; notes?: Array<string> }
```

## cycles.read

種類: `control`。DWT の CYCCNT（32 ビット）と 8 ビットのイベントカウンタ（cpi・exc・sleep・lsu・fold）のいまの値と、読んだときのタイムベースの時刻。何も変えない。

応答:

```ts
{ cyccnt: number; cyccnt_enabled: boolean; time: number; counters?: { cpi: number; exc: number; sleep: number; lsu: number; fold: number } }
```

## cycles.measure

種類: `control`。start から end（関数名・"ファイル:行"・"0x..."）までのサイクルを count 回測る（ブレークポイントを置いて走らせ、止まるたびに CYCCNT を読む。止まっている間は数えない。置いたものは外す）。counters ならCPI・EXC・SLEEP・LSU・FOLD の増分と命令数・IPC も（`swo.configure` の counters で溢れを数えていれば正確、そうでなければ増分は 256 で割った余り = counters_exact false。本当の増分は 256 の倍数だけ多いこともある。数えた溢れと値が合わなかったカウンタは notes に）。core_clock_hz で µs も。終わるとターゲットは end で止まっている。

| 引数 | 型 | 必須 |
|---|---|---|
| `start` | `string` | ○ |
| `end` | `string` | ○ |
| `count` | `number` |  |
| `timeout_s` | `number` |  |
| `core_clock_hz` | `number | null` |  |
| `counters` | `boolean` |  |

応答:

```ts
{ intervals: Array<number>; summary: { count: number; min: number; max: number; mean: number; median: number; stdev: number; time_us?: { min: number; max: number; mean: number; median: number; stdev: number } }; start: { address: number; where: string; function?: string; file?: string; line?: number }; end: { address: number; where: string; function?: string; file?: string; line?: number }; notes: Array<string>; counters?: Array<{ cpi: number; exc: number; sleep: number; lsu: number; fold: number; cycles: number; instructions: number; ipc: number | null }>; counters_exact?: boolean }
```

## cycles.clock

種類: `control`。CPU クロックを実測する（コアが走っている間に CYCCNT の進みをデバイスのタイムベースと比べる。DAP サンプラが使えれば rate_hz で duration_s の間読んだ直線の傾き、ほかの道具が使っていれば前後 2 回の読み）。CYCCNT を有効にする。clock_hz（直線の傾き、または 2 回の読みの中点どうし）・直線からの残差の最大と二乗平均（max_residual_cycles・residual_rms_cycles）・2 回の読みなら前後の時刻で挟んだ範囲（clock_min_hz〜clock_max_hz）・止まった区間の警告を返す（標準誤差は残差が独立という仮定の上の推定なので出さない）。

| 引数 | 型 | 必須 |
|---|---|---|
| `duration_s` | `number` |  |
| `rate_hz` | `number` |  |
| `ap` | `number` |  |

応答:

```ts
{ method: "two_reads"; clock_hz: number; clock_mhz: number; samples: number; span_s: number; max_residual_cycles: number | null; residual_rms_cycles: number | null; clock_min_hz: number | null; clock_max_hz: number | null; warnings: Array<string> } | { method: "sampler"; rate_hz: number; clock_hz: number; clock_mhz: number; samples: number; span_s: number; max_residual_cycles: number | null; residual_rms_cycles: number | null; clock_min_hz: number | null; clock_max_hz: number | null; warnings: Array<string> }
```

## timing.measure

種類: `read`。記録（記録中のもの、または開いたもの）の 2 点間の時間の統計（最小・最大・平均・σ・中央値・p90・p99）・分布（histogram: bins 個の等しい幅の箱）・長い順の上位 longest 件（外れ値の経験則は使わない。各 bin = 入る箱の番号）。start / end は `recording_find` と同じ条件。長い順の上位と min_at・max_at の時刻は `recording_events` で開ける。生の組 intervals（[開始の時刻, 時間]）と同じ並びの箱の番号 interval_bins は limit 件まで（切る前の数は intervals_total）。

| 引数 | 型 | 必須 |
|---|---|---|
| `start` | `Record<string, unknown>` | ○ |
| `end` | `Record<string, unknown>` | ○ |
| `bins` | `number` |  |
| `limit` | `number` |  |
| `longest` | `number` |  |

応答:

```ts
IntervalReport
```

## testpoint.instrument

種類: `read`。テストポイント（DT+ 方式）の挿入: C のソース（source_dir の下の *.c）に出力のコードを入れた写しを out_dir に作り、ID の対応表（map_path、既定 out_dir/testpoints.json）を書く。写しの行番号は元と同じ（#line）。対応表が既にあれば中身の変わらない関数の ID を保つ（差分の再挿入。renumber で振り直す）。only_changed は前回（baseline）から変わった・新しい関数にだけ入れる。variables は書き込みを記録する変数（"name" か "name:novalue"）、include / exclude は関数名の glob、branches=false で関数の入口・出口と変数だけ。写しは -DFPROBE_TP_BACKEND=（GPIO_SERIAL / GPIO_PARALLEL / ITM / RTT。fprobe_tp.h）でビルドする。機器に触らない。以後の `testpoint_start` の既定の対応表。戻り値は出口の前に宣言どおりの型の一時変数に置く（型が宣言から取れない複雑な宣言子だけ __auto_type）。non_type_words は戻り値の型の中の型でない語（記憶域・属性のマクロ等。例 STATIC_INLINE）で、一時変数の型から除く（語がマクロかは名前の形で決めない）。

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
| `non_type_words` | `Array<string>` |  |

応答:

```ts
{ map: string; probes: number; next_id: number; header: string; files: Array<{ path: string; probes: number; instrumented: Array<string>; reused: Array<string>; changed: Array<string>; skipped: Array<{ function: string; line: number; reason: string }>; warnings: Array<string>; error: string | null }>; removed: Array<string>; written: Array<string> }
```

## testpoint.start

種類: `control`。テストポイントの受け取りを始める（すぐ戻る）。method: gpio_serial（2 線。clock_channel・data_channel・guard_samples = 開始・終了を認めるまで CLK が 1 のままのサンプル数 = CLK と DAT のずれを許す数。必須で既定は無い）/ gpio_parallel（data_channels = D0..D(W-1)、最後が先頭の印。strobe_channel）はロジアナで、capture deep（深いバッファ。trigger なら CLK / ストローブの最初の立ち上がりから after_beats 拍。止めたときに読む）か stream（流し続ける捕捉を背景で解読・解析する。長時間向け。メモリは一定。サンプル 0 の時刻は `recording_la_stream_start` の anchor と同じ決め方で、settings.anchor に ticks・uncertainty_ticks・method・t_lo・t_hi・basis）。itm は SWO（port のスティミュラス・ポート。swo_baud で受信を始め、core_clock_hz も渡すと TPIU も設定。受信を始めてからポートを有効にし、止めると元に戻す。時刻は FPGA が SWO のバイトに付けた時刻 = 出来事の上限。time_map の bound = upper・uncertainty_ticks = null（8N1 を仮定しない）。rtt は RTT の上りの rtt_channel（core_clock_hz = CYCCNT の速さが要る。時刻はホストの時計で写すので不確かさ付き。最初のかたまりの最後の記録を読んだ時刻を上限とする（time_map の bound = upper、幅は PC の時計の写しだけで読みの周期を足さない））。map_path を省くと最後の `testpoint_instrument` の対応表。duration_s を渡すと `testpoint_stop` はそこまで待つ（stream は自分で終わる）。silence_s（これより長く何も来ない）・open_s（これより長く出口が無い）・long_s（これより長い呼び出し）は、明示したときだけ付ける印（経験則の「いつもより長い」は付けない。いちばん長い呼び出しは統計の max_s・max_at）。記録中（`recording_start`）なら stream "testpoint"（call / hit / anomaly）に書く。

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
| `long_s` | `number | null` |  |
| `guard_samples` | `number` |  |
| `tree_max_depth` | `number` |  |

応答:

```ts
{ running: boolean; method: "gpio_serial" | "gpio_parallel" | "itm" | "rtt"; map: string; hz: number; settings: { [key: string]: Json }; hits: number; losses: number; elapsed_s: number; recorded: boolean; warnings: Array<string>; skipped_before_sync?: number; capture?: "deep" | "stream"; duration_s?: number; error?: string; unsynced_symbols?: number; cyccnt_wraps?: number; stream?: { samples: number; duration_s: number; mb_per_s: number; overrun: boolean; link_buffer: StreamBuffer | null }; window?: EventWindowInfo; time_map?: { timebase_hz: number; offset: number; uncertainty_ticks: number | null; bound: string | null } }
```

## testpoint.stop

種類: `control`。テストポイントの受け取りを止めて解析を終える（duration_s を渡して始めたならそこまで待つ。deep は深いバッファが埋まるのを timeout_s まで待って読む。捕捉は `la_view` と記録の波形にも残る）。要約（上位 top の関数・カバレッジ・異常）を返す。詳しくは `testpoint_report` / `testpoint_calls` / `testpoint_coverage`。

| 引数 | 型 | 必須 |
|---|---|---|
| `timeout_s` | `number` |  |
| `top` | `number` |  |

応答:

```ts
{ running: boolean; method: "gpio_serial" | "gpio_parallel" | "itm" | "rtt"; map: string; hz: number; settings: { [key: string]: Json }; hits: number; losses: number; elapsed_s: number; recorded: boolean; warnings: Array<string>; skipped_before_sync?: number; capture?: "deep" | "stream"; duration_s?: number; error?: string; unsynced_symbols?: number; cyccnt_wraps?: number; stream?: { samples: number; duration_s: number; mb_per_s: number; overrun: boolean; link_buffer: StreamBuffer | null }; window?: EventWindowInfo; time_map?: { timebase_hz: number; offset: number; uncertainty_ticks: number | null; bound: string | null }; span_s: number | null; functions_total: number; functions: Array<{ name: string; calls: number; inclusive: { count: number; total_s?: number; min_s?: number; max_s?: number; max_at?: number | null; mean_s?: number; stdev_s?: number }; exclusive: { count: number; total_s?: number; min_s?: number; max_s?: number; max_at?: number | null; mean_s?: number; stdev_s?: number }; total_inclusive_s: number }>; coverage: { functions: [number, number]; c0: [number, number]; c1: [number, number]; function_ratio: number | null; c0_ratio: number | null; c1_ratio: number | null; uncovered_total: number }; anomaly_counts: { [key: string]: number }; anomalies_total: number; anomalies: Array<{ t_s: number; t?: number; type: string; id: number | null; function: string | null; detail: string }>; bucket_s: number; buckets_total: number; buckets: Array<{ t_s: number; t?: number; hits: number; calls: number; anomalies: number }>; variables: Array<{ id: number; variable: string; function: string; count: number; last: number | null; min: number | null; max: number | null }>; open: Array<{ function: string; t_s: number; t?: number }>; tree: TestpointTree; tree_max_depth: number; tree_cut: number; limits: { top: number; anomalies: number; buckets: number; tree_depth: number } }
```

## testpoint.status

種類: `control`。テストポイントの受け取りの様子（方式・設定・受けた印の数・取りこぼし・経過・流し続ける捕捉の速さと overrun・時刻の写し・警告。方式 rtt は cyccnt_wraps = CYCCNT の回り込みを「記録の間が 2^32 クロックより短い」前提で戻した回数）。

応答:

```ts
{ running: false } | { running: boolean; method: "gpio_serial" | "gpio_parallel" | "itm" | "rtt"; map: string; hz: number; settings: { [key: string]: Json }; hits: number; losses: number; elapsed_s: number; recorded: boolean; warnings: Array<string>; skipped_before_sync?: number; capture?: "deep" | "stream"; duration_s?: number; error?: string; unsynced_symbols?: number; cyccnt_wraps?: number; stream?: { samples: number; duration_s: number; mb_per_s: number; overrun: boolean; link_buffer: StreamBuffer | null }; window?: EventWindowInfo; time_map?: { timebase_hz: number; offset: number; uncertainty_ticks: number | null; bound: string | null } }
```

## testpoint.report

種類: `control`。テストポイントの解析の要約（受け取っている最中ならいままでの分）: 関数ごとの回数・子を含む／自分だけの時間（合計・最小・最大・平均・σ）を多い順に top、カバレッジの割合（関数・C0・C1）、異常（long_call / missing_exit / exit_without_entry / unknown_id / silence / no_exit / loss）の種類ごとの数と先頭 anomalies 件、時間の区切りごとの件数の最後の buckets 個、変数（最新・最小・最大）、開いたままの呼び出し、呼び出しの木（深さ tree_depth まで。それより下は節の children_omitted）。切る前の数は functions_total・anomalies_total・buckets_total、使った件数は limits。解析の木の深さの上限 tree_max_depth より深くて木に入れなかった呼び出しの数は tree_cut（節ごとは cut。その時間は節の inclusive に入る）。t_s は最初の印からの秒、t はタイムベース（`recording_events` 等の時刻）。

| 引数 | 型 | 必須 |
|---|---|---|
| `top` | `number` |  |
| `anomalies` | `number` |  |
| `buckets` | `number` |  |
| `tree_depth` | `number` |  |

応答:

```ts
{ running: boolean; method: "gpio_serial" | "gpio_parallel" | "itm" | "rtt"; hz: number; hits: number; losses: number; span_s: number | null; functions_total: number; functions: Array<{ name: string; calls: number; inclusive: { count: number; total_s?: number; min_s?: number; max_s?: number; max_at?: number | null; mean_s?: number; stdev_s?: number }; exclusive: { count: number; total_s?: number; min_s?: number; max_s?: number; max_at?: number | null; mean_s?: number; stdev_s?: number }; total_inclusive_s: number }>; coverage: { functions: [number, number]; c0: [number, number]; c1: [number, number]; function_ratio: number | null; c0_ratio: number | null; c1_ratio: number | null; uncovered_total: number }; anomaly_counts: { [key: string]: number }; anomalies_total: number; anomalies: Array<{ t_s: number; t?: number; type: string; id: number | null; function: string | null; detail: string }>; bucket_s: number; buckets_total: number; buckets: Array<{ t_s: number; t?: number; hits: number; calls: number; anomalies: number }>; variables: Array<{ id: number; variable: string; function: string; count: number; last: number | null; min: number | null; max: number | null }>; open: Array<{ function: string; t_s: number; t?: number }>; tree: TestpointTree; tree_max_depth: number; tree_cut: number; limits: { top: number; anomalies: number; buckets: number; tree_depth: number }; time_map?: { timebase_hz: number; offset: number; uncertainty_ticks: number | null; bound: string | null } }
```

## testpoint.calls

種類: `control`。テストポイントの関数の遷移の時間軸: 呼び出しを始まりの順に（関数・始まり t_s / t・時間・自分だけの時間・深さ・出口を見たか）と、深さで字下げした文字列 text。t0_s ≤ 始まり < t1_s（最初の印からの秒）・function で絞り、先頭 limit 件（total・truncated と使った limit を返す）。持っているのは最近の 65536 件（dropped はそれより前に捨てた数。全部は記録の stream "testpoint" の call）。

| 引数 | 型 | 必須 |
|---|---|---|
| `t0_s` | `number | null` |  |
| `t1_s` | `number | null` |  |
| `function` | `string | null` |  |
| `limit` | `number` |  |

応答:

```ts
{ total: number; truncated: boolean; limit: number; dropped: number; calls: Array<{ function: string; t_s: number; t?: number; duration_us: number; self_us: number; depth: number; complete: boolean; entry_id: number | null; exit_id: number | null }>; text: string }
```

## testpoint.coverage

種類: `control`。テストポイントのカバレッジ（実行の記録に基づく正確なもの）: 関数（入った / 入れた）・C0（ソースにある塊の印）・C1（分岐の組の結果。足した else・default を含む）の割合と、通らなかった印（ファイル・行・関数・種類。試験を足す手がかり）、分岐の組ごとの結果の回数。file（対応表の相対パス）・function で絞り、先頭 limit 件（uncovered_total・decisions_total と使った limit を返す）。

| 引数 | 型 | 必須 |
|---|---|---|
| `file` | `string | null` |  |
| `function` | `string | null` |  |
| `limit` | `number` |  |

応答:

```ts
{ running: boolean; functions: [number, number]; c0: [number, number]; c1: [number, number]; function_ratio: number | null; c0_ratio: number | null; c1_ratio: number | null; limit: number; uncovered_total: number; uncovered: Array<{ id: number; where: string; function: string; kind: string; label: string; implicit: boolean }>; decisions_total: number; decisions: Array<{ id: number; where: string; function: string; outcomes: Array<{ id: number; label: string; kind: string; implicit: boolean; count: number }>; covered: number }> }
```

## xtrig.route

種類: `control`。源（"la_trigger" / "dap_match" / "host" / "sequence" = 多段トリガ / "obs_match" = FPGA の観測の値の一致（fpga_obs.trigger_set） / "sampler_match" = サンプラの値の一致（sampler.trigger_set））を動作につなぐ。to_la: ロジアナをトリガする源、
to_inject: 割り込み書き込み（既定はターゲット停止）をする源。指定しない動作は切る。

| 引数 | 型 | 必須 |
|---|---|---|
| `to_la` | `Array<string> | null` |  |
| `to_inject` | `Array<string> | null` |  |

応答:

```ts
{ to_la: Array<"la_trigger" | "dap_match" | "host" | "sequence" | "obs_match" | "sampler_match">; to_inject: Array<"la_trigger" | "dap_match" | "host" | "sequence" | "obs_match" | "sampler_match"> }
```

## xtrig.set_inject

種類: `control`。割り込み書き込みの番地と値（既定は DHCSR ← C_HALT | C_DEBUGEN = ターゲット停止）。
ホストの DAP コマンドの境界で行い、ホストの SELECT・CSW・TAR は元に戻す。

| 引数 | 型 | 必須 |
|---|---|---|
| `address` | `number` |  |
| `value` | `number` |  |
| `ap` | `number` |  |

応答:

```ts
null
```

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

応答:

```ts
null
```

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

応答:

```ts
{ stages: number; armed: boolean; stage: number; fired: boolean }
```

## xtrig.sequence_status

種類: `read`。多段トリガの状態（レジスタの事実）: 今の段 stage、発火したか fired、発火した時刻 time（タイムベースのカウント）、仕掛けているか armed、設定した段の数 stages、段ごとの状態 stage_states（done = 済んだ / current = 待っている段 / waiting = まだ / idle = 仕掛けていない。発火したら全部 done）。config は段とピンの条件のレジスタの読み戻し（xtrig.set_sequence の stages・pattern_a・pattern_b と同じ形。within_us は刻みに丸めた値、制限なしは null）。fire_windows は発火した後だけ: 最後の段を進めえた事象ごと（event = pattern_a / pattern_b / dap_match / host。パターンは使っている ch の種類 cause = pin / internal / input_lanes ごと）の、事象の点の範囲 t_lo 以上 t_hi 未満（タイムベース。match・host はパルスの周期で t_lo = t_hi。1 周期に複数のサンプルの専用の入力は fine_lo・fine_hi も。TLV TIMING の seq_*。申告が無ければ null で unknown に名前）。どの事象が進めたかは回路が記録しないので選ばない。

応答:

```ts
{ stage: number; fired: boolean; time: number; armed: boolean; stages: number; stage_states: Array<"done" | "current" | "waiting" | "idle">; config: { stages: Array<{ when: Array<"pattern_a" | "pattern_b" | "dap_match" | "host">; count: number; within_us: number | null }>; pattern_a: { mask: number; value: number; rise: number; fall: number }; pattern_b: { mask: number; value: number; rise: number; fall: number } }; fire_windows: Array<{ event: "pattern_a" | "pattern_b" | "dap_match" | "host"; cause?: "pin" | "internal" | "input_lanes" | null; t_lo: number | null; t_hi: number | null; fine_lo?: number; fine_hi?: number; point?: string | null; source?: string; from?: "TIMING" | null; unknown: Array<string> }> }
```

## xtrig.fire

種類: `control`。源 host を 1 回発火する。

応答:

```ts
null
```

## xtrig.status

種類: `read`。割り込み書き込みの状態 inject（結果 result・busy・ack・成功した回数 count・最後の時刻 time）と中身（ap・address・value。レジスタの読み戻し）、番地一致 match の回数 count・最後の時刻 time（タイムベースのカウント）と条件（address・address_mask・data・data_mask・on_write・on_read。読み戻し）、いまの経路 route（レジスタから読んだ to_la・to_inject の源の名前。xtrig.route 以外の道具が書いたビットも含む）。

応答:

```ts
{ inject: { busy: boolean; result: "ok" | "swd_error" | "not_connected" | "absent"; ack: number; count: number; time: number; ap: number; address: number; value: number }; match: { count: number; time: number; address: number; address_mask: number; data: number; data_mask: number; on_write: boolean; on_read: boolean }; route: { to_la: Array<"la_trigger" | "dap_match" | "host" | "sequence" | "obs_match" | "sampler_match">; to_inject: Array<"la_trigger" | "dap_match" | "host" | "sequence" | "obs_match" | "sampler_match"> } }
```

## xtrig.output_set

種類: `control`。クロストリガの動作「出力の端子を動かす」の設定（U41）。channel = 出力に使う端子のロジアナの ch（使える端子は xtrig.output_status。Primer: 4 予備・5 UART RX・6 SWO・7 UART TX）。条件 = pattern（端子の並び。ロジアナの ch 0〜7 のビット {"mask", "value", "any"}。any = true は見る ch のどれかが一致で成立。毎周期その場で照合）or sources（源 "la_trigger" / "dap_match" / "host" / "sequence" / "obs_match" / "sampler_match"）。mode: "pulse"（条件の立ち上がりで pulse_us の脈。刻みに丸めた実際の範囲を返す）/ "hold"（立ち上がりから無効にするまで）/ "while"（条件の間だけ。遅れ 40 ns 程度）。level: 動いている間に出す値（high / low）。動いていない間は駆動しない（高インピーダンス。プルアップのある端子は弱く Highなので notes を返す）。設定するとその端子の普段の機能（UART・SWO）を止めて出力に使う（戻すのは xtrig.output_release）。同時に動かせる数は xtrig.output_status の actions。enable = true で続けて有効にする（既定は無効のまま。xtrig.output_enable）。

| 引数 | 型 | 必須 |
|---|---|---|
| `channel` | `number` | ○ |
| `mode` | `"pulse" | "hold" | "while"` |  |
| `level` | `"high" | "low"` |  |
| `pattern` | `{ mask?: number; value?: number; any?: boolean } | null` |  |
| `sources` | `Array<"la_trigger" | "dap_match" | "host" | "sequence" | "obs_match" | "sampler_match"> | null` |  |
| `pulse_us` | `number | null` |  |
| `enable` | `boolean` |  |

応答:

```ts
{ channel: number; mode: "pulse" | "hold" | "while"; level: "high" | "low"; pull_up: boolean; enabled: boolean; pulse_us?: { min: number; max: number }; notes?: Array<string> }
```

## xtrig.output_enable

種類: `control`。出力の端子の動作を有効にする（脈・保持は次の条件の立ち上がりから、条件の間はすぐ）。

| 引数 | 型 | 必須 |
|---|---|---|
| `channel` | `number` | ○ |

応答:

```ts
{ channel: number; enabled: true }
```

## xtrig.output_disable

種類: `control`。出力の端子の動作を無効にする（保持も戻る）。端子は出力に使ったまま駆動しない（高インピーダンス）。

| 引数 | 型 | 必須 |
|---|---|---|
| `channel` | `number` | ○ |

応答:

```ts
{ channel: number; enabled: false }
```

## xtrig.output_release

種類: `control`。出力の端子の動作を無効にし、設定を消して、端子を普段の機能（UART・SWO）に戻す。

| 引数 | 型 | 必須 |
|---|---|---|
| `channel` | `number` | ○ |

応答:

```ts
{ channel: number; claim: false }
```

## xtrig.output_status

種類: `control`。出力の端子の候補ごとの状態: channel（ロジアナの ch）・pull_up（外せないプルアップ）・configured・enabled・active（いま駆動中）・claim・mode・level・pattern・sources・脈の長さ。actions = 同時に動かせる数、free = 空いている数。この構成に出力の端子が無ければ空。

応答:

```ts
{ outputs: Array<{ channel: number; pull_up: boolean; configured: boolean; enabled?: boolean; active?: boolean; claim?: boolean; mode?: "pulse" | "hold" | "while" | null; mode_raw?: number; level?: "high" | "low"; pattern?: { mask: number; value: number; any: boolean }; sources?: Array<"la_trigger" | "dap_match" | "host" | "sequence" | "obs_match" | "sampler_match">; length_raw?: 0; pulse_us?: { min: number; max: number } }>; actions: number; free: number }
```

## gpio.read

種類: `control`。拡張ヘッダの汎用入出力 8 本の状態（U41）: drive（出力の値）・enable（出力にしている端子）・level（端子の値）。ビット n = ロジアナの ch n（Primer の J12: 0 SWCLK/TCK・1 TDI・2 nRESET・3 SWDIO/TMS・4 予備・5 UART RX・6 SWO/TDO・7 UART TX）。変えない。

応答:

```ts
{ drive: number; enable: number; level: number }
```

## gpio.set

種類: `control`。拡張ヘッダの端子を出力にする・値を決める（U41）。mask の端子だけ、出力の値を drive、出力にするかを enable のビットにする（ほかの端子は変えない）。出力にした端子は普段の機能（デバッグの線・UART・SWO・クロストリガの出力）より優先して駆動する（戻すのは gpio.release）。ターゲットに接続中は断る。書いた後の状態を返す。

| 引数 | 型 | 必須 |
|---|---|---|
| `drive` | `number` | ○ |
| `enable` | `number` | ○ |
| `mask` | `number` |  |

応答:

```ts
{ drive: number; enable: number; level: number }
```

## gpio.release

種類: `control`。mask の端子を出力にしない（普段の機能に戻す。U41）。spi.transfer の後の SPI の端子もこれで離す。

| 引数 | 型 | 必須 |
|---|---|---|
| `mask` | `number` |  |

応答:

```ts
{ drive: number; enable: number; level: number }
```

## gpio.sequence

種類: `control`。drive の値の列を順に書き、書くごとに端子を読む（汎用のビットバング。U41・U42）。USB の 1 往復で最大 126 状態（CONTROL の EXCHANGE。プロトコル 2 のビットストリームが要る）。出力にする端子は先に gpio.set で決める。levels[i] は states[i] を書いた頃の端子（同期化の分、書く前の値のことがある。同じ値を 2 回書けば 2 回目には必ず見える）。ターゲットに接続中は断る。

| 引数 | 型 | 必須 |
|---|---|---|
| `states` | `Array<number>` | ○ |

応答:

```ts
{ levels: Array<number> }
```

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

応答:

```ts
{ received: string; words: number; states: number; elapsed_s: number }
```

## swo.start

種類: `control`。SWO の受信を始める。mode: "uart"（NRZ。TPIU の SPPR = 2）/ "manchester"（SPPR = 1。ケイパビリティの swo_manchester がある構成だけ。ビットごとに同期し直すので送り手との速さのずれに強い）。ターゲット側の TPIU（ACPR = TRACECLKIN / baud - 1、SPPR）と ITM はファームウェアかデバッガで設定しておくこと。受けられる速さ（sys 100 MHz）: UART は 25 Mbaud まで（速さが一致すること）、Manchester は 16.7〜20 Mbit/s までが実用。

| 引数 | 型 | 必須 |
|---|---|---|
| `baud` | `number` |  |
| `mode` | `"uart" | "manchester"` |  |

応答:

```ts
null
```

## swo.read

種類: `control`。timeout_s の間に届いた SWO のバイト列（ITM パケットのまま、16 進文字列）。

| 引数 | 型 | 必須 |
|---|---|---|
| `timeout_s` | `number` |  |

応答:

```ts
string
```

## swo.stop

種類: `control`。SWO の受信を止める。

応答:

```ts
null
```

## swo.decode

種類: `control`。timeout_s の間に届いた SWO を ITM / DWT のパケットに解読する（`swo_start` の後。`swo_read` と同じ受信を使うのでどちらか一方で読む）。packets（例外トレースは name 付き・PC サンプル・スティミュラス等）は先頭 limit 件、text はポートごとの printf の行。記録中なら共通の時間軸にも入れる。 synced = ITM の解読が同期しているか、unsynced_bytes = 同期を失ってから捨てたバイトの数（同期パケットを待つ間。推定で再開しない）。

| 引数 | 型 | 必須 |
|---|---|---|
| `timeout_s` | `number` |  |
| `limit` | `number` |  |

応答:

```ts
{ bytes: number; synced: boolean; unsynced_bytes: number; packets: Array<ItmPacket>; dropped: number; text: { [key: string]: Array<string> }; overflows: number; errors: number }
```

## swo.text

種類: `control`。timeout_s の間に届いた SWO から、ITM のポート port に printf された行（pending は改行の来ていない途中）。

| 引数 | 型 | 必須 |
|---|---|---|
| `port` | `number` |  |
| `timeout_s` | `number` |  |

応答:

```ts
{ port: number; lines: Array<string>; pending: string }
```

## swo.configure

種類: `control`。ITM・DWT の SWO への出力を、ターゲットを止めずに設定する（前の設定は戻してから）。ports: ITM のスティミュラス・ポート。timestamps: ローカルのタイムスタンプ（timestamp_prescale 1/4/16/64）。exception_trace: 例外の入る・出る・戻る。counters: DWT のイベントカウンタ（cpi・exc・sleep・lsu・fold）の溢れ。data_trace: DWT の比較器で変数へのアクセスを値・PC・時刻付きで記録（expression は変数名・C の式・"0x番地:大きさ"、access read/write/rw、pc・value・offset のどれを出すか。ARMv7-M）。value_watch: 値一致（変数に value が読み書きされたら action halt = 止める / trace = 止めずに記録。expression を省くと番地を問わない）。swo_baud で SWO の受信も始め、core_clock_hz も渡すと TPIU（分周）も設定する。受信を始めてから出力を有効にする。trace_pin: SWO のピンを有効にするチップ固有のレジスタの欄（{register: SVD の "周辺.レジスタ", fields: {欄: 値}}。SVD が要る。レジスタ名の慣習で探さないので、渡さなければ書かない）。pyOCD のウォッチポイントと比較器を取り合わない。計画（比較器・書いたレジスタ・帯域の見積もり・警告）を返す。結果は `swo.trace`。

| 引数 | 型 | 必須 |
|---|---|---|
| `ports` | `Array<number> | null` |  |
| `timestamps` | `boolean` |  |
| `timestamp_prescale` | `1 | 4 | 16 | 64` |  |
| `exception_trace` | `boolean` |  |
| `counters` | `Array<"cpi" | "exc" | "sleep" | "lsu" | "fold"> | null` |  |
| `data_trace` | `Array<{ expression: string; access?: "read" | "write" | "rw"; pc?: boolean; value?: boolean; offset?: boolean }> | null` |  |
| `value_watch` | `Array<{ expression?: string; value: number; size?: 1 | 2 | 4; access?: "read" | "write" | "rw"; action?: "halt" | "trace"; pc?: boolean }> | null` |  |
| `swo_baud` | `number | null` |  |
| `core_clock_hz` | `number | null` |  |
| `trace_pin` | `{ register: string; fields: { [key: string]: number } } | null` |  |

応答:

```ts
{ running: true; core: { name: string; arch: string; cpuid: number }; plan: { comparators: Array<SwoComparator>; warnings: Array<string>; bytes_per_access: { [key: string]: number }; swo_bytes_per_s_max: number | null; writes: Array<{ address: number; value: number; mask: number; note: string }> }; swo_baud: number | null; synced: boolean; trace_pin: { register: string; fields: { [key: string]: number } } | null; t0: number | null; notes: Array<string> }
```

## swo.trace

種類: `control`。`swo.configure` の結果（設定中ならいままでの分、戻した後は最後の結果）。accesses: データトレース・値一致（trace）のアクセス（t = タイムベースの時刻・変数・値（`debug_evaluate` と同じ形。変数の型で解釈できなければ kind = int の符号なし整数。生の値は raw）・読み書き・番地・PC とその関数と行）の offset から limit 件（name で変数を絞る）、counters: イベントカウンタの合計（溢れ × 256）、exceptions: 例外ごとの回数。記録中なら記録の stream "swo"（kind data_access・counter_overflow）にも入る。 exceptions.events_dropped = ためる上限（events_limit 件）を超えてためなかった例外の出来事の数。 counter_overflow の時刻はその溢れの印のパケットの時刻。untimed = デバイスの時刻が分からなかったパケットの数（そのアクセスの t は null で、記録には置かない。0 にしない）。

| 引数 | 型 | 必須 |
|---|---|---|
| `offset` | `number` |  |
| `limit` | `number` |  |
| `name` | `string | null` |  |

応答:

```ts
SwoTrace
```

## swo.unconfigure

種類: `control`。`swo.configure` の設定を戻す（比較器を外し、DWT_CTRL・ITM の変えたビットを元に。SWO の受信と TPIU は残す）。届いている分までの結果（`swo.trace` と同じ形）を返す。

応答:

```ts
SwoTrace
```

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

応答:

```ts
{ address: number; wake: { mode: "poll"; interval_s: number } | { mode: "sampler"; rate_hz: number; samples: number; wakes: number; idle: number; full_polls: number; lost_events: number; interval_s: number }; up: Array<{ index: number; name: string | null; size: number; mode: number; resyncs: number; received: number; unread: number }>; down: Array<{ index: number; name: string | null; size: number; mode: number; resyncs: number }> }
```

## rtt.read

種類: `read`。貯めた受信を読む（機器には触らない）。channel は RTT の上りの番号か semihosting（セミホスティングの出力）。since を省くと前回の続きから（読むと進む）、since（前回の next）を渡すとそこから（画面用。進まない）。timeout_s までデータを待つ。encoding: text / hex。lost はあふれて読めなかったバイト数。

| 引数 | 型 | 必須 |
|---|---|---|
| `channel` | `number | "semihosting"` |  |
| `since` | `number | null` |  |
| `max_bytes` | `number` |  |
| `encoding` | `"text" | "hex"` |  |
| `timeout_s` | `number` |  |

応答:

```ts
{ channel: number | "semihosting"; data: string; start: number; next: number; lost: number }
```

## rtt.write

種類: `control`。ターゲットへ送る（RTT の下りの番号。semihosting ならセミホスティングの標準入力）。text（UTF-8）か data_hex。ターゲットが読んで空くまで timeout_s 待つ。written は書けたバイト数。

| 引数 | 型 | 必須 |
|---|---|---|
| `channel` | `number | "semihosting"` |  |
| `text` | `string | null` |  |
| `data_hex` | `string | null` |  |
| `timeout_s` | `number` |  |

応答:

```ts
{ written: number }
```

## rtt.stop

種類: `control`。RTT の受信を止める（貯めたものは `rtt_read` で読める）。

応答:

```ts
{ stopped: boolean }
```

## rtt.status

種類: `read`。RTT の受信の様子（制御ブロックの番地・チャンネルの名前と大きさ・受信したバイト数・未読・最後の誤り）とセミホスティングの出力の量。wake（読みの起こし方と、サンプラなら idle = ターゲットに触れなかった周期等）、stats（poll の数・デバッグポートの読み書きの回数とバイト数）も。

応答:

```ts
{ running: boolean; error: string | null; polls: number; address?: number; wake?: { mode: "poll"; interval_s: number } | { mode: "sampler"; rate_hz: number; samples: number; wakes: number; idle: number; full_polls: number; lost_events: number; interval_s: number }; stats?: { polls: number; empty_polls: number; hinted_polls: number; reads: number; writes: number; read_bytes: number; write_bytes: number; up_bytes: number; down_bytes: number }; up?: Array<{ index: number; name: string | null; size: number; mode: number; resyncs: number; received: number; unread: number }>; down?: Array<{ index: number; name: string | null; size: number; mode: number; resyncs: number }>; semihosting: { received: number; unread: number } }
```

## terminal.sources

種類: `read`。端末に出せる源の一覧（機器には触れない）: id（rtt0〜 / swo0〜swo31 / uart / semihosting）・kind・name・writable（RTT は同じ番号の下りがあるとき、UART、セミホスティングの標準入力）・received（貯めた量）。SWO のポートは swo_start の後（0 と、データの来たポート）。 uart には baud（いまのボーレート。まだ開いていなければ null）。

応答:

```ts
{ sources: Array<{ id: string; kind: "rtt" | "swo" | "uart" | "semihosting"; name: string; writable: boolean; received: number; channel?: number; port?: number; baud?: number | null }>; rtt: boolean; swo: boolean }
```

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

応答:

```ts
{ source: string; data: string; start: number; next: number; lost: number; times?: Array<{ at: number; wall: number; t: number | null }>; timebase_hz?: number | null; terminals?: Array<{ at: number; id: number }> }
```

## user.trace.start

種類: `control`。ターゲットが出す値とイベントを取り始める（止めない）。channels: ITM のポート（source itm、index 1〜23 等）か RTT のチャンネル（source rtt、rtt_start 済み）の値の型（i8〜u32・f32・qM.N・struct）と名前。events: ファームの fprobe_user.h の名前付きイベント（FPROBE_EVENT_START / STOP / MARK・FPROBE_VALUE_*。ITM のポート 24・25。名前は ELF の文字列）。event_rtt_channel: 名前付きイベントを RTT のこのチャンネルからも。ITM を使うなら swo_baud で SWO の受信も始め（受信が先、ポートを有効にするのは後）、core_clock_hz も渡すと TPIU も設定する。

| 引数 | 型 | 必須 |
|---|---|---|
| `channels` | `Array<{ name?: string; source?: "itm" | "rtt"; index: number; type?: string | { type: "struct"; fields: Array<Array<string>> }; unit?: string }> | null` |  |
| `events` | `boolean` |  |
| `event_rtt_channel` | `number | null` |  |
| `swo_baud` | `number | null` |  |
| `core_clock_hz` | `number | null` |  |

応答:

```ts
{ running: boolean; channels: Array<{ name: string; source: "itm" | "rtt"; index: number; type: string | { type: "struct"; fields: Array<[string, string]> }; unit?: string }>; events: boolean; itm_ports: Array<number>; rtt_channels: Array<number>; timebase_hz: number; t0: number | null; warnings: Array<string> }
```

## user.trace.stop

種類: `control`。値とイベントを取るのを止め（足した ITM のポートを戻す）、要約（数・区間の数・誤り）を返す。記録中なら stream "user" に書く（value・mark・start・stop・interval）: `timing_measure` の start / stop に {"stream": "user", "kind": "start", "body": {"name": …}} がそのまま使える。

応答:

```ts
{ running: boolean; channels: Array<{ name: string; source: "itm" | "rtt"; index: number; type: string | { type: "struct"; fields: Array<[string, string]> }; unit?: string }>; events: boolean; itm_ports: Array<number>; rtt_channels: Array<number>; timebase_hz: number; t0: number | null; warnings: Array<string>; records: number; intervals: number; errors: { [key: string]: number }; unmatched_stops: { [key: string]: number }; open_intervals: { [key: string]: number }; stops_before_first_start: { [key: string]: number }; window?: EventWindowInfo; recorded: boolean }
```

## user.trace.get

種類: `control`。値とイベントの一覧（時刻順。取っている最中ならいままでの分）。name・kinds（value / mark / start / stop）で絞り、offset から limit 件。時刻はタイムベース（ITM は SWO のバイトのデバイスの時刻、RTT はホストの時刻を写したものと不確かさ）。

| 引数 | 型 | 必須 |
|---|---|---|
| `name` | `string | null` |  |
| `kinds` | `Array<"value" | "mark" | "start" | "stop"> | null` |  |
| `offset` | `number` |  |
| `limit` | `number` |  |

応答:

```ts
{ running: boolean; channels: Array<{ name: string; source: "itm" | "rtt"; index: number; type: string | { type: "struct"; fields: Array<[string, string]> }; unit?: string }>; events: boolean; itm_ports: Array<number>; rtt_channels: Array<number>; timebase_hz: number; t0: number | null; warnings: Array<string>; records: Array<{ t: number; kind: "value" | "mark" | "start" | "stop"; name: string; source: "itm" | "rtt"; index: number; value?: number | { [key: string]: number }; uncertainty?: number }>; intervals: number; errors: { [key: string]: number }; unmatched_stops: { [key: string]: number }; open_intervals: { [key: string]: number }; stops_before_first_start: { [key: string]: number }; window?: EventWindowInfo; total: number; offset: number }
```

## user.trace.stats

種類: `control`。値とイベントの集計（Keil の Event Statistics・SystemView のユーザイベント相当）: 値は名前ごと（構造体は 名前.欄）の数・最小・最大・平均・最後、区間（start / stop を名前ごとに後入れ先出しで対に。入れ子・割り込みの中も正しい）は回数・最小・最大・平均・σ・分位・分布・長い順の上位（開始の時刻）、mark は回数。name で絞る。

| 引数 | 型 | 必須 |
|---|---|---|
| `name` | `string | null` |  |
| `bins` | `number` |  |
| `limit` | `number` |  |
| `longest` | `number` |  |

応答:

```ts
{ running: boolean; channels: Array<{ name: string; source: "itm" | "rtt"; index: number; type: string | { type: "struct"; fields: Array<[string, string]> }; unit?: string }>; events: boolean; itm_ports: Array<number>; rtt_channels: Array<number>; timebase_hz: number; t0: number | null; warnings: Array<string>; records: number; intervals: { [key: string]: IntervalReport }; errors: { [key: string]: number }; unmatched_stops: { [key: string]: number }; open_intervals: { [key: string]: number }; stops_before_first_start: { [key: string]: number }; window?: EventWindowInfo; values: { [key: string]: { count: number; min: number; max: number; mean: number; last: number; last_t: number; source: "itm" | "rtt"; index: number } }; marks: { [key: string]: number } }
```

## uart.write

種類: `control`。ターゲットの UART に送る（latin-1）。baud を指定するとボーレートも変える。written（送ったバイト数）と、使ったボーレート baud（頼まれず、まだ開いていなければ 115200）。

| 引数 | 型 | 必須 |
|---|---|---|
| `data` | `string` | ○ |
| `baud` | `number | null` |  |

応答:

```ts
{ written: number; baud: number }
```

## uart.read

種類: `control`。timeout_s の間に届いたもの data（latin-1 で文字列化）と、受けたボーレート baud（頼まれず、まだ開いていなければ 115200）。

| 引数 | 型 | 必須 |
|---|---|---|
| `timeout_s` | `number` |  |
| `baud` | `number | null` |  |

応答:

```ts
{ data: string; baud: number | null }
```

## uart.configure

種類: `control`。ターゲットの UART のボーレートを変える（送らない・読まない。端末の画面の設定）。変えた後の baud を返す。

| 引数 | 型 | 必須 |
|---|---|---|
| `baud` | `number` | ○ |

応答:

```ts
{ baud: number }
```

## uart.flow

種類: `control`。UART のハードウェアのフロー制御（RTS/CTS）。rts_channel / cts_channel: RTS を出す / CTS を読む端子（ロジアナの ch。候補はケイパビリティ uart_flow = 結果の pins。Primer は 4 予備・1 TDI。選んだ端子の普段の機能の出力は止める。null で使わない）。auto: 自動の RTS/CTS（CTS が出ている間だけ送り、受信の下流が満杯なら RTS を下げる。CTS を取らなければ出ているとみなす）。auto = false なら RTS は COM ポートの RTS どおり。線はアクティブ Low。設定と制御線の状態（`uart.lines` と同じ形）を返す。

| 引数 | 型 | 必須 |
|---|---|---|
| `auto` | `boolean` |  |
| `rts_channel` | `number | null` |  |
| `cts_channel` | `number | null` |  |

応答:

```ts
UartFlowState
```

## uart.lines

種類: `control`。UART の制御線の状態: cts（CTS が出ている）・rts（RTS を出している）・host_rts（COM ポートの RTS）・host_break（COM ポートのブレークを送っている）と、フロー制御の設定（auto・rts_channel・cts_channel・pins）。

応答:

```ts
UartFlowState
```

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

応答:

```ts
{ recording: string; path: string }
```

## recording.stop

種類: `control`。記録を止める。

応答:

```ts
{ recording: string; events: number | null }
```

## recording.status

種類: `read`。記録中か、名前・件数・時刻の範囲（タイムベース）・ホストの出来事の時刻の不確かさ。記録中は watch_running（MCU のメモリの読みが動いているか。false なら記録にメモリが入らず欠ける）・events_errors（取り込みの失敗）。 ended = 記録を終えた理由（null = 続いている）: timebase_restart（タイムベースが前の読みより小さくなった = 0 に戻った。記録に timebase・restart の行。以後の行は入らず meta の rejected_rows に数える）・design_switch・link_lost。終えた記録は recording_stop で閉じる。

応答:

```ts
{ recording: boolean; name: string | null; events?: number; span?: [number, number] | null; timebase_hz?: number; ended?: RecordingEnded | null; watch_running?: boolean; clock_uncertainty_ticks?: number | null; segmented?: boolean; events_errors?: { count: number; last: string | null; lost: number }; la_stream?: LaRecordingStatus }
```

## recording.list

種類: `read`。保存した記録の一覧（新しい順）。

応答:

```ts
{ recordings: Array<{ name: string; title: string | null; board: string | null }> }
```

## recording.open

種類: `read`。保存した記録を開く（以後の読み出しの対象）。name は `recording_list` の name。

| 引数 | 型 | 必須 |
|---|---|---|
| `name` | `string` | ○ |

応答:

```ts
{ name: string; events: number; span: [number, number] | null }
```

## recording.events

種類: `read`。時刻（タイムベース、t1 は含まない）の範囲の出来事を時刻順に。streams: dap / uart / swo / rtt / aux / host / target /
annotation / sample（name = 記録の変数の名前）/ fpga（fields = 信号ごとの値）/ clock。fpga の value_untimed・gap_untimed・group_untimed は、時刻合わせの印が来ないまま鎖が終わって時刻の決まらない記録（t は受けた時 = 起きた時刻の上限）。 dap のクロストリガの割り込み書き込みは kind inject（本体に転送数 transfers。2026-10-23 より前の記録・旧い回路では ap_write）。 PC の時計で付けた時刻の出来事は本体に time_source: host（PC で起きた出来事: 操作・印・フック）/ host_received（PC が受けた・読んだ時刻: SWO の ITM パケット・RTT・セミホスティングの行・ターゲットの状態・ヒープ・トレースポイント・ログのブレークポイント。対象の出来事はそれより前で、どれだけ前かは分からず uncertainty に入っていない）。host の call は本体に t_start（始まりを同じ時計の対応付けで写したもの）・t_start_uncertainty。印の無い出来事の時刻はデバイスのタイムベース。 先頭 limit 件（total = 範囲の総数・truncated = 切ったか・limit = 使った上限）。

| 引数 | 型 | 必須 |
|---|---|---|
| `t0` | `number | null` |  |
| `t1` | `number | null` |  |
| `streams` | `Array<string> | null` |  |
| `kinds` | `Array<string> | null` |  |
| `limit` | `number` |  |

応答:

```ts
{ events: Array<{ t: number; stream: string; kind: string; a: number; b: number; body: Json; t_end: number | null; uncertainty: number | null; name?: string; fields?: { [key: string]: number | string } }>; total: number; truncated: boolean; limit: number }
```

## recording.correlate

種類: `read`。結果の直前に多い出来事（原因の判定はしない）: 結果の条件 effect（例 "誤り changed"）の各時刻の直前（window_s 秒の中）に、causes の各条件（例 "restart changed and state=BUSY"・"in0 rose"）が来ていた割合 preceded・記録のうち候補の後 window_s 以内に当たる時間の割合 coverage・後に結果が来た候補の割合 followed・遅れ（秒）・例の時刻（タイムベース）。測った事実だけを返す（判定・経験則の順位づけをしない）。並びは preceded の大きい順、同じなら条件の文字の順。変数: MCU のメモリの名前（watch.set の names）か番地、FPGA の信号の名前、ピンの名前か ch<N>（同じ名前は mcu. / fpga. / pin. で区別）。項: <変数> changed / rose / fell、<変数>=<値>（列挙の名前も）を and でつなぐ（瞬間の項が 1 つ以上）。sampling = 変数の読みの間隔（秒。遅れの分解能）、fpga_hidden = 標本型の標本の間で見えなかった変化の数。causes を省くと記録の変数から候補を作って調べ（auto: 信号の変化・列挙の値に入った瞬間・変化 かつ 列挙 = 値の組・ピンの rose / fell・ほかのメモリの変化）、上位 limit 個（切る前の数 causes_total・切ったか truncated・使った limit。明示の causes は切らず limit = null）。max_coverage を渡せば coverage がそれを超える自動の候補を除く。reads = MCU のメモリの読みの事実（回数・間隔の分位・いちばん長い空白）。gaps = 使った変数のデータの欠け（reason: reads_missing（gap_s を渡したときだけ）/ read_failed / pins_missing / fpga_lost / events_error / recording_dropped、t0・t1。その間の結果・候補は数えられていない。pin_skew_unknown だけは欠けではなく、値はあるがピンの ch のずれが分からずずらさずに置いた区間で、結果から外さない。warnings にも件数）。judged = 先行率の分母（窓が候補の変数の欠けにかかる結果と、結果の変数の欠けの直後に見えた結果は除く）。fpga_hidden = 標本型の FPGA の観測で標本の間に見えなかった変化の数（信号を問わない合計）。sampling の null = 決まった読みの間隔が無い変数。effect.delta = 結果の MCU のメモリの範囲の最初と最後の値の差（count と違えば 1 回の読みで何回分も動いている）。 reads の各変数に time_point（read_request = 変わった値を読みに行った転送の点（線上の点の回路）/ value_returned = 値が返った周期）・change_window_s（線上の点の回路: 変化があった範囲 = 前の読みの t_lo より後〜変わった値の語の t_hi の幅の分位）・change_window_unknown（端が分からない変化の数）。 候補ごとに terms（項: variable・kind = mcu / fpga / pin・op・value）と fpga_only（項の変数がすべて FPGA の信号）。

| 引数 | 型 | 必須 |
|---|---|---|
| `effect` | `string` | ○ |
| `causes` | `Array<string> | null` |  |
| `window_s` | `number` | ○ |
| `t0` | `number | null` |  |
| `t1` | `number | null` |  |
| `examples` | `number` |  |
| `limit` | `number` |  |
| `gap_s` | `number | null` |  |
| `max_coverage` | `number | null` |  |

応答:

```ts
{ effect: { condition: string; count: number; delta: { [key: string]: number } }; window_s: number; range: [number, number]; timebase_hz: number; auto: boolean; no_values: Array<string>; warnings: Array<string>; causes: Array<{ condition: string; terms: Array<{ variable: string; kind: "mcu" | "fpga" | "pin"; op: "changed" | "rose" | "fell" | "="; value?: string }>; fpga_only: boolean; count: number; judged: number; preceded: number | null; coverage: number | null; followed: number | null; delay_s: Quantiles | null; examples: Array<{ effect_t: number; cause_t: number }>; same?: Array<string> }>; causes_total: number; truncated: boolean; limit: number | null; sampling: { [key: string]: number | null }; reads: { [key: string]: { count: number; failed: number; interval_s: Quantiles | null; longest: [number, number] | null; time_point: "read_request" | "value_returned"; change_window_s: Quantiles | null; change_window_unknown: number } }; fpga_hidden: number; gaps: Array<DataGap>; gap_s: number | null }
```

## recording.condition.parse

種類: `read`。条件の文字（`recording_correlate`・`recording_trigger_set`・`fpga_obs_count` と同じ書き方: 「restart changed and state=BUSY」。空白・= を含む名前は "…" で囲む）を項に分ける。記録・機器に触らない（変数が記録にあるかは見ない）。読めない・瞬間の項（changed / rose / fell）が無ければ誤り（理由）。

| 引数 | 型 | 必須 |
|---|---|---|
| `condition` | `string` | ○ |

応答:

```ts
{ condition: string; terms: Array<{ variable: string; op: "changed" | "rose" | "fell" | "="; value?: string }> }
```

## recording.durations

種類: `read`。変数（recording.correlate と同じ指し方: MCU のメモリの名前・番地、FPGA の信号、ピン）の値ごとの回数・続いた時間（両端の見えている区間だけ。秒の最小・中央値・90 % 点・最大）・範囲の中でその値だった時間の割合 share。続いた時間の合計の大きい順に limit 個（値の総数 values_total・切ったか truncated・使った limit）。例: FPGA の state が BUSY のまま続く時間と、ファームウェアの待ち時間を比べる。データの欠けにかかる回は数えず excluded（欠けは gaps。pin_skew_unknown はずれが分からずずらさずに置いた区間で、除かない）。

| 引数 | 型 | 必須 |
|---|---|---|
| `variable` | `string` | ○ |
| `t0` | `number | null` |  |
| `t1` | `number | null` |  |
| `limit` | `number` |  |
| `gap_s` | `number | null` |  |

応答:

```ts
{ variable: string; range: [number, number]; changes: number; excluded: number; gaps: Array<DataGap>; values_total: number; truncated: boolean; limit: number; values: { [key: string]: { count: number; share: number | null; duration_s: Quantiles | null } } }
```

## recording.transactions

種類: `read`。記録を区間（リクエスト等の 1 回の取引）に切って数える: 区間 = start（瞬間の項のある条件）が成り立ってから次に成り立つまで。区間ごとに by（変数）の開始時の値・counts の各条件の回数・effect が起きたか。groups = by の値ごとの区間の数・effect の割合・counts の平均、rows = 最初の rows 区間（入れうる区間の数 rows_total・切ったか rows_truncated・使った rows は rows_limit）。条件・変数は recording.correlate と同じ書き方。by_at = end なら by は区間の終わり（次の始めの直前）の値。データの欠けにかかる区間は groups・rows に入れず excluded（変数ごとの内訳 excluded_by、欠けは gaps。pin_skew_unknown はずれが分からずずらさずに置いた区間で、除かない）。length_s = 区間の長さ・effect_offset_s = 区間の始めから結果までの時間（秒の分位。始めの直後に集まるなら前の区間の結果かもしれない: effect_lag_s で前の区間に割り当てる）。warnings = 欠けで除いた区間の数と内訳。effect があれば by_effect = 結果が起きた区間（with）と起きなかった区間（without）の数・counts の平均。

| 引数 | 型 | 必須 |
|---|---|---|
| `start` | `string` | ○ |
| `by` | `string | null` |  |
| `counts` | `Array<string> | null` |  |
| `effect` | `string | null` |  |
| `t0` | `number | null` |  |
| `t1` | `number | null` |  |
| `rows` | `number` |  |
| `by_at` | `"start" | "end"` |  |
| `effect_lag_s` | `number` |  |
| `gap_s` | `number | null` |  |

応答:

```ts
{ start: string; by: string | null; effect: string | null; range: [number, number]; timebase_hz: number; transactions: number; excluded: number; excluded_by: { [key: string]: number }; length_s: Quantiles | null; effect_offset_s: Quantiles | null; effect_lag_s: number; warnings: Array<string>; gaps: Array<DataGap>; groups: Array<{ by: string | null; count: number; effect_rate: number | null; counts: { [key: string]: number } }>; rows: Array<{ t: number; end: number; by: string | null; counts: Array<number>; effect: boolean | null }>; rows_total: number; rows_truncated: boolean; rows_limit: number; by_effect: { with: { count: number; counts: { [key: string]: number | null } }; without: { count: number; counts: { [key: string]: number | null } } } | null }
```

## recording.trigger_set

種類: `control`。記録中に、条件（recording.correlate と同じ書き方。MCU のメモリ・FPGA の信号・ピンをまたげる。例 "誤り changed"・"restart changed and state=BUSY"）が成り立つたびに記録に印（stream annotation の mark）を付ける。action = stop なら、最初に成り立った時刻から post_s 秒が記録に入ったら記録を止める（めったに起きない不具合を前後つきで残す）。判定は PC で、今から lag_s 秒前までを調べる（ハードのクロストリガとは別。調べ終えた時刻より前に後から届いた出来事は調べず、その数を trigger_status の late に出す）。記録の始めから調べる。

| 引数 | 型 | 必須 |
|---|---|---|
| `condition` | `string` | ○ |
| `action` | `"mark" | "stop"` |  |
| `post_s` | `number` |  |
| `lag_s` | `number` |  |

応答:

```ts
{ state: "off" } | { condition: string; action: "mark" | "stop"; post_s: number; lag_s: number; checked: number; hits: number; first_t: number | null; last_t: number | null; state: "off" | "armed" | "fired" | "stopped" | "error"; error: string | null; late: number }
```

## recording.trigger_status

種類: `read`。条件の見張り（recording.trigger_set）の様子: 条件・action・回数 hits・最初と最後に成り立った時刻 first_t / last_t（タイムベース）・調べ終えた時刻 checked・lag_s・調べ終えた後に届いて調べなかった出来事の数 late・状態 state（off / armed / fired / stopped / error）。

応答:

```ts
{ state: "off" } | { condition: string; action: "mark" | "stop"; post_s: number; lag_s: number; checked: number; hits: number; first_t: number | null; last_t: number | null; state: "off" | "armed" | "fired" | "stopped" | "error"; error: string | null; late: number }
```

## recording.trigger_clear

種類: `control`。条件の見張りをやめる。

応答:

```ts
{ state: "off" }
```

## recording.find

種類: `read`。条件に一致する出来事の一覧。query: stream / kind / t0 / t1 / a / b（値か {"eq"|"lt"|"ge"…: 値}）/ body（{欄: 値か {"regex": …}}）。 events は時刻の順に先頭 limit 件（count = 総数、truncated = 切ったか、limit = 使った上限）。

| 引数 | 型 | 必須 |
|---|---|---|
| `query` | `Record<string, unknown>` | ○ |
| `limit` | `number` |  |

応答:

```ts
{ count: number; events: Array<RecordingEvent>; truncated: boolean; limit: number }
```

## recording.code_at

種類: `read`。時刻からコードの位置へ: 記録（記録中のもの、または開いたもの）の時刻 t（タイムベース）の前後に記録に残っている事実。pc_before / pc_after = サンプラが読んだ DWT_PCSR（stream sample、a = 0xE000101C。profile.start の method pcsr か、その番地の watch.set）の直前（時刻 ≦ t）と直後（時刻 > t）の 1 つずつ: t（時刻）・dt_s（t との差、秒）・value（読んだ値）・pc・function・offset・file・line・where（elf（省くと読み込んである ELF）で引く。引けない欄は null）。値 0xFFFFFFFF は pc null・reason pcsr_invalid（コアが止まっている・サンプルできない）、読めなかった語は reason read_failed と ack。pc_gap_s = 直前と直後のサンプルの間の秒（行を推定しない: この間に何を実行したかは記録に無い）。state = t 以前に最後に記録されたターゲットの状態（stream target の kind state。デーモンが変化を見た時刻 = 変わった時刻の上限、uncertainty_s 付き。本文は target.status のまま: state・pc・function・file・line・halt_reason 等）。halt_before / halt_after = t の直前・直後の止まった出来事（HALTED。止まった PC は正確。呼び出し履歴・変数は記録に無い）。task = t を含む RTOS の実行の区間（stream rtos の run: t0・t1・handle・task・method・resolution 等）。missing = 材料の無いものの理由: pc no_samples（記録に DWT_PCSR のサンプルが無い）/ elf no_elf / state no_state（記録に状態の出来事が無い）/ task no_rtos（RTOS の時間軸が無い）・gap（時間軸の欠け。t0・t1・detail）・no_run（t を含む区間が無い）。elf = 行を引いた ELF、recorded_elf = 記録を始めたときに読み込まれていた ELF（違えば行が合わないことがある）。

| 引数 | 型 | 必須 |
|---|---|---|
| `t` | `number` | ○ |
| `elf` | `string | null` |  |

応答:

```ts
{ t: number; timebase_hz: number; elf: string | null; recorded_elf: string | null; pc_before: CodeAtSample | null; pc_after: CodeAtSample | null; pc_gap_s: number | null; state: CodeAtTargetState | null; halt_before: CodeAtTargetState | null; halt_after: CodeAtTargetState | null; task: { t0: number; t1: number; handle: number; task: string; method: string; resolution: number | null; idle?: true; samples?: number; start_cut?: true; end_cut?: true } | null; missing: { [key: string]: { reason: string; t0?: number; t1?: number; detail?: string | null } } }
```

## recording.intervals

種類: `read`。開始の条件から次の終了の条件までの時間（タイムベース）の一覧と統計（最小・最大・平均・標準偏差）。条件は `recording_find` と同じ。 intervals は先頭 limit 組（count = 総数、intervals_truncated = 切ったか、limit = 使った上限。統計は全部の組から）。

| 引数 | 型 | 必須 |
|---|---|---|
| `start` | `Record<string, unknown>` | ○ |
| `end` | `Record<string, unknown>` | ○ |
| `limit` | `number` |  |

応答:

```ts
{ intervals: Array<[number, number]>; count: number; min?: number; max?: number; mean?: number; stdev?: number; intervals_truncated: boolean; limit: number }
```

## recording.cpu_load

種類: `read`。記録の stream rtos（run・gap・isr）から、タスクの表（stats: `rtos_timeline_get` の stats と同じ形。cpu の分母 covered = 情報のある時間）と、window_us を渡せば窓ごとの CPU 負荷の推移（load: `rtos_timeline_load` と同じ形。窓が limit を超えたら先頭の limit 個、windows_total = 全体の数）。情報のある時間 = run のある時間 − 抜け（run の無い時間は止まっていた・記録していない間として抜け not_recorded に数える。gaps_not_recorded = その数）。記録の行を全部読む（行の上限で欠けない）。run が無ければ stats = null。 stats は `rtos_timeline_get` と同じ（max_run 等は両端が切り替えの区間だけ、cut_runs・max_cut_run）。

| 引数 | 型 | 必須 |
|---|---|---|
| `window_us` | `number | null` |  |
| `limit` | `number` |  |

応答:

```ts
{ recording: string; method?: string; timebase_hz: number; t0: number | null; t1: number | null; lower_bound?: boolean; stats: RtosStats | null; gaps_not_recorded?: number; load?: RtosLoadSeries }
```

## recording.interrupts

種類: `read`。記録の stream rtos の割り込みの区間（isr）から、割り込みごとの回数・時間（含む／自分 = 中に入れ子になった回を引く）・負荷（自分の時間 ÷ stream rtos の最初から最後まで）・最長の回の時刻（`profile_exceptions` と同じ形。clock_hz = タイムベース。並びは自分の時間の合計の多い順）。number・bins・longest を渡すと、その割り込みの含む時間の分布（distribution.histogram: bins 個の等しい幅の箱。箱ごとにいちばん長い回 longest）と長い順の上位 longest 回（t・t_end・inclusive・bin）。at（タイムベース）を渡すと、その時刻に処理していた割り込みの番号（at_numbers。外から内の順で、最後が実行中）。記録の行を全部読む。

| 引数 | 型 | 必須 |
|---|---|---|
| `number` | `number | null` |  |
| `bins` | `number | null` |  |
| `longest` | `number | null` |  |
| `at` | `number | null` |  |

応答:

```ts
{ recording: string; timebase_hz: number; span: [number, number] | null; exceptions: Array<ExceptionStats>; activations: number; distribution?: { number: number; bins: number; longest_limit: number; histogram: Array<{ lo: number; hi: number; count: number; longest: { t: number; t_end: number; inclusive: number } | null }>; longest: Array<{ t: number; t_end: number; inclusive: number; bin: number }> }; at_numbers?: Array<number> }
```

## recording.kinds

種類: `read`。記録の範囲 [t0, t1)（省くと全体）の stream・kind ごとの件数・最初と最後の時刻（first・last）・全体に対する割合（share）・1 秒あたり（per_second = 件数 ÷ 範囲の長さ seconds。全体なら記録の最初から最後まで）。件数の多い順（同じなら stream・kind の順）。total = 全部の件数、span = 数えた範囲。長期の記録はすべての分割を数える（行・分割の上限で欠けない）。

| 引数 | 型 | 必須 |
|---|---|---|
| `t0` | `number | null` |  |
| `t1` | `number | null` |  |

応答:

```ts
{ recording: string; timebase_hz: number; span: [number, number] | null; seconds: number | null; total: number; kinds: Array<{ stream: string; kind: string; count: number; first: number; last: number; share: number; per_second: number | null }> }
```

## recording.voltage

種類: `read`。記録の電圧（stream aux・kind mv）の範囲 [t0, t1)（省くと全体）の stats = {count, min, max, mean}（V）、lowest = いちばん低い点 {t, volts}（同じ値なら早い方）、dips = below_v（使う人が渡す V）以下の連続する点の区間 [{t（下がった最初の点）, t_end（戻った最初の点。戻らなければ最後の点）, at, min, under = below_v − min}]（時刻の順。below_v を渡さなければ null。基準の水準は推定しない）。すべての出来事を数える。

| 引数 | 型 | 必須 |
|---|---|---|
| `below_v` | `number | null` |  |
| `t0` | `number | null` |  |
| `t1` | `number | null` |  |

応答:

```ts
{ stats: VoltageStats; lowest: { t: number; volts: number } | null; below_v: number | null; dips: Array<{ t: number; t_end: number; at: number; min: number; under: number }> | null }
```

## recording.density

種類: `read`。画素ごとの出来事の件数（縮図・タイムラインの要約）。長期の記録は事前計算した縮図の和で正確に出す。間引いた・消した分割にかかれば coverage（欠けの一覧）と、端がそこにかかって数えられない画素の inexact（[{pixel, max}]。その画素の counts は残っている出来事だけの下限、max は上限。消した分割なら null。按分で推定しない）。

| 引数 | 型 | 必須 |
|---|---|---|
| `t0` | `number` | ○ |
| `t1` | `number` | ○ |
| `width` | `number` |  |
| `streams` | `Array<string> | null` |  |

応答:

```ts
{ counts: Array<number>; inexact?: Array<{ pixel: number; max: number | null }>; coverage?: RecordingCoverage }
```

## recording.waveform

種類: `read`。記録の波形を画素ごとに '0' / '1' / 'x'（変化）/ '-'（波形が無い）。 長期の記録で波形のファイルを間引いた所は '?' = そのかたまりの中で変化したチャンネル（この画素の中で変わったかは分からない）。

| 引数 | 型 | 必須 |
|---|---|---|
| `t0` | `number` | ○ |
| `t1` | `number` | ○ |
| `width` | `number` |  |

応答:

```ts
{ levels: Array<string> }
```

## recording.waveform_measure

種類: `read`。記録の波形の時刻の範囲 [t0, t1)（タイムベース。省略で全体）を `la_measure` と同じく測る（周波数・周期・デューティ・パルス幅・relations のチャンネル間の時間差）。捕捉（かたまり）ごとに測って合わせ、かたまりをまたぐ周期・時間差は作らない。位置（*_at・violations の at）はタイムベース。skew_unknown は la_measure と同じ（どれかのかたまりでずれが分からなかった ch）。

| 引数 | 型 | 必須 |
|---|---|---|
| `t0` | `number | null` |  |
| `t1` | `number | null` |  |
| `channels` | `Array<number> | null` |  |
| `relations` | `Array<Record<string, unknown>> | null` |  |

応答:

```ts
{ at_unit: "sample" | "timebase"; resolution_ns: number; channels: { [key: string]: MeasureChannel }; relations: Array<MeasureRelation>; skew_unknown: Array<number>; t0: number | null; t1: number | null; chunks: number }
```

## recording.mark

種類: `control`。印とメモを残す（調査の経緯。人と AI の両方が使う）。t（タイムベース）を省くと記録中の記録のいまの時刻、指定するとその時刻（記録中の記録か、開いた記録。時間軸で選んだ時刻に後から印を置く。長期の記録は記録中だけ）。置いた呼び出し元は印の本文の client（{id, name, role}）にデーモンが事実として残す。

| 引数 | 型 | 必須 |
|---|---|---|
| `text` | `string` | ○ |
| `t` | `number | null` |  |

応答:

```ts
{ t: number }
```

## recording.export

種類: `read`。記録を書き出す。format: vcd（波形）/ csv / json（出来事）/ perfetto（Chrome JSON のトレースイベント。Perfetto UI（ui.perfetto.dev）・trace_processor・chrome://tracing で開く。タスク・割り込み・テストポイントの関数・解読・API の操作はスライス、変数（サンプラの語・テストポイントの変数）・電圧・ロジアナのチャンネルはカウンタ、DAP・UART・印は点。ts は記録の最初からの µs）。t0 / t1（タイムベース）・streams は csv・json・perfetto の範囲と種類（perfetto は範囲をまたぐ区間も含める。波形は streams に "waveform"）、waveform・channels は perfetto の波形（channels は vcd も）。vcd の時刻の単位は丸めない最も粗いもの（ns か 100 / 10 / 1 ps。1 ps でも割り切れなければ丸めて $comment に書く）、ch のずれが分からずずらさなかった ch も $comment に。perfetto のロジアナの捕捉のスライスの skew_unknown も同じ。

| 引数 | 型 | 必須 |
|---|---|---|
| `format` | `"vcd" | "csv" | "json" | "perfetto"` | ○ |
| `path` | `string` | ○ |
| `t0` | `number | null` |  |
| `t1` | `number | null` |  |
| `streams` | `Array<string> | null` |  |
| `waveform` | `boolean` |  |
| `channels` | `Array<number> | null` |  |

応答:

```ts
{ path: string } | { path: string; format: "chrome-json"; origin_ticks: number; events: number; slices: number; instants: number; counters: number; tracks: Array<string> }
```

## recording.query

種類: `read`。記録（記録中のもの、または開いたもの）に読み取り専用の SQL を 1 文（AI の集計・検索用）。表: events(t, t_end, stream, kind, a, b, body, unc, rowid)（rowid は入れた順 = 同じ時刻の並びの鍵。SELECT * の列もこの順。時刻はタイムベース、body は JSON で json_extract(body, '$.text') の形で読む）、chunks(t0, t1, path, ticks_per_sample, channels, length)（波形のかたまり）。関数: timebase_hz()、seconds(t)。読み以外（書き・PRAGMA・ATTACH・一時表）は断る。params は ? か :名前 の値。limit 行（既定 1000、上限 100000。多い行は SQL の集計（COUNT・GROUP BY）か t の範囲で分ける）・timeout_s 秒まで（超えたら truncated / 誤り）。結果は columns と rows（列の並び）。長期の記録は t0 / t1 に重なる分割（8 個まで。多ければ断る）をつないで読み、表 segments・summary も読める。t0 / t1 を省くと新しい 8 個（それより多ければ partial = true）。結果に segments（つないだ分割の番号）。

| 引数 | 型 | 必須 |
|---|---|---|
| `sql` | `string` | ○ |
| `params` | `Array<unknown> | Record<string, unknown> | null` |  |
| `limit` | `number` |  |
| `timeout_s` | `number` |  |
| `t0` | `number | null` |  |
| `t1` | `number | null` |  |

応答:

```ts
{ recording: string; columns: Array<string>; rows: Array<Array<number | string | null>>; count: number; truncated: boolean; elapsed_s: number; segments?: Array<number>; partial?: boolean }
```

## target.flash.erase

種類: `control`。Flash を消す。mode: range（address から length バイトにかかるセクタ。境界まで広がる）/ chip（Flash の領域すべて）/ mass（チップの mass erase。保護の解除等を伴うことがある）。ターゲットは止まったまま（プログラムの状態は壊れる）。消した範囲・セクタ数・速度を返す。 既定は消す前にリセットする。preserve でしない。

| 引数 | 型 | 必須 |
|---|---|---|
| `mode` | `"range" | "chip" | "mass"` |  |
| `address` | `number | null` |  |
| `length` | `number | null` |  |
| `preserve` | `boolean` |  |

応答:

```ts
{ start: number; end: number; sectors: number; rate: Rate } | { method: "chip"; rate: Rate } | { method: "mass"; seconds: number }
```

## target.flash.verify

種類: `control`。ファイル（ELF / HEX / BIN。BIN は address）とターゲットのメモリを比べる（書かない・止めない）。match、違うバイト数、違う箇所（番地・期待値・実際の値。先頭 32 箇所）、読み出しの速度。

| 引数 | 型 | 必須 |
|---|---|---|
| `path` | `string` | ○ |
| `address` | `number | null` |  |

応答:

```ts
{ match: boolean; bytes: number; differing_bytes: number; differences: Array<{ address: number; length: number; expected: string; actual: string }>; more_differences: number; read: Rate; image: FlashImage }
```

## target.flash.upload

種類: `control`。画面で選んだ書き込むファイル（ELF / HEX / BIN。中身は data_base64）をデーモンの PC の FPROBE_HOME/uploads/<name> に置き、path・bytes・format（elf / hex / bin）を返す（読まない。ブラウザはファイルの場所を渡せないため。返った path を target.flash・target.flash.verify に渡す。BIN は番地 address が要る）。64 MB まで。

| 引数 | 型 | 必須 |
|---|---|---|
| `name` | `string` | ○ |
| `data_base64` | `string` | ○ |

応答:

```ts
{ path: string; bytes: number; format: "elf" | "hex" | "bin" }
```

## target.flash.read

種類: `control`。ターゲットのメモリを読んでファイルに保存する（.hex / .ihex は Intel HEX、ほかは BIN）。address・length 省略時は起動する Flash（隙間なく続く領域を含む）の全体。止めない。CRC-32 と読み出しの速度を返す。

| 引数 | 型 | 必須 |
|---|---|---|
| `path` | `string` | ○ |
| `address` | `number | null` |  |
| `length` | `number | null` |  |

応答:

```ts
{ path: string; format: string; address: number; bytes: number; crc32: string; read: Rate }
```

## target.flash.regions

種類: `control`。Flash の領域（番地・大きさ・ページ・セクタの並び・消した値・起動するか・アルゴリズムの出所 builtin / pack / flm:<path>）と、`target_flash_algo_add` で足したアルゴリズム。

応答:

```ts
{ regions: Array<FlashRegion>; algorithms: Array<{ name: string; flm: string; address: number | null; size: number | null; ram_address: number | null; ram_size: number | null }> }
```

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

応答:

```ts
{ name: string; pending: true } | FlashRegion
```

## target.flash.algo.remove

種類: `control`。`target_flash_algo_add` で足した領域を外す。

| 引数 | 型 | 必須 |
|---|---|---|
| `name` | `string` | ○ |

応答:

```ts
{ removed: string }
```

## flash.algorithm_guard.set

種類: `control`。書き込みアルゴリズムを動かす間だけチップのレジスタを決めた状態にし、抜けるときに戻す手順（Flash の書き込み・消去・Flash のブレークポイントのどれにも使う）。guard = {enter: [手順], exit: [手順]}、手順は write {address, mask, value}（読んで mask のビットを value に）/ wait {address, mask, value}（(値 & mask) == value まで読む）/ restore {address, mask}（enter の前の値に戻す）/ wait_restored {address, mask}。null で外す。機器固有の守り（例: STM32F2 / F4 / F7 で走っていたプログラムの PLL のままアルゴリズムを動かさない）は本体に持たず、使う側が渡す（プラグイン fprobe.plugins.stm32_flash_clock）。

| 引数 | 型 | 必須 |
|---|---|---|
| `guard` | `Record<string, unknown> | null` |  |

応答:

```ts
{ guard: RegisterGuard | null }
```

## flash.algorithm_guard.get

種類: `read`。`flash_algorithm_guard_set` で渡した書き込みアルゴリズムの間の守り（無ければ guard = null）。

応答:

```ts
{ guard: RegisterGuard | null }
```

## target.flash.benchmark

種類: `control`。書き込み・読み出しの速度を計る。ram_length: 既定の RAM へ書いて読む（中身と走る状態を戻す。0 で省く）。read_length: 起動する Flash を読む（0 で省く）。program_address（と program_length、省略時は 1 セクタ）: そのセクタを消して書いて読み比べ、元の中身・RAM・レジスタ・走る状態に戻す（使っていないセクタを選ぶ）。SWD の周波数と各段のバイト数・秒・bytes_per_s。

| 引数 | 型 | 必須 |
|---|---|---|
| `ram_length` | `number` |  |
| `read_length` | `number` |  |
| `program_address` | `number | null` |  |
| `program_length` | `number | null` |  |

応答:

```ts
{ frequency_hz: number | null; ram: { address: number; write: Rate; read: Rate; verified: boolean } | null; flash_read: { address: number; bytes: number; seconds: number; bytes_per_s: number | null } | null; flash_program: { address: number; length: number; erase: Rate; program: Rate; total: Rate; verified: boolean; restored: boolean } | null }
```

## target.pack.find

種類: `read`。CMSIS-Pack の索引を部品番号で探す（グロブ。ワイルドカードが無ければ部分一致）。索引が無いか update なら取ってくる（ネットに出る）。部品・メーカー・Pack・版・入れてあるか（parts。名前の順に limit 個まで。合った部品の総数 total・切ったか truncated・使った上限 limit）。

| 引数 | 型 | 必須 |
|---|---|---|
| `pattern` | `string` | ○ |
| `update` | `boolean` |  |
| `limit` | `number` |  |

応答:

```ts
{ parts: Array<{ part: string; vendor: string; pack: string; version: string; installed: boolean }>; total: number; truncated: boolean; limit: number }
```

## target.pack.install

種類: `control`。部品番号に合う部品の CMSIS-Pack を落として入れる（ネットに出る）。以後 `target_connect` の target に返した targets の名前が使える（Pack の書き込みアルゴリズム・メモリマップ）。

| 引数 | 型 | 必須 |
|---|---|---|
| `pattern` | `string` | ○ |
| `update` | `boolean` |  |

応答:

```ts
{ packs: Array<{ pack: string; version: string }>; parts: Array<string>; targets: Array<string> }
```

## target.pack.list

種類: `read`。入れてある CMSIS-Pack（名前・版・ファイル）。

応答:

```ts
{ packs: Array<{ pack: string; version: string; path: string }> }
```

## target.pack.load

種類: `control`。手元の .pack ファイルのデバイスをターゲットとして登録する（この実行の間。索引に無い Pack・版を固定したいとき）。`target_connect` に使える名前を返す。

| 引数 | 型 | 必須 |
|---|---|---|
| `path` | `string` | ○ |

応答:

```ts
{ path: string; targets: Array<string> }
```

## snapshot.save

種類: `control`。止まったターゲットの状態を 1 つのファイル（.fsnap、既定は ~/.fprobe/snapshots）に保存する: コアレジスタ（浮動小数点も）・RAM・システム制御空間（SCB・NVIC・SysTick 等。読むと変わるデバッグのレジスタは読まない）・SVD の周辺レジスタ（SVD の readAction のあるものと、skip_registers（"周辺.レジスタ" の glob）に合うものは読まない。readAction の無いレジスタは名前から推さずに読む）・停止の理由と場所・RTOS のタスクの一覧・故障の解析・記録の参照（記録中なら stream "snapshot" にも）・ELF の指紋（embed_elf で ELF も）。halt なら動いていても止める。ranges（[[番地, 長さ], …]）を省くとメモリマップの RAM すべて、flash で Flash も。peripherals は周辺の名前（省略で SVD のすべて、[] で読まない）。

| 引数 | 型 | 必須 |
|---|---|---|
| `name` | `string` |  |
| `path` | `string | null` |  |
| `halt` | `boolean` |  |
| `ranges` | `Array<Array<number>> | null` |  |
| `flash` | `boolean` |  |
| `peripherals` | `Array<string> | null` |  |
| `embed_elf` | `boolean` |  |
| `note` | `string` |  |
| `skip_registers` | `Array<string> | null` |  |

応答:

```ts
{ path: string; created: string; pc: number | null; registers: number; bytes: number; regions: Array<{ address: number; length: number; kind: "ram" | "flash" | "system" | "memory"; name: string }>; peripheral_registers: number; elf: { name: string; size: number; sha256: string; build_id: string | null; embedded: boolean } | null; target?: string | null; halt?: { reason: string; pc: number; where: string; function?: string; file?: string; line?: number }; elf_path?: string | null; svd?: { source: string; origin: string } | null; recording?: { name: string; time: number | null } | null; note?: string; rtos: { error: string } | { rtos: string | null; tasks: number; current: string | null } | null; fault: { error: string } | { faulted: boolean; causes: Array<string>; exception: string | null; at: string | null } | null; skipped: Array<{ path: string; reason: string }> }
```

## snapshot.open

種類: `control`。スナップショットを開く（未接続のとき）。以後 `debug_backtrace`・`debug_evaluate`・`debug_frame_variables`・`rtos_tasks`・`rtos_backtrace`・`target_peripheral_read`・`target_fault`・`target_memory_view` がスナップショットを読む（書き込み・実行は不可）。ELF は elf、無ければ埋め込んだもの、それも無ければ保存した時の場所（指紋が合うときだけ）。`target_connect` で閉じる。

| 引数 | 型 | 必須 |
|---|---|---|
| `path` | `string` | ○ |
| `elf` | `string | null` |  |

応答:

```ts
{ path: string; created: string; pc: number | null; registers: number; bytes: number; regions: Array<{ address: number; length: number; kind: "ram" | "flash" | "system" | "memory"; name: string }>; peripheral_registers: number; elf: { name: string; size: number; sha256: string; build_id: string | null; embedded: boolean } | null; target?: string | null; halt?: { reason: string; pc: number; where: string; function?: string; file?: string; line?: number }; elf_path: string | null; svd?: { source: string; origin: string } | null; recording?: { name: string; time: number | null } | null; note?: string; rtos: { error: string } | { rtos: string | null; tasks: number; current: string | null } | null; fault: { error: string } | { faulted: boolean; causes: Array<string>; exception: string | null; at: string | null } | null; warnings: Array<string> }
```

## snapshot.close

種類: `control`。開いたスナップショットを閉じる。

応答:

```ts
{ closed: boolean }
```

## snapshot.info

種類: `read`。スナップショットの説明（path 省略で開いているもの）: 時刻・停止の理由と場所・区画・ELF の指紋・記録の参照と、保存した時の RTOS のタスクの一覧・故障の解析・読まなかった周辺レジスタ。

| 引数 | 型 | 必須 |
|---|---|---|
| `path` | `string | null` |  |

応答:

```ts
{ open: false } | { open: boolean; path: string; created: string; pc: number | null; registers: number; bytes: number; regions: Array<{ address: number; length: number; kind: "ram" | "flash" | "system" | "memory"; name: string }>; peripheral_registers: number; elf: { name: string; size: number; sha256: string; build_id: string | null; embedded: boolean } | null; target?: string | null; halt?: { reason: string; pc: number; where: string; function?: string; file?: string; line?: number }; elf_path?: string | null; svd?: { source: string; origin: string } | null; recording?: { name: string; time: number | null } | null; note?: string; rtos: Json; fault: FaultReport | { error: string } | null; skipped: Array<{ path: string; reason: string }> }
```

## snapshot.list

種類: `read`。保存したスナップショットの一覧（新しい順。止まった場所・ELF・メモ）。

応答:

```ts
{ directory: string; snapshots: Array<{ name: string; path: string; bytes: number; created: string; pc: number | null; where: string | null; reason: string | null; elf: string | null; note: string } | { name: string; path: string; error: string }> }
```

## snapshot.export_core

種類: `read`。スナップショット（path 省略で開いているもの）を ELF のコアファイル（NT_PRSTATUS・PT_LOAD。GDB の core-file で読める）に書き出す。out 省略で拡張子 .core。

| 引数 | 型 | 必須 |
|---|---|---|
| `path` | `string | null` |  |
| `out` | `string | null` |  |

応答:

```ts
{ path: string; bytes: number; gdb: string }
```

## snapshot.restore

種類: `control`。接続中のターゲットをスナップショットの状態に戻す: 止めて、ELF のコード（スナップショットの RAM の外）が同じかを確かめ（force で省く）、RAM・システムのレジスタ（VTOR・優先度・割り込みの許可と保留・SysTick・CPACR・FPU）・peripherals に挙げた周辺・コアレジスタを書く。resume で再開。Flash は書かない。

| 引数 | 型 | 必須 |
|---|---|---|
| `path` | `string` | ○ |
| `resume` | `boolean` |  |
| `peripherals` | `Array<string> | null` |  |
| `force` | `boolean` |  |

応答:

```ts
{ path: string; ram_bytes: number; system: Array<string>; peripherals: Array<string>; registers: number; code_checked: boolean; state: CoreState; target?: string; frequency_hz?: number; wire?: "swd" | "jtag"; core?: number; arch?: string; hart?: number; debug_module?: DebugModuleInfo; architecture?: "armv7-a" | "armv6"; debug_version?: number; breakpoints?: number; watchpoints?: number; core_count?: number; debug_base?: number; apb_ap?: number; discovery?: "ahb_system_map" | "rom_table" | "part_table"; jtag_chain?: Array<number>; tap?: number; pc?: number; where?: string; function?: string; file?: string; line?: number; halt_reason?: "request" | "breakpoint" | "bkpt_instruction" | "watchpoint" | "vector_catch" | "external"; halt_bits?: Array<"HALTED" | "BKPT" | "DWTTRAP" | "VCATCH" | "EXTERNAL">; debug_mode?: "monitor"; fatal?: FatalState; rescue_dp?: number; cores?: Array<CoreEntry> }
```

## rules.check

種類: `read`。規則の検査（宣言的な規則集を記録に当てる）。rules（辞書）か path（JSON / YAML のファイル）のどちらか: {name, requirements: {要件 ID: 文}, rules: [{id, kind, requirement, severity, allow_empty, ...}]}。kind: interval（start・end・min_us・max_us）/ period（event・period_us と tolerance_us、または min_us・max_us）/ sequence（trigger・steps・within_us・forbid）/ count（event・min・max・window_us）/ never（event）/ value（event・field・min・max・equals）/ states（event・field・allowed = [from, to] の列・labels・initial）。条件は `recording_find` と同じ、field は a / b / kind / body.<名前>。結果は規則ごとの verdict（pass / fail / warn / no_data）・expected・measured・violations（t・at_us・kind・message。規則ごとに limit 件まで: violations_total = 全数、violations_limit = 使った上限）と全体の verdict。mark（既定 true）で違反を記録の stream "rule"（kind violation）に派生イベントとして書く（同じ規則集の前の印は置き換え）。baseline（`recording_list` の name）で基準の記録の実測と比べる（baseline.delta）。範囲は t0 / t1（タイムベース）。

| 引数 | 型 | 必須 |
|---|---|---|
| `rules` | `Record<string, unknown> | null` |  |
| `path` | `string | null` |  |
| `t0` | `number | null` |  |
| `t1` | `number | null` |  |
| `baseline` | `string | null` |  |
| `mark` | `boolean` |  |
| `limit` | `number` |  |

応答:

```ts
RulesCheck
```

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

応答:

```ts
{ states: { [key: string]: { entries: number; first_t: number; dwell_us: { min: number; max: number; mean: number; total: number } | null } }; transitions: Array<{ from: string; to: string; count: number; first_t: number }>; changes: number; initial: string | null; dot: string }
```

## rules.report

種類: `read`。証跡を書く: 最後の `rules_check` と `inject_run` の結果を、要件ごとの判定（要件 → 規則・試行 → 判定）・規則ごとの期待値と実測・違反の時刻・基準との差・故障注入の試行・記録と ELF（SHA-256）・規則集（SHA-256）を含む 1 つの文書（format markdown / json）として path に書く。

| 引数 | 型 | 必須 |
|---|---|---|
| `path` | `string` | ○ |
| `format` | `"markdown" | "json"` |  |

応答:

```ts
{ path: string; verdict: "pass" | "warn" | "no_data" | "not_triggered" | "error" | "inject_error" | "fail"; requirements: { [key: string]: "pass" | "warn" | "no_data" | "not_triggered" | "error" | "inject_error" | "fail" | "not_covered" } }
```

## rules.last

種類: `read`。最後の `rules_check` と `inject_run` の結果（`rules_report` が証跡に書くもの。機器には触れない）: rules・inject がそれぞれ {result（その呼び出しの応答のまま）, at（UNIX 秒）, caller（呼び出し元 {id, name, role}。分からなければ null）}。まだ無ければ null。

応答:

```ts
{ rules: { result: RulesCheck; at: number | null; caller: { id: number; name: string; role: string } | null } | null; inject: { result: InjectRun; at: number | null; caller: { id: number; name: string; role: string } | null } | null }
```

## inject.run

種類: `control`。故障注入の台本を試行する（クロストリガの割り込み書き込み: 条件の成立から数 µs で、ハードウェアが任意の番地へ 1 語書く）。script（辞書）か path（JSON / YAML）: {name, requirements, trials: [{id, requirement, when, write, expect, restore, timeout_s, observe_s, repeat}]}。timeout_s（条件の成立を待つ秒 = not_triggered の判定）と observe_s（注入の後に見る秒 = pass / fail の判定）は必須（既定は無い）で、試行の結果にも入る。when は 1 つ: pattern {mask, value, rise, fall}（ピン）/ sequence（`xtrig_set_sequence` と同じ stages・pattern_a・pattern_b）/ dap_match（`xtrig_set_match` と同じ）/ host {delay_s}。write: {address（数か ELF の変数名。4 の倍数）, value か flip（仕掛けた時点の値とのビット反転）, ap}。expect: {state: running|halted} / {expression: C の式, equals|not_equals|min|max} / {memory: {address, size}, 同じ比較} / {rules: [規則]}（注入から observe_s の間の記録。記録が要る）。restore: {write: [{address, value, size}], reset: 種類, resume}。試行ごとの verdict: pass / fail / not_triggered / inject_error / error と、注入の時刻 t（タイムベース）。記録中なら stream "inject"（kind trial）に入る。最後に割り込み書き込みを既定（停止）へ戻す。

| 引数 | 型 | 必須 |
|---|---|---|
| `script` | `Record<string, unknown> | null` |  |
| `path` | `string | null` |  |

応答:

```ts
InjectRun
```

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

応答:

```ts
{ id: number; event: "connect" | "disconnect" | "reset" | "flash" | "resume" | "halt"; phase: "before" | "after"; actions: Array<HookAction>; name: string; required: boolean; enabled: boolean; operations: Array<string> }
```

## hooks.remove

種類: `control`。フックを外す（id、または all=true ですべて）。

| 引数 | 型 | 必須 |
|---|---|---|
| `id` | `number | null` |  |
| `all` | `boolean` |  |

応答:

```ts
{ removed: number }
```

## hooks.list

種類: `read`。登録したフックの一覧（id・event・phase・actions・name・required・enabled）。AI と人が同じものを見る。

応答:

```ts
{ hooks: Array<Hook> }
```

## hooks.log

種類: `read`。フックの実行の記録（最新 last 件。seq・時刻・フック・event・phase・きっかけの操作 method と利用者 client・動作ごとの ok と value か error・所要時間）。

| 引数 | 型 | 必須 |
|---|---|---|
| `last` | `number` |  |

応答:

```ts
{ entries: Array<{ seq: number; time: number; hook: number; name: string; event: "connect" | "disconnect" | "reset" | "flash" | "resume" | "halt"; phase: "before" | "after"; method: string | null; client: string; seconds: number; results: Array<{ action: "evaluate" | "mark" | "call"; target: string; ok: boolean; value?: Json; error?: string }> }> }
```

## hooks.save

種類: `read`。フックの宣言を JSON のファイルに書く（`hooks_load` で読み直せる。プロジェクトに置いて人と AI で共有する）。

| 引数 | 型 | 必須 |
|---|---|---|
| `path` | `string` | ○ |

応答:

```ts
{ path: string; hooks: number }
```

## hooks.load

種類: `control`。JSON のファイル（`hooks_save` の形 {hooks: [...]}）からフックを読む。replace（既定 true）なら今のフックを置き換え、false なら足す。全部を確かめてから入れる。

| 引数 | 型 | 必須 |
|---|---|---|
| `path` | `string` | ○ |
| `replace` | `boolean` |  |

応答:

```ts
{ loaded: number; hooks: Array<Hook> }
```

## heap.scan

種類: `control`。ヒープを辿る（ELF のシンボルから。DWARF 不要）: allocator = freertos（heap_4 / heap_5）/ newlib_nano / newlib（dlmalloc）。省略で ELF にあるもの全部。使用量・空き・最大の空き・断片化（1 − 最大の空き / 空きの合計）・大きさの分布・アロケータのカウンタと、壊れ（issues: bad_size = 大きさが不正。previous_used は直前の使用中のブロック（事実。壊した者は推定しない） / bad_link / bad_end / free_list_loop・unsorted・outside / free_not_listed / listed_not_free / unmerged_free / bad_padding / bad_links・bad_footer（dlmalloc）/ counter_mismatch）。heap_5 の領域は xHeapRegions か regions（[[先頭, 大きさ]]）、newlib-nano のヒープの終わりは sbrk の変数か end。blocks でブロックの一覧（先頭から）、compare で前回の走査からの増減（changes: 増えた new・消えた gone 使用中のブロックを番地の順に changes_limit 個まで。総数 new_count・gone_count、切ったか new_truncated・gone_truncated、使った上限 limit）。`heap_trace_start` 中なら使用中のブロックを呼び出し元ごとに（callers）。止まっているときだけ（live=true で走行中にも: 一貫しない）。

| 引数 | 型 | 必須 |
|---|---|---|
| `allocator` | `"freertos" | "newlib_nano" | "newlib" | null` |  |
| `regions` | `Array<Array<number>> | null` |  |
| `end` | `number | null` |  |
| `live` | `boolean` |  |
| `blocks` | `number` |  |
| `compare` | `boolean` |  |
| `changes_limit` | `number` |  |
| `regions_symbol` | `string | null` |  |
| `break_symbol` | `string | null` |  |

応答:

```ts
{ heaps: Array<HeapScan>; halted: boolean; consistent: boolean }
```

## heap.trace.start

種類: `control`。malloc / free の記録を始める（止めずに）: 確保の関数（FreeRTOS pvPortMalloc・newlib _malloc_r）の入口で大きさと呼び出し元、戻ったところで返した番地を、解放の関数（vPortFree・_free_r）の入口で番地を読む（ブレークポイント。当たるたびにコアを数 ms 止める）。calloc / realloc は中の確保・解放として残る。allocators で絞る（省略で ELF にあるもの）。呼び出し元は戻り先の関数。skip_functions（関数の名前）を渡すと、戻り先がその関数なら呼び出し履歴で外へ辿り、最初の skip_functions に無い関数を呼び出し元にする（飛ばしたフレームは出来事の skipped、巻き戻せない・すべて飛ばす関数なら unwind_error。名前の表は本体に持たない。標準の包みの名前はプラグイン fprobe.plugins.heuristics.ALLOCATOR_WRAPPERS）。結果の skip_functions = 使う名前。記録中なら stream "heap"（kind alloc / free、a = 番地、b = 大きさ）にも入る。→ `heap_trace_report`・`heap_trace_events`・`heap_trace_stop`。

| 引数 | 型 | 必須 |
|---|---|---|
| `allocators` | `Array<"freertos" | "newlib_nano" | "newlib"> | null` |  |
| `skip_functions` | `Array<string> | null` |  |
| `issues_limit` | `number` |  |

応答:

```ts
{ functions: Array<{ function: string; op: "alloc" | "free"; address: number }>; skip_functions: Array<string>; notes: Array<string> }
```

## heap.trace.stop

種類: `control`。malloc / free の記録を止める（ブレークポイントを外す。記録は `heap_trace_report` で読める）。

応答:

```ts
{ allocations: number; frees: number; live: { count: number; bytes: number }; peak_bytes: number }
```

## heap.trace.report

種類: `read`。malloc / free の記録の要約: 確保・解放の回数、生きている確保（数・バイト）、最大の使用量、誤り（double_free・failed_allocation・reused_live_pointer）、呼び出し元ごとの生きている確保（live_by_caller: min_age_s 秒より古いもの。バイトの多い順に top 個。callers_total = 切る前の呼び出し元の数・truncated = 切ったか・top = 使った値。各呼び出し元の pointers は先頭の 8 個で pointers_truncated = 切ったか。live_counts = 記録の最初と最後の間を 4 つに分けた 5 つの時点での生きている数。リークかは判定しない）、使用量の推移（series: 記録を始めてからの秒, バイト）。 時刻は PC の時計（time_source = host_received: 確保・解放のブレークポイントの止まりを PC が処理した時刻。対象の出来事はそれより前）。

| 引数 | 型 | 必須 |
|---|---|---|
| `min_age_s` | `number` |  |
| `top` | `number` |  |
| `series_points` | `number` |  |
| `live_points` | `number` |  |

応答:

```ts
{ callers_total: number; truncated: boolean; top: number; allocations: number; frees: number; untracked_frees: number; live: { count: number; bytes: number }; peak_bytes: number; issues: Array<{ kind: "failed_allocation" | "reused_live_pointer" | "double_free"; t: number; pointer: number; caller: string; detail: string }>; issues_total: number; issues_truncated: boolean; issues_limit: number; live_by_caller: Array<{ caller: string; count: number; bytes: number; pointers: Array<number>; live_counts: Array<[number, number]>; pointers_truncated: boolean; oldest_age_s: number }>; series: Array<[number, number]>; series_total: number; series_downsampled: boolean; series_points: number; live_points: number; active: boolean; elapsed_s: number; time_source: "host_received"; notes: Array<string>; functions: Array<string>; skip_functions: Array<string> }
```

## heap.trace.events

種類: `read`。記録した確保・解放の出来事（op・pointer・size・caller・skipped = 呼び出し元を探すとき飛ばしたフレームの関数・t = 記録を始めてからの秒）を古い順に。seq が since より後を limit 件、next を次の since に。出来事は新しい kept_limit 件だけ持つ: dropped = since より後で持つ数を超えて捨てた出来事の数。

| 引数 | 型 | 必須 |
|---|---|---|
| `since` | `number` |  |
| `limit` | `number` |  |

応答:

```ts
{ events: Array<{ seq: number; t: number; op: "alloc" | "free"; pointer: number; size: number; caller: string; allocator: string; skipped: Array<string>; unwind_error?: string }>; next: number; dropped: number; kept_limit: number }
```

## recording.segments

種類: `read`。長期の記録の分割の一覧（no・path・state = open / sealed / deleted・時刻の範囲 t0 / t1・件数・バイト数・dropped = 間引いた stream）と方針（policy）。普通の記録は segmented = false。

応答:

```ts
{ segmented: false; recording: string } | { segmented: true; recording: string; policy: { segment_s: number; segment_bytes: number | null; max_bytes: number | null; decimate_after_s: number | null; decimate_streams: Array<string> } | null; segments: Array<{ no: number; path: string; state: "open" | "sealed" | "deleted"; t0: number | null; t1: number | null; events: number; bytes: number; dropped: Array<string> }>; bytes: number }
```

## recording.overview

種類: `read`。記録の全体の縮図（ミニマップ。Tracealyzer の Trace Preview の相当）: 画素ごと・stream ごとの出来事の件数（streams）と合計（total）、波形（waveform: チャンネルごとに '0' / '1' / 'x' / '-'）と、波形のある所が全部同じ値の ch（unchanged_channels = 記録の中で変化が無い）、分割の状態（segments）。画素の境は edges（タイムベース）。長期の記録は事前計算した索引から出すので 30 日でも速く、消した分割の件数も残る。streams で種類を絞る。 長期の記録で波形のファイルを間引いた所は '?' = そのかたまりの中で変化したチャンネル（この画素の中で変わったかは分からない。unchanged_channels には入れない）。

| 引数 | 型 | 必須 |
|---|---|---|
| `width` | `number` |  |
| `streams` | `Array<string> | null` |  |

応答:

```ts
{ recording: string; span: [number, number] | null; edges: Array<number>; pixel_ticks?: number; total: Array<number>; streams: { [key: string]: Array<number> }; waveform: Array<string>; segments: Array<{ no: number; state: "open" | "sealed" | "deleted"; t0: number | null; t1: number | null; dropped: Array<string> }>; unchanged_channels: Array<number> }
```

## recording.la_stream.start

種類: `control`。記録中に、ロジアナを止めずに流し続けて記録へ入れる（`recording_la_stream_stop` まで。日単位も可）。chunk_s 秒ごとの波形のかたまりにし、時刻はデバイスのタイムベース（サンプル 0 の時刻は回路が記録しないので、anchor は確かに言える範囲 [t_lo, t_hi]（始める前後の読み、トリガがあればその時刻が上の端。根拠 basis）を返し、波形は範囲の中点 ticks に置く。uncertainty_ticks = 範囲の半分。範囲の中のどこかは推定しない）。rle = true は変化点圧縮（遅い信号向き）、false は RAW（12.5 MS/s 程度まで）。イベントも流す構成（`probe.design.get` の features.la_stream_events。fprobe_la）では、イベント記録（DAP・UART・SWO・補助の出来事）も同じ流れでデバイスの時刻のまま記録へ入り続ける（止めると取り込みは通常の周期の読みに戻る。欠けも二重も出ない）。ほかの構成では、流している間はイベント記録を取り込めない。どちらでも、流している間はロジアナ・イベント記録の道具は使えない（読み出しの経路が同じ）。溢れたら（ロジアナの未読がリングに迫る・イベントの未読がイベントのリングの半分）止まり、記録に印（annotation la_overrun）が残る。
pack_channels（8・4・2。rle = false のときだけ。ケイパビリティ la_pack）は、使うチャンネル（ch 0〜n − 1）だけを詰めて流す（データ量が 1/2・1/4・1/8。選んだチャンネルは欠けない。選ばなかった上位のチャンネルは記録されない。8 ch で 50 MS/s、4 ch で 100 MS/s が 50 MB/s）。
gaps = true（ケイパビリティ la_stream_gaps）: 溢れても止めず、回路が捨てた拍の区間（欠け）を記録の la の出来事 gap（時刻 = 欠けの最初のサンプルの位置、t_end = 欠けの後の最初のサンプルの位置、body の samples・duration_ticks・beat）にして続ける（欠けの区間に波形は置かない）。status の gaps・gap_samples・unplaced_beats は事実の数。gap_hold_bytes（gaps = true のとき必ず指定。既定は無い）= 欠けの記録が届くまで位置の決まらない拍を PC に持つ上限（バイト）。超えたら推定で置かずに流れを止める（status の error に StreamHoldExceeded）。false（既定）は今までどおり溢れたら止まる。
control_read_retries（既定 0）= 流している間の見回りの CSR の READ（読みに副作用が無い）の応答が時間切れになったとき、送り直してよい回数（使う側が明示する。0 なら送り直さず止まる。止める WRITE は送り直さない）。status の control = 送り直した READ と読み捨てた遅れた応答の数（事実）。

| 引数 | 型 | 必須 |
|---|---|---|
| `rate_hz` | `number | null` |  |
| `rle` | `boolean` |  |
| `chunk_s` | `number` |  |
| `pack_channels` | `8 | 4 | 2 | null` |  |
| `gaps` | `boolean` |  |
| `gap_hold_bytes` | `number | null` |  |
| `control_read_retries` | `number` |  |

応答:

```ts
LaRecordingStatus
```

## recording.la_stream.stop

種類: `control`。ロジアナの流し続けを止める（残りを出し切って記録へ入れる）。サンプル数・長さ・溢れ（overrun）・時刻の基準（anchor）・かたまりの数・記録へ入れたイベントの数（events。イベントも流す構成のとき）を返す。

応答:

```ts
LaRecordingStatus
```

## debug.elf.watch

種類: `control`。読み込んだ ELF の更新の見張りの設定（既定で有効）。更新（再ビルド）を見つけると auto_reload なら読み込み直し、通知 elf.changed（AI には次の道具の結果の notices）で知らせる。ターゲットに書いた中身と違えば target_image = stale（`target_flash` で書き直す）。interval_s は見回りの間隔。

| 引数 | 型 | 必須 |
|---|---|---|
| `enabled` | `boolean` |  |
| `auto_reload` | `boolean` |  |
| `interval_s` | `number` |  |

応答:

```ts
ElfStatus
```

## debug.elf.status

種類: `read`。読み込んだ ELF と更新の見張りの様子（機器には触れない）: path・loaded_at・file_changed（読み込んだ後にファイルが変わったか）・target_image（current / stale = 書き直しが要る / unknown）・changes・reloads・last（最後の更新の知らせ）・error。

応答:

```ts
ElfStatus
```

## debug.halt_risk

種類: `read`。止める操作（中断・ブレークポイント・ステップ・関数の呼び出し）の前の手がかり（ELF のシンボルだけ。機器には触れない。事実だけで判定はしない）: names・patterns（`debug.halt_risk.set` で使う側が渡した名前・正規表現。既定は空で、本体は名前の表を持たない）、matched（どれかに合った）、matches（合った条件ごと: query、by = name / pattern、count、symbols = [{name, kind = function / object 等, address}] を全部）、monitor_in_elf（止めずにデバッグするモニタがファームにあるか。あれば target.debug_mode.set の mode="monitor"）。debug_mode（いまの方式 halt / monitor）、confirm_before_halt（matched かつ debug_mode が halt = 止める操作がコアを止める。人の画面はこれで止める前に確かめる）。合わなかった条件は matches に入らない。名前の慣習（関数名の接頭辞等）で無線・USB・ウォッチドッグ・RTOS に分けるのはプラグイン fprobe.plugins.heuristics の halt_risk_conventions（経験則。結果に heuristic・assumption が付く）。

応答:

```ts
{ elf: string; names: Array<string>; patterns: Array<string>; matched: boolean; matches: Array<{ query: string; by: "name" | "pattern"; count: number; symbols: Array<{ name: string; kind: "function" | "object"; address: number }> }>; monitor_in_elf: boolean; debug_mode: "halt" | "monitor"; confirm_before_halt: boolean }
```

## debug.halt_risk.get

種類: `read`。止める前の手がかり（`debug.halt_risk`）で探す今の条件: names・patterns（`debug.halt_risk.set` で渡されたもの。既定は空）。ELF に依らない。

応答:

```ts
{ names: Array<string>; patterns: Array<string> }
```

## debug.halt_risk.set

種類: `control`。`debug.halt_risk` で ELF から探す条件を置き換える: names（シンボルの名前。完全一致）、patterns（正規表現。Python の re.search。大文字小文字を区別しないなら (?i)）。渡さなければ空（何も探さない）。読めない正規表現は誤り。条件はデーモンが覚え、画面の止める前の確かめもこの条件で出る。結果は渡した条件 {names, patterns}。

| 引数 | 型 | 必須 |
|---|---|---|
| `names` | `Array<string>` |  |
| `patterns` | `Array<string>` |  |

応答:

```ts
{ names: Array<string>; patterns: Array<string> }
```

## watch.trigger

種類: `control`。観測している変数の取得を条件で始める・止める（`watch_set` の後。結果は `watch_capture`）。start / stop: {"variable", "op"（> >= < <= == !=）, "value", "edge"（偽→真の変化で）} か {"event": "xtrig"}（クロストリガの割り込み書き込みの時刻。`xtrig_route` の to_inject にロジアナのトリガ・DAP の一致・多段の条件をつなぐ）。start を省くといまから。pre_s: 始まりより前の分（プリトリガ）、post_s: 始まりから取る長さ、max_samples: 件数の上限。

| 引数 | 型 | 必須 |
|---|---|---|
| `start` | `{ variable?: string; op?: ">" | ">=" | "<" | "<=" | "==" | "!="; value?: number; edge?: boolean; event?: "xtrig" } | null` |  |
| `stop` | `{ variable?: string; op?: ">" | ">=" | "<" | "<=" | "==" | "!="; value?: number; edge?: boolean; event?: "xtrig" } | null` |  |
| `pre_s` | `number` |  |
| `post_s` | `number | null` |  |
| `max_samples` | `number` |  |

応答:

```ts
{ state: "armed" | "capturing" | "done"; reason: "stop" | "post" | "max_samples" | "cancelled" | null; start: { event: string } | { variable: string; op: string; value: number; edge: boolean } | null; stop: { event: string } | { variable: string; op: string; value: number; edge: boolean } | null; pre_s: number; post_s: number | null; max_samples: number; t_start_us: number | null; t_end_us: number | null; count: number; dropped: number; lost_events: number }
```

## watch.capture

種類: `read`。条件つきの取得（`watch_trigger`）の状態と結果（機器には触れない）。終わるまで wait_s 待つ。state: armed / capturing / done（reason: stop / post / max_samples / cancelled）、t_start_us・t_end_us、samples（変数ごとの値の列。時刻は `watch_series` と同じ軸の µs）。 値は `debug_evaluate` と同じ形（kind を持つ object）。番地の式（"0x20000000:4"）の値は kind = int（符号なし整数）。

| 引数 | 型 | 必須 |
|---|---|---|
| `wait_s` | `number` |  |

応答:

```ts
{ state: "armed" | "capturing" | "done"; reason: "stop" | "post" | "max_samples" | "cancelled" | null; start: { event: string } | { variable: string; op: string; value: number; edge: boolean } | null; stop: { event: string } | { variable: string; op: string; value: number; edge: boolean } | null; pre_s: number; post_s: number | null; max_samples: number; t_start_us: number | null; t_end_us: number | null; count: number; dropped: number; lost_events: number; samples: { [key: string]: Array<{ time_us: number; value: Value }> } }
```

## watch.write

種類: `control`。走行中の変数に書く（止めない）。expression: 大域・静的の C の式（ポインタをたどってもよい）か番地（"0x20000000:4" は整数）。value: 数・列挙の名前・真偽。書いた後に読み直した値を返す。ビットフィールドは読み・変え・書き（atomic = false: ターゲットが同じ語を同時に書くと失われうる）。 読み直した value は `debug_evaluate` と同じ形（番地の式は kind = int）。

| 引数 | 型 | 必須 |
|---|---|---|
| `expression` | `string` | ○ |
| `value` | `number | string | boolean` | ○ |

応答:

```ts
{ expression: string; address: string; type: string | null; value: Value; atomic: boolean }
```

## watch.buffer_start

種類: `control`。ターゲット側のバッファを吸い出す（Snapshot 方式。速い変化を取りこぼさず、ターゲットの周期に同期する）。ファームが配列 array に値（構造体でも）を書いてから head を進める（counter = true: 書いた総数。false: 次に書く位置）。rate_hz ごとに head を読み、増えた要素だけを読んで DWARF の型で解釈する。読みは `watch_buffer_read`。

| 引数 | 型 | 必須 |
|---|---|---|
| `array` | `string` | ○ |
| `head` | `string` | ○ |
| `counter` | `boolean` |  |
| `rate_hz` | `number` |  |

応答:

```ts
{ array: string; head: string; counter: boolean; rate_hz: number; address: string; length: number; element: string; total: number; lost: number; error: string | null }
```

## watch.buffer_read

種類: `read`。吸い出した要素（機器には触れない）: samples = [{seq（通し番号）, time_us（PC が読んだ時刻）, value}]。since を省くと前回の続きから（読むと進む）、渡すとそこから（画面用）。lost は一周されて読めなかった要素の数、next は次の since。 evicted = ホストで持つ数の上限（kept_limit 件）を超えて捨てた、since より後の要素の数。 値は `debug_evaluate` と同じ形（kind を持つ object）。番地の式（"0x20000000:4"）の値は kind = int（符号なし整数）。

| 引数 | 型 | 必須 |
|---|---|---|
| `since` | `number | null` |  |
| `limit` | `number` |  |

応答:

```ts
{ samples: Array<{ seq: number; time_us: number; value: Value }>; next: number; evicted: number; kept_limit: number; array: string; head: string; counter: boolean; rate_hz: number; address: string; length: number; element: string; total: number; lost: number; error: string | null }
```

## watch.buffer_stop

種類: `control`。ターゲット側のバッファの吸い出しを止める（吸い出した要素は `watch_buffer_read` で読める）。

応答:

```ts
{ array: string; head: string; counter: boolean; rate_hz: number; address: string; length: number; element: string; total: number; lost: number; error: string | null }
```

## rtt.measure

種類: `read`。RTT の受信の速さを duration_s の間はかる（機器には触れない。背景の読みの数え上げを比べる。ターゲットが出し続けている間に呼ぶ）: bytes_per_s（チャンネルごとと合計）、polls_per_s、empty_poll_ratio、reads_per_s・read_bytes_per_s（デバッグポートの読み）、efficiency（受けたバイト ÷ 読んだバイト）、wake。

| 引数 | 型 | 必須 |
|---|---|---|
| `duration_s` | `number` |  |

応答:

```ts
{ duration_s: number; up: { [key: string]: { bytes: number; bytes_per_s: number } }; bytes: number; bytes_per_s: number; polls: number; polls_per_s: number; empty_poll_ratio: number | null; reads: number; reads_per_s: number; read_bytes_per_s: number; writes_per_s: number; efficiency: number | null; wake: { mode: "poll"; interval_s: number } | { mode: "sampler"; rate_hz: number; interval_s: number; samples: number; wakes: number; idle: number; full_polls: number; lost_events: number } }
```

## stream.serve

種類: `control`。生の流れ stream（rtt0〜 = RTT の上り / swo / uart / semihosting）を TCP の 127.0.0.1:port（0 なら空いているポート）で公開する（SEGGER の RTT Telnet サーバ・OpenOCD の RTT ポート・orbuculum に相当）。つながった全員が同じバイトを受け、送ったバイトはターゲットへ書く（rttN は下り N、uart、semihosting は標準入力。swo と writable = false は読むだけ）。書き込みは調停と操作の記録を通る。protocol: raw / telnet。流れの出どころは `rtt_start`・`swo_start` で別に始める。LAN には開かない。

| 引数 | 型 | 必須 |
|---|---|---|
| `stream` | `string` | ○ |
| `port` | `number` |  |
| `protocol` | `"raw" | "telnet"` |  |
| `writable` | `boolean` |  |

応答:

```ts
StreamServed
```

## stream.close

種類: `control`。流れの公開をやめる（つながっている相手は切れる）。

| 引数 | 型 | 必須 |
|---|---|---|
| `stream` | `string` | ○ |

応答:

```ts
StreamServed
```

## stream.log_start

種類: `control`。流れ stream をファイル path に書き始める（SEGGER の RTT Logger に相当。append = false なら書き直す）。timestamps: 行の頭にこの PC の時刻（文字の流れ用）。

| 引数 | 型 | 必須 |
|---|---|---|
| `stream` | `string` | ○ |
| `path` | `string` | ○ |
| `timestamps` | `boolean` |  |
| `append` | `boolean` |  |

応答:

```ts
StreamLog
```

## stream.log_stop

種類: `control`。流れのファイルへの記録をやめる。

| 引数 | 型 | 必須 |
|---|---|---|
| `stream` | `string` | ○ |

応答:

```ts
StreamLog
```

## stream.list

種類: `read`。公開中の流れ（ポート・相手ごとの送ったバイトと捨てたバイト・書き込みの誤り）・ファイルへの記録・流れの出どころ（RTT の上り・SWO の受信中か・背景の読みの誤り）。機器には触れない。

応答:

```ts
{ served: Array<StreamServed>; logs: Array<StreamLog>; sources: { rtt: Array<string>; swo: boolean; errors: { [key: string]: string } } }
```

## log.start

種類: `control`。ログの流れの解読を始める（止めずに。すぐ戻る）。format: defmt（ELF の .defmt の表。枠は ELF の _defmt_encoding_（rzcobs / raw）、無ければ framing で明示（仮定しない）。RTT は既定で名前 "defmt" のチャンネル）/ trice（til・li = til.json・li.json、framing tcobs / cobs / none。行頭のタグ（err: 等）をチャンネル（channel）とするのは channel_levels（タグ → trace / debug / info / warn / error、重要度なしは null）で明示したタグだけ（名前の形では決めない。本文は書き換えずタグも残る）。回の番号の飛びは数えて notes に待った番号と来た番号（原因は解釈しない））/ compact（書式の文字列の番地 + 引数の語。ELF から引く）/ memfault（ELF の log_fmt。transport cbor / text = ML:…: の行）/ text（行）。source: rtt（channel は番号か名前。省けば defmt は "defmt"（無ければ誤り）、ほかは SEGGER RTT の端末のチャンネル 0）/ swo（ITM の port。swo_baud・core_clock_hz で受信と TPIU も設定）/ uart（プローブの UART の受信。時刻は FPGA が受けた時刻）。ELF は elf か読み込み済みのもの。annotate なら本文の中のコードの番地を関数・ファイル:行に。読むのは `log_read`。記録中なら stream "log"（kind = 重要度、解読できないものは undecoded）。

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
| `channel_levels` | `{ [key: string]: "trace" | "debug" | "info" | "warn" | "error" | null } | null` |  |

応答:

```ts
LogState
```

## log.stop

種類: `control`。ログの受け取りを止める（貯めた行は `log_read` で読める）。要約（行の数・重要度ごとの数・解読できなかった数）。

応答:

```ts
LogState
```

## log.status

種類: `read`。ログの受け取りの様子（形式・行の数・重要度ごとの数・解読できなかった数・受けたバイト・誤り）。

応答:

```ts
LogState
```

## log.read

種類: `read`。解読したログの行（since の番号より後を limit 件）: 本文・重要度・書式・引数・ターゲットの時刻・ソースの場所・本文の中の 0x の数のうち実行区画に入る値の場所（`log_annotate` と同じ）・受けた時刻 t（タイムベース。対象の出来事の上限）と time_source（device_received = デバイスが SWO・UART のバイトを受けた時刻、不確かさは分からない / host_received = PC が RTT を読んだ時刻、不確かさは PC の時計の写しの分だけ。記録の stream log の本体にも）。level 以上の重要度だけ（重要度の無い行は常に）、contains を含むものだけ。next を次の since に。最近の 10000 行だけ持つ（全部は記録の stream "log"）。

| 引数 | 型 | 必須 |
|---|---|---|
| `since` | `number` |  |
| `limit` | `number` |  |
| `level` | `"trace" | "debug" | "info" | "warn" | "error" | null` |  |
| `contains` | `string | null` |  |

応答:

```ts
{ records: Array<{ seq: number; format: string; kind: string; text: string; level?: string; id?: number; template?: string; timestamp?: number; timestamp_text?: string; location?: { file?: string | null; line?: number; module?: string; symbol?: string }; channel?: string; error?: string; args?: Array<Json>; addresses?: Array<LogAddress>; raw?: string; notes?: Array<string>; cut_at?: number; continued?: true; t?: number; time_source?: "device_received" | "host_received" }>; next: number; more: boolean; dropped: number; running: boolean }
```

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
| `channel_levels` | `{ [key: string]: "trace" | "debug" | "info" | "warn" | "error" | null } | null` |  |

応答:

```ts
{ format: string; bytes: number; count: number; errors: number; records: Array<LogRecord>; truncated: boolean }
```

## log.annotate

種類: `read`。文字（UART のパニックの出力・Backtrace の行等）の中の 0x の 16 進の数（桁数によらない）のうち、値が ELF の実行できる区画に入るものを、その番地の関数・ファイル:行にする（機器に触らない。elf か読み込み済みの ELF）。番地として書かれた数かどうかは決めない。addresses の各項目に value（書かれた値）と address（引いた番地。ARM の ELF では Thumb の bit 0 を落とす）。

| 引数 | 型 | 必須 |
|---|---|---|
| `text` | `string` | ○ |
| `elf` | `string | null` |  |

応答:

```ts
{ text: string; addresses: Array<LogAddress> }
```

## trace.start

種類: `control`。トレースの流れの取り込みを始める（すぐ戻る）。format: systemview（RTT の "SysView"。send_start なら開始のコマンドを下りに書く。description = SYSVIEW_<OS>.txt で OS の API の名前と引数）/ ctf（Zephyr。metadata = ビルドの TSDL、無ければ組み込みの表と ctf_header。見出しの形は版で違うので既定なし: metadata に event_header が無ければ ctf_header が要る。時刻の速さは TSDL の clock の freq か ts_hz で明示する（既定なし。どちらも無ければ時刻をタイムベースに写さない））/ tracealyzer（RTT の "TzData"。開始は "TzCtrl"）/ eventrecorder（source memory: Event Recorder の RAM を period_s ごとに止めずに読む。EventRecorderInfo か address。書式は `scvd_load`）。source: rtt（channel を省けば形式の名前 "SysView"・"TzData" のチャンネル。無ければ誤りでほかのチャンネルで代えない。ctf は SEGGER RTT の端末のチャンネル 0）/ swo（ITM の port）/ uart / memory。記録中なら出来事を stream "trace" に。`trace_stop` で時間軸にも。

| 引数 | 型 | 必須 |
|---|---|---|
| `format` | `"systemview" | "ctf" | "tracealyzer" | "eventrecorder"` | ○ |
| `source` | `"rtt" | "swo" | "uart" | "memory"` |  |
| `channel` | `number | string | null` |  |
| `port` | `number` |  |
| `description` | `string | null` |  |
| `metadata` | `string | null` |  |
| `ctf_header` | `"ts32_id8" | "ts32_id16" | "ts64_id16" | "id16" | null` |  |
| `send_start` | `boolean` |  |
| `period_s` | `number` |  |
| `address` | `number | null` |  |
| `swo_baud` | `number | null` |  |
| `core_clock_hz` | `number | null` |  |
| `ts_hz` | `number | null` |  |
| `string_limit` | `number` |  |

応答:

```ts
TraceState
```

## trace.stop

種類: `control`。トレースの取り込みを止め、要約（出来事の種類ごとの数・タスク・失った数・時刻の速さ・時刻の対応・ts_wraps = 32 ビット等の時刻の桁あふれを「前の出来事から 1 周未満」の前提で戻した回数）を返す。timeline ならタスクの出入りを時間軸にし、`rtos_timeline_get`・`rtos_timeline_load` 等の解析がそのまま使える（記録中なら stream "rtos" にも）。idle_tasks: アイドルとして扱うタスクの名前（完全一致。fprobe は名前でアイドルを決めない。結果の timeline.idle_tasks に合った名前 → タスクの ID と合わなかった名前。無ければアイドルは不明）。

| 引数 | 型 | 必須 |
|---|---|---|
| `timeline` | `boolean` |  |
| `idle_tasks` | `Array<string> | null` |  |

応答:

```ts
{ running: boolean; source: "rtt" | "swo" | "uart" | "memory" | "file"; settings: { channel?: number | string; port?: number; send_start?: boolean; description?: string; metadata?: string; ctf_header?: string; ts_hz?: number; swo_baud?: number; period_s?: number; address?: number; string_limit?: number; snapshot?: boolean; path?: string }; bytes: number; format: string; events: number; kinds: { [key: string]: number }; tasks: Array<{ id: number; name?: string; priority?: number; stack_base?: number; stack_size?: number }>; lost: number; errors: number; notes: Array<string>; ts_wraps: number; timed: boolean; segments: number; breaks: Array<{ segment: number; reason: string; init_count?: number | null; previous_ts?: number; ts?: number; mismatch?: number; since_ts?: number | null }>; recorded_events: number; warnings: Array<string>; elapsed_s: number; ts_wrap_min_back?: number | null; assumptions?: Array<string>; ts_first?: number; ts_last?: number; duration_s?: number; ts_hz?: number; cpu_hz?: number; system?: { [key: string]: Json }; clock?: { anchors: Array<{ segment: number; anchor_ts: number; anchor_t: number; uncertainty: number | null }>; anchor_bound: "upper"; assumptions: Array<string>; unknown: Array<string>; timebase_hz: number; ts_hz: number; basis: "anchor_per_segment_declared_rate" }; unplaced_events?: number; error?: string; window?: EventWindowInfo; timeline?: TraceTimeline }
```

## trace.status

種類: `read`。トレースの取り込みの様子（形式・出来事の数・タスク・失った数・誤り）。時刻の対応 clock = {anchors: 時刻の区切り（segment）ごとの対応点 [{segment, anchor_ts, anchor_t, uncertainty（null = 分からない）}]（その区切りの最後の出来事を受けた・読んだ時刻 = 上限）, anchor_bound: upper, assumptions: [declared_rate], unknown: [anchor_lag, rate_error, …], timebase_hz, ts_hz, basis: anchor_per_segment_declared_rate}。対応点の無い区切りの出来事は写さない（unplaced_events、trace_events の t なし、記録に書かない）。時刻の区切り: segments（数）・breaks（[{segment, reason（trace_start / init / resync / systime_mismatch（mismatch・since_ts）/ recorder_reinit / ts_went_back）, …}]。区切りをまたぐ ts の差は意味を持たない）。timed = false は時刻の無い形（ts は順の番号）。ts_wraps = 32 ビットの桁あふれを「前の出来事から 1 周未満」の前提で戻した回数、戻したときは ts_wrap_min_back（生の値が戻った量の最小）と assumptions: [ts_wrap_less_than_one_period]。

応答:

```ts
{ running: false } | TraceState
```

## trace.events

種類: `read`。取り込んだトレース（取り込み中・最後の結果・`trace_snapshot`・`trace_import`）の出来事を offset から limit 件（ターゲットの時刻 ts、タイムベースの時刻 t、種類 kind = task_in / task_out / task_ready / task_block / task_create / task_delete / isr_enter / isr_exit / idle / print / marker / object / overflow / info / other、形式での名前、値、本文）。kinds・task で絞る。

| 引数 | 型 | 必須 |
|---|---|---|
| `offset` | `number` |  |
| `limit` | `number` |  |
| `kinds` | `Array<string> | null` |  |
| `task` | `number | null` |  |

応答:

```ts
{ format: string; total: number; offset: number; events: Array<TraceEvent> }
```

## trace.snapshot

種類: `control`。ターゲットの RAM のトレースのバッファを吸い出して解読する（ポストモーテム・スナップショット。止めなくてよいが、止めた方が書きかけが無い。`snapshot_open` 中ならその RAM から）。format: systemview（RTT の "SysView" のリングを消費せずに）/ ctf（Zephyr の ram_tracing。address なら size も）/ tracex（ThreadX の _tx_trace_header_ptr か address。ts_hz = 時刻の速さ（ctf も: TSDL に clock が無ければ ts_hz で明示する。無ければ時刻をタイムベースに写さない。時刻の無い形には付けない）。切り替えの出来事が無いので実行中のスレッドの区間は出さず、各出来事の文脈を args.context に）/ eventrecorder（EventRecorderInfo か address）。最後の出来事を、それを読んだ後のデバイスの時刻に置いて記録・時間軸にする（上限: 出来事は読むまでの分だけ前で、どれだけ前かは分からない。clock は trace_status と同じ形（時刻の区切りごとの対応点 anchors。この読みでは最後の出来事の区切りだけ）。記録の stream trace の本体に time_source: target_clock・time_bound: upper、不確かさの列は入れない）。read = 吸い出しの情報（bytes = 読んだバイト数。ctf は size = 領域の大きさ・max_bytes = 読む上限・truncated = 上限で切ったか）。idle_tasks は `trace_stop` と同じ。

| 引数 | 型 | 必須 |
|---|---|---|
| `format` | `"systemview" | "ctf" | "tracex" | "eventrecorder"` | ○ |
| `address` | `number | null` |  |
| `size` | `number | null` |  |
| `description` | `string | null` |  |
| `metadata` | `string | null` |  |
| `ctf_header` | `"ts32_id8" | "ts32_id16" | "ts64_id16" | "id16" | null` |  |
| `ts_hz` | `number | null` |  |
| `timeline` | `boolean` |  |
| `rtt_address` | `number | null` |  |
| `max_bytes` | `number` |  |
| `idle_tasks` | `Array<string> | null` |  |
| `string_limit` | `number` |  |

応答:

```ts
{ running: boolean; source: "rtt" | "swo" | "uart" | "memory" | "file"; settings: { channel?: number | string; port?: number; send_start?: boolean; description?: string; metadata?: string; ctf_header?: string; ts_hz?: number; swo_baud?: number; period_s?: number; address?: number; string_limit?: number; snapshot?: boolean; path?: string }; bytes: number; format: string; events: number; kinds: { [key: string]: number }; tasks: Array<{ id: number; name?: string; priority?: number; stack_base?: number; stack_size?: number }>; lost: number; errors: number; notes: Array<string>; ts_wraps: number; timed: boolean; segments: number; breaks: Array<{ segment: number; reason: string; init_count?: number | null; previous_ts?: number; ts?: number; mismatch?: number; since_ts?: number | null }>; recorded_events: number; warnings: Array<string>; elapsed_s: number; read: { bytes: number; source: "rtt:SysView" } | { bytes: number; size: number; max_bytes: number; truncated: boolean; address: number; source: string } | { bytes: number; address: number; source: "tracex" } | { bytes: number; address: number; next: number; source: string; records: number }; ts_wrap_min_back?: number | null; assumptions?: Array<string>; ts_first?: number; ts_last?: number; duration_s?: number; ts_hz?: number; cpu_hz?: number; system?: { [key: string]: Json }; clock?: { anchors: Array<{ segment: number; anchor_ts: number; anchor_t: number; uncertainty: number | null }>; anchor_bound: "upper"; assumptions: Array<string>; unknown: Array<string>; timebase_hz: number; ts_hz: number; basis: "anchor_per_segment_declared_rate" }; unplaced_events?: number; error?: string; window?: EventWindowInfo; timeline?: TraceTimeline }
```

## trace.import

種類: `control`。トレースのファイル（SystemView の .SVDat・Zephyr の CTF のストリーム・Tracealyzer の PSF・TraceX のバッファのダンプ）を解読する（機器に触らない）。時刻はターゲットの時刻のまま（記録には書かない）。timeline なら時間軸の解析にも。ram_buffer は CTF の RAM の backend のダンプ（0 埋めで終わる）。idle_tasks は `trace_stop` と同じ。

| 引数 | 型 | 必須 |
|---|---|---|
| `format` | `"systemview" | "ctf" | "tracealyzer" | "tracex"` | ○ |
| `path` | `string` | ○ |
| `description` | `string | null` |  |
| `metadata` | `string | null` |  |
| `ctf_header` | `"ts32_id8" | "ts32_id16" | "ts64_id16" | "id16" | null` |  |
| `ts_hz` | `number | null` |  |
| `ram_buffer` | `boolean` |  |
| `timeline` | `boolean` |  |
| `idle_tasks` | `Array<string> | null` |  |

応答:

```ts
{ running: boolean; source: "rtt" | "swo" | "uart" | "memory" | "file"; settings: { channel?: number | string; port?: number; send_start?: boolean; description?: string; metadata?: string; ctf_header?: string; ts_hz?: number; swo_baud?: number; period_s?: number; address?: number; string_limit?: number; snapshot?: boolean; path?: string }; bytes: number; format: string; events: number; kinds: { [key: string]: number }; tasks: Array<{ id: number; name?: string; priority?: number; stack_base?: number; stack_size?: number }>; lost: number; errors: number; notes: Array<string>; ts_wraps: number; timed: boolean; segments: number; breaks: Array<{ segment: number; reason: string; init_count?: number | null; previous_ts?: number; ts?: number; mismatch?: number; since_ts?: number | null }>; recorded_events: number; warnings: Array<string>; elapsed_s: number; ts_wrap_min_back?: number | null; assumptions?: Array<string>; ts_first?: number; ts_last?: number; duration_s?: number; ts_hz?: number; cpu_hz?: number; system?: { [key: string]: Json }; clock?: { anchors: Array<{ segment: number; anchor_ts: number; anchor_t: number; uncertainty: number | null }>; anchor_bound: "upper"; assumptions: Array<string>; unknown: Array<string>; timebase_hz: number; ts_hz: number; basis: "anchor_per_segment_declared_rate" }; unplaced_events?: number; error?: string; window?: EventWindowInfo; timeline?: TraceTimeline }
```

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

応答:

```ts
{ etm: { version: string; arch_major: number; arch_minor: number; commit_opt1: boolean; cycle_counting: boolean; branch_broadcast: boolean; return_stack: boolean; timestamp_bits: number; vmid_bytes: number; cid_bytes: number }; trace_id: number; registers: { [key: string]: number }; funnel?: { base: number; ctrl: number }; tpiu?: { base: number; CSPSR: number; SPPR: number; FFCR: number; FSCR: number } }
```

## trace.etm.stop

種類: `control`。ETM を止め（TRCSTATR.IDLE を待つ = 出し切る）、TPIU のフォーマッタを出し切る（FFCR.FOnMan）。番地を省くと `trace_etm_start` のもの。

| 引数 | 型 | 必須 |
|---|---|---|
| `etm_base` | `number | null` |  |
| `tpiu_base` | `number | null` |  |

応答:

```ts
{ idle: boolean; flushed: boolean | null }
```

## trace.etm.decode

種類: `control`。命令トレースのバイト列を解読し、ELF（無ければ実機のメモリ。from_elf で選ぶ）と合わせて実行した命令の流れを再構成する。バイト列は data_hex・path（ファイル）・address + length（ターゲットのメモリのリング。oldest = 最も古いバイトの位置）のどれか 1 つ。formatted: TPIU のフォーマッタの枠付き（align = auto / fsync / start でフレームを揃え、trace_id の流れを取り出す。省略時は `trace_etm_start` の ID、それも無ければ流れが 1 つだけならその ID。複数なら量の多さで選ばずに ID ごとのバイト数を添えて断る）か、ETM のバイト列そのものか。結果は要約（命令の数・例外の数・解けなかったアトム・誤り・最初と最後の命令・次に実行する番地 next_address・ID ごとのバイト数）。誤りは先頭 errors_limit 件（errors_total = 総数・errors_truncated = 切ったか・errors_limit = 使った値）。続けて `trace_etm_history`（止まる直前の命令）/ `trace_etm_instructions` / `trace_etm_packets`。protocol は etmv4（ETMv3.5 は未対応）。 etm_config = {commit_opt1, vmid_bytes, cid_bytes}: ETM の構成（TRCIDR0.COMMOPT・TRCIDR2 の VMID / Context ID の大きさ）。省略時は `trace_etm_start` で読んだ ID レジスタの値。どちらも無ければ断る（暗黙の値で解読しない）。使った値と出どころは結果の etm_config。

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
| `errors_limit` | `number` |  |
| `etm_config` | `Record<string, unknown> | null` |  |

応答:

```ts
{ bytes: number; protocol: string; etm_config: { commit_opt1: boolean; vmid_bytes: number; cid_bytes: number; from: "etm_config" | "id_registers" }; code: "elf" | "target"; packets: number; instructions: number; exceptions: number; unresolved_atoms: number; errors: Array<string>; errors_total: number; errors_truncated: boolean; errors_limit: number; first: { address: number; where: string; function?: string; file?: string; line?: number } | null; last: { address: number; where: string; function?: string; file?: string; line?: number } | null; next_address: number | null; deformat?: { frames: number; aligned: "fsync" | "start" | null; skipped: number; leftover: number }; streams?: { [key: string]: number }; trace_id?: number | null }
```

## trace.etm.packets

種類: `read`。解読した ETM のパケット（共通の要素: async / trace_info / address / atoms（E = 成立、N = 不成立、古い順）/ exception / exception_return / timestamp / cycle_count / trace_on / overflow / decode_error 等。トレースの中の位置 offset とバイト raw 付き）を offset から limit 件。kinds で絞る。OpenCSD 等との照合に。

| 引数 | 型 | 必須 |
|---|---|---|
| `offset` | `number` |  |
| `limit` | `number` |  |
| `kinds` | `Array<string> | null` |  |

応答:

```ts
{ total: number; offset: number; packets: Array<EtmElement> }
```

## trace.etm.instructions

種類: `read`。再構成した命令の一覧（実行順の通し番号 index・番地・命令・関数・ファイル:行。分岐の命令は branch = taken / not_taken / exception と行き先 target）を offset から limit 件と、その範囲の出来事 markers（例外の入口・戻り・時刻・途切れ。index = その前に実行した命令の数）。function で関数に絞る（通った回数・順序を見る）。

| 引数 | 型 | 必須 |
|---|---|---|
| `offset` | `number` |  |
| `limit` | `number` |  |
| `function` | `string | null` |  |

応答:

```ts
{ total: number; offset: number; instructions: Array<EtmInstruction>; markers: Array<EtmMarker> }
```

## trace.etm.history

種類: `read`。事後の命令履歴: 解読したトレースの最後（止まった所・故障の直前）の count 命令（古い順）と、その間の例外・途切れ、次に実行する番地 next_address。実行した命令は確実だが、M-profile の ETM にデータの値は無いので、レジスタ・メモリの過去の値は含まない（note）。解読の誤りは先頭 errors_limit 件（errors_total = 総数・errors_truncated = 切ったか・errors_limit = 使った値）。

| 引数 | 型 | 必須 |
|---|---|---|
| `count` | `number` |  |
| `errors_limit` | `number` |  |

応答:

```ts
{ total: number; instructions: Array<EtmInstruction>; markers: Array<EtmMarker>; next_address: number | null; errors: Array<string>; errors_total: number; errors_truncated: boolean; errors_limit: number; note: string }
```

## scvd.load

種類: `control`。SCVD（Component Viewer Description: RTX5.scvd・EventRecorder.scvd・ミドルウェアのもの）を読む。`scvd_view` の表示と、Event Recorder の出来事の名前・書式・状態に使う（前に読んだものに足す）。

| 引数 | 型 | 必須 |
|---|---|---|
| `paths` | `Array<string>` | ○ |

応答:

```ts
{ files: Array<string>; components: Array<string | null>; typedefs: number; events: number; objects: Array<string | null> }
```

## scvd.view

種類: `control`。SCVD の objects（Component Viewer）を評価して、ターゲットのメモリから表示の木（property・value・children）を作る（RTOS のオブジェクト・ミドルウェアの状態等。止めずに読める。一貫させたいなら止めて）。読めない・式の誤りは notes。readlist・list を回す数の上限 list_limit（結果にも使った値）に達して止めた・リストが輪になって止めたことも notes に事実として出す。%N・%t で番地から読む文字列が上限 string_limit バイト（結果にも使った値）の中で終わらなければ切って、値の中に「<上限 N バイトで切った>」の印・notes に番地と上限を出す。

| 引数 | 型 | 必須 |
|---|---|---|
| `list_limit` | `number` |  |
| `string_limit` | `number` |  |

応答:

```ts
{ files: Array<string>; objects: Array<ScvdNode>; list_limit: number; string_limit: number }
```

## debug.elf.size

種類: `read`。ELF のメモリ使用量（Build Analyzer 相当）: 領域ごとの使用量・空き・使用率と内訳（区画ごと。.data の初期値のように読み込み番地が違う区画は Flash にも数える = as "load"）、読み込む区画の一覧（VMA・LMA・大きさ・領域）、どの領域にも入らない区画（outside）、大きいシンボル（section・region・kind = function / object・name（glob）で絞り、大きい順に top 個）。領域は regions（[{name, origin, length}]）> map_file（GNU ld の -Map の Memory Configuration）> linker_script（MEMORY）> 接続中のターゲットのメモリマップ（regions_source）。elf を省くと読み込んだ ELF。機器に触らない。

| 引数 | 型 | 必須 |
|---|---|---|
| `elf` | `string | null` |  |
| `linker_script` | `string | null` |  |
| `map_file` | `string | null` |  |
| `regions` | `Array<{ name: string; origin: number; length: number; attributes?: string }> | null` |  |
| `top` | `number` |  |
| `section` | `string | null` |  |
| `region` | `string | null` |  |
| `kind` | `"function" | "object" | null` |  |
| `name` | `string | null` |  |

応答:

```ts
{ elf: string; regions: Array<{ name: string; origin: number; length: number; attributes: string; used: number; free: number; fraction: number; sections: Array<{ section: string; as: "run" | "load"; address: number; size: number }> }>; sections: Array<{ name: string; address: number; load_address: number; size: number; type: "progbits" | "nobits"; writable: boolean; executable: boolean; region: string | null; load_region: string | null }>; outside: Array<{ section: string; as: "run" | "load"; address: number; size: number }>; symbols: Array<{ name: string; address: number; size: number; kind: "function" | "object"; section: string; region: string | null }>; symbols_total: number; notes: Array<string>; regions_source: "regions" | "map_file" | "linker_script" | "target" | null }
```

## debug.stack.static

種類: `read`。静的な最大のスタック（Static Stack Analyzer）: 関数ごとの枠（frame。コンパイラの .su（-fstack-usage）があればそれ、無ければ逆アセンブルの SP の動き = frame_source）と、呼び先を含む最大（max）・最深の経路（path）・関数の番地（address）・直接の呼び先（calls。呼び出しの木）。再帰（cycles）・関数ポインタの呼び出し・可変長の確保（alloca）・ELF に無い呼び先は bounded=false と理由（reasons。max は分かる分の下限、unknown_from は上限不明の元の関数）。根（roots: reset / exception = vector_table で渡したベクタ表の処理、total は例外の枠を足したもの / uncalled / address_taken = 関数ポインタの呼び先の候補 / user）と MSP の見積もり（msp: スレッド + 例外 1 段 = one_level、全部が入れ子 = all_nested）。su_paths を省くと ELF のディレクトリの下の .su。indirect = {関数: [呼び先の候補]} で関数ポインタを解決、assume_address_taken で番地を取られた関数のどれかとみなす。vector_table = ベクタ表の番地（使う側が渡す。fprobe は g_pfnVectors 等の名前でベクタ表を探さない。実機なら VTOR = 0xE000ED08 の値）、大きさは vector_table_size か ELF のその番地から始まる変数の大きさ。渡さなければ reset（entry で渡せる）・exception の根は無く、msp は null（結果の vector_table に使った表）。exception_frame を省くと FPU の命令があれば 108、無ければ 36 バイト。機器に触らない。 branch_tables = tbb / tbh の表の大きさ（無い表の分かれ先はたどらず、たどれない命令の SP を下げる量の和を unreached_frame として frame に足す）。解析を同じ所の訪ねなおしの上限で打ち切った関数は truncated（max は下限）。

| 引数 | 型 | 必須 |
|---|---|---|
| `su_paths` | `Array<string> | null` |  |
| `indirect` | `{ [key: string]: Array<string> } | null` |  |
| `assume_address_taken` | `boolean` |  |
| `roots` | `Array<string> | null` |  |
| `entry` | `string | null` |  |
| `exception_frame` | `number | null` |  |
| `function` | `string | null` |  |
| `top` | `number` |  |
| `vector_table` | `number | null` |  |
| `vector_table_size` | `number | null` |  |
| `branch_tables` | `Array<{ address: number; entries: number }> | null` |  |

応答:

```ts
{ functions: Array<{ function: string; frame: number; frame_source: string; max: number; bounded: boolean; reasons: Array<string>; unknown_from: Array<string>; path: Array<string>; address: number; calls: Array<string> }>; roots: Array<{ root: string; kind: "reset" | "exception" | "user" | "address_taken" | "uncalled"; max: number; bounded: boolean; path: Array<string>; exceptions?: Array<number>; total?: number | null }>; msp: { thread: string | null; thread_max: number; exception_frame: number; exception_frame_source: "argument" | "fpu_instructions" | "no_fpu_instructions"; fpu: boolean; one_level: number; all_nested: number; bounded: boolean; notes: Array<string> } | null; cycles: Array<Array<string>>; notes: Array<string>; functions_total: number; su_paths: Array<string>; su_files: Array<string>; address_taken: Array<string>; vector_table: { address: number; size: number } | null }
```

## target.stack.paint

種類: `control`。ベアメタルの MSP のスタックを fill（既定 0xCD）で塗る（止めて。リセット直後が最適。走らせた後に `target_stack_usage`）。範囲は bottom・top（番地）か、使う側が名前・番地を渡す ELF の情報（fprobe は _estack 等の名前の慣習でシンボルを選ばない）: 上端は top・top_symbol（リンカのシンボル名。例 _estack）・vector_table（ベクタ表の番地。最初の語 = 初期の MSP を ELF から読む）のどれか 1 つ、下端は bottom・limit_symbol（下端のシンボル名）・size_symbol（上端 − その値）のどれか、無ければ上端の下の最後の RAM の区画の終わり（空き全部）。結果の bounds に top_source・bottom_source。いまの MSP − margin より上は塗らない。RTOS のタスクのスタックは `rtos_tasks`。

| 引数 | 型 | 必須 |
|---|---|---|
| `fill` | `number` |  |
| `bottom` | `number | null` |  |
| `top` | `number | null` |  |
| `margin` | `number` |  |
| `top_symbol` | `string | null` |  |
| `vector_table` | `number | null` |  |
| `limit_symbol` | `string | null` |  |
| `size_symbol` | `string | null` |  |

応答:

```ts
{ bottom: number; end: number; top: number; fill: number; bytes: number; msp: number; margin: number; bounds: StackBounds }
```

## target.stack.usage

種類: `control`。MSP のスタックの使用量の実測（`target_stack_paint` の後）: 底から塗りが残るバイト数（untouched）から使った最大の深さ（used）・割合（fraction）、塗りが 1 バイトも残っていなければ reached_bottom、SP が下端より下なら overflow（事実だけ。warning は threshold を明示したときだけ）、止まっていれば いまの深さ（current = 上端 − MSP）。走行中でも読める（止めない）。範囲・塗りの値は塗ったときのもの（bottom・top・top_symbol・vector_table・limit_symbol・size_symbol・fill で上書き。範囲の引数は `target_stack_paint` と同じ）。`target_stack_paint` で塗った記録が無ければ fill（ファームが塗った値）が要る（慣習の値を仮定しない）。

| 引数 | 型 | 必須 |
|---|---|---|
| `threshold` | `number | null` |  |
| `fill` | `number | null` |  |
| `bottom` | `number | null` |  |
| `top` | `number | null` |  |
| `top_symbol` | `string | null` |  |
| `vector_table` | `number | null` |  |
| `limit_symbol` | `string | null` |  |
| `size_symbol` | `string | null` |  |

応答:

```ts
{ bottom: number; top: number; size: number; fill: number; untouched: number; used: number; fraction: number; sp: number | null; current: number | null; overflow: boolean; reached_bottom: boolean; warning: boolean | null; threshold: number | null; notes: Array<string>; painted: boolean; bounds: StackBounds }
```

## profile.aggregate

種類: `control`。プロファイル（取っている最中ならいままでの分）を時間の区間に限って集計する（IAR の Timeline で選んだ区間の Function Profiler）。t0_s / t1_s は最初のサンプルからの秒、方法 pcsr はタイムベース（記録の時間軸）の t0 / t1 も（itm はローカルのタイムスタンプが要る = profile_start の timestamps）。by: function / line / instruction / exception は `profile_result` と同じ表、file / directory / prefix（関数名の、使う側が明示した区切り separator の最初の出現より前: separator "_" なら HAL_GPIO_Init → HAL、"::" なら ns::Cls::f → ns。区切りの無い名前はそのまま。by prefix には separator が要る）/ groups はグループ（groups = {名前: [関数名の glob か "file:" + ファイルの glob]}、上から順に最初に合ったもの、残りは "(other)"）ごとのサンプル・割合と多く使った関数（グループは top 個・関数は 5 個まで。切る前の数 groups_total・functions_total と使った top・members を返す）。window に区間とサンプル数。

| 引数 | 型 | 必須 |
|---|---|---|
| `by` | `"function" | "line" | "instruction" | "exception" | "file" | "directory" | "prefix" | "groups"` |  |
| `groups` | `{ [key: string]: Array<string> } | null` |  |
| `t0_s` | `number | null` |  |
| `t1_s` | `number | null` |  |
| `t0` | `number | null` |  |
| `t1` | `number | null` |  |
| `top` | `number` |  |
| `function` | `string | null` |  |
| `separator` | `string | null` |  |
| `order` | `"samples" | "line"` |  |

応答:

```ts
{ by: "function" | "line" | "instruction" | "exception"; window: ProfileWindow; total: number; active: number; categories: ProfileCategories; rows: Array<ProfileRow>; rows_total: number; notes: Array<string> } | { by: "file" | "directory" | "prefix" | "groups"; total: number; active: number; categories: ProfileCategories; groups_total: number; top: number; members: number; rows: Array<{ group: string; samples: number; fraction: number; active_fraction: number; functions: Array<{ function: string; samples: number }>; functions_total: number }>; notes: Array<string>; window: ProfileWindow }
```

## profile.timeline

種類: `control`。CPU 使用率とグループの割合の推移（Core Benchmark）: bucket_s 秒ごとの load（眠り = WFI / WFE 以外の割合）・sleep と、全体で多い top 個のグループ（by・groups・separator は `profile_aggregate` と同じ）の割合（残りは "(other)"。グループの総数 groups_total と使った top を返す）。t0_s / t1_s で範囲。サンプルの時刻が要る（pcsr、または itm の timestamps）。

| 引数 | 型 | 必須 |
|---|---|---|
| `bucket_s` | `number` |  |
| `by` | `"function" | "file" | "directory" | "prefix" | "groups"` |  |
| `groups` | `{ [key: string]: Array<string> } | null` |  |
| `top` | `number` |  |
| `t0_s` | `number | null` |  |
| `t1_s` | `number | null` |  |
| `separator` | `string | null` |  |

応答:

```ts
{ bucket: number; top: number; groups_total: number; groups: Array<string>; buckets: Array<{ t0: number; samples: number; load: number; sleep: number; groups: { [key: string]: number }; t0_s: number }>; bucket_s: number; window: ProfileWindow }
```

## profile.export

種類: `control`。プロファイルをほかのツールの形式で書き出す（t0_s / t1_s・t0 / t1 で区間）。gmon = gprof の gmon.out（OpenOCD の profile と同じ番地の度数。low / high で範囲、率は時刻からの実測。16 ビットを超える数は全体を割って率も割る）/ callgrind = KCacheGrind（関数・行ごとの自分のサンプル。呼び出しの関係は PC サンプリングでは分からない）/ lcov = 近似のカバレッジの .info（SF は source_root を前に付ける。回数 0 の行が実行されなかったとは限らない）/ gcov = .gcov のテキスト（path はディレクトリ。source_root の下にソースがあれば行の中身も）/ dot = Graphviz（関数の割合。static_calls なら逆アセンブルの静的な呼び出しを破線で、min_fraction 未満は省く）。lcov / gcov の関数の記録（FN）は DWARF の宣言の場所（DW_AT_decl_file・DW_AT_decl_line）に置く（行の多いファイルを所在とみなさない。宣言の場所の無い関数は FN を書かず、結果の undeclared_functions に）。

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

応答:

```ts
{ format: "gmon"; path: string; window: ProfileWindow; rate_source: "measured" | "plan"; samples: number; outside: number; scale: number; low: number | null; high: number | null; bins: number; arcs: number; bin_bytes?: number; prof_rate?: number } | { format: "callgrind" | "dot"; path: string; window: ProfileWindow; total: number } | { format: "lcov" | "gcov"; path: string; window: ProfileWindow; total: number; files: number; notes: Array<string>; undeclared_functions?: Array<string>; written?: Array<string> }
```

## testpoint.aggregate

種類: `control`。テストポイントの関数の時間を区間（最初の印からの t0_s〜t1_s 秒。`testpoint_calls` と同じ）に限って集計: inclusive_s（子を含む。区間で切る。再帰は外側だけ）・exclusive_s（自分だけ）・started（区間で始まった回数）・active（区間に掛かった回数）。by が function 以外なら file / directory / prefix（separator は `profile_aggregate` と同じ）/ groups のグループごとの exclusive_s と started。持っている最近の呼び出し（65536 件）だけ（dropped・kept_from_s）。 rows は先頭 top 件（rows_total・rows_truncated・top）。

| 引数 | 型 | 必須 |
|---|---|---|
| `t0_s` | `number | null` |  |
| `t1_s` | `number | null` |  |
| `by` | `"function" | "file" | "directory" | "prefix" | "groups"` |  |
| `groups` | `{ [key: string]: Array<string> } | null` |  |
| `top` | `number` |  |
| `separator` | `string | null` |  |

応答:

```ts
{ running: boolean; hz: number; t0_s: number | null; t1_s: number | null; dropped: number; kept_from_s: number | null; by: string; rows: Array<{ function: string; inclusive_s: number; exclusive_s: number; started: number; active: number }> | Array<{ group: string; exclusive_s: number; started: number; functions: Array<string> }>; rows_total: number; rows_truncated: boolean; top: number; notes?: Array<string> }
```

## testpoint.export

種類: `control`。テストポイントの結果をほかのツールの形式で書き出す: callgrind = KCacheGrind（呼び出しの木。calls = 回数と含む時間、自分の時間。単位はタイムベースの刻み）/ lcov = .info（C0 の行 DA・関数 FN / FNDA・C1 の分岐 BRDA。判定まで来なかった組は "-"。SF は対応表の相対の道に source_root を前に付ける）/ gcov = .gcov のテキスト（path はディレクトリ。関数の呼ばれた・戻った回数と塊の割合、枝の回数）/ dot = Graphviz の呼び出しグラフ（含む・自分の割合と回数）/ gmon = gprof の呼び出しの組（arc）だけ（番地は読み込んだ ELF から。無い関数は missing）。

| 引数 | 型 | 必須 |
|---|---|---|
| `format` | `"callgrind" | "lcov" | "gcov" | "dot" | "gmon"` | ○ |
| `path` | `string` | ○ |
| `source_root` | `string | null` |  |
| `test_name` | `string` |  |
| `min_fraction` | `number` |  |

応答:

```ts
{ format: "callgrind" | "dot"; path: string; running: boolean } | { format: "lcov" | "gcov"; path: string; running: boolean; files: number; written?: Array<string> } | { format: "gmon"; path: string; running: boolean; arcs: number; missing: Array<string> }
```

## linux.connect

種類: `control`。Linux のターゲットに SSH（Windows の OpenSSH。利用者の ~/.ssh と ssh-agent、BatchMode）でつなぎ、能力の事実を返す: カーネルの版・機械・カーネルの設定（uprobes・ftrace・kprobes・BPF・BTF・perf・ハードのブレークポイント・devmem。読めなければ null = 不明）・tracefs（trace_clock のある場所）・uprobe_events と kprobe_events の有無・trace_clock・perf_event_paranoid・ptrace_scope・道具（gcc・gdbserver・gdb・perf・bpftrace）・uid・sudo。sudo=true ならターゲットの操作を sudo -n で行う（sudo_password_env を渡すと sudo -S -k で、その環境変数のパスワードを stdin の最初の行で渡す。パスワードの要る sudo のターゲットだけに使い、つなぐときに sudo -v で確かめる）。

| 引数 | 型 | 必須 |
|---|---|---|
| `target` | `string` | ○ |
| `host` | `string` | ○ |
| `user` | `string` | ○ |
| `port` | `number` |  |
| `key` | `string | null` |  |
| `sudo` | `boolean` |  |
| `sudo_password_env` | `string | null` |  |

応答:

```ts
LinuxCapabilities
```

## linux.disconnect

種類: `control`。Linux のターゲットを切る（プローブを外し、エージェントを止める）。

| 引数 | 型 | 必須 |
|---|---|---|
| `target` | `string` | ○ |

応答:

```ts
{ target: string; disconnected: true; remote_cleanup_skipped?: string }
```

## linux.capabilities

種類: `read`。つないだときに採った能力の事実（`linux_connect` の結果と同じ）。

| 引数 | 型 | 必須 |
|---|---|---|
| `target` | `string` | ○ |

応答:

```ts
LinuxCapabilities
```

## linux.agent.deploy

種類: `control`。エージェント（C）の源をターゲットの ~/.fprobe に置き、ターゲットの gcc で作る。使った gcc（--version の 1 行目）と源の sha256 を返す。gcc が無ければ失敗。

| 引数 | 型 | 必須 |
|---|---|---|
| `target` | `string` | ○ |

応答:

```ts
LinuxAgentDeploy
```

## linux.agent.start

種類: `control`。エージェントを SSH の中で起動する（sudo なら sudo -n）。新しいエージェントの通しの番号は 0 から始まるので、前の番号の表とリングの読みを捨てる（リングは開き直す）。 watchdog_s を渡すと、デーモンからの入力がその秒数途絶えたとき、linger のリングがあれば ptrace しているプロセスが終わるまで続けてから、無ければ bp を戻してすぐ、エージェントは終わる。

| 引数 | 型 | 必須 |
|---|---|---|
| `target` | `string` | ○ |
| `watchdog_s` | `number | null` |  |

応答:

```ts
{ hello: { id: number; ok: true; version: number; clock: string; pid: number } | null; already_running: boolean; watchdog_s: number | null }
```

## linux.agent.status

種類: `read`。エージェントの状態: 動いているか・終了コード・stderr の最後・出来事の受け手の誤りの数 event_errors と最後の誤り・捨てた JSON でない行の数 bad_lines・導入の記録・時刻合わせを続ける処理の誤り。

| 引数 | 型 | 必須 |
|---|---|---|
| `target` | `string` | ○ |

応答:

```ts
LinuxAgentStatus
```

## linux.agent.stop

種類: `control`。エージェントを止める（ptrace のブレークポイントは元に戻して外す）。ptrace のプローブは一覧から消え、消した名前を cleared_probes で返す。 リングの読み続け（linux.ring.watch）も止める。

| 引数 | 型 | 必須 |
|---|---|---|
| `target` | `string` | ○ |

応答:

```ts
{ running: boolean; exit_code: number | null; stderr_tail: string; watchdog_s: number | null; event_errors: number; last_event_error: string | null; bad_lines: number; deployed: LinuxAgentDeploy | null; sync_error: string | null; ring: LinuxRing | null; ring_stats: LinuxRingStats; trace_unparsed: number; trace_unrouted: number; cleared_probes: Array<string> }
```

## linux.probe.add

種類: `control`。アプリの関数（symbol）か番地（address。32 ビットの ARM は isa = arm / thumb を明示）にプローブを付ける。バイナリはターゲットから取り寄せて手元に置く（sysroot_file・sha256）。method=uprobe: tracefs の uprobe_events に書く（作った行 line を返す）。引数は fetchargs（例 "a=%r0:s32"）を明示する。32 ビットの ARM では ARM 命令の関数だけ（Thumb は断る）。カーネルに uprobe_events が無ければ失敗。method=ptrace: pid のプロセスの全スレッドに付ける。当たるたびに全スレッドを止め、regs のレジスタを読み、1 歩進めて置き直す（振る舞いが変わる。時刻はエージェントが止まりを知った時刻）。fork の子は元の命令に戻して外す（vfork の子は親とメモリを共有するので書き換えない）。最初の命令が PC を書く・Thumb の IT なら断る。32 ビットの ARM だけ。

| 引数 | 型 | 必須 |
|---|---|---|
| `target` | `string` | ○ |
| `name` | `string` | ○ |
| `binary` | `string` | ○ |
| `method` | `"uprobe" | "ptrace"` | ○ |
| `symbol` | `string | null` |  |
| `address` | `number | null` |  |
| `isa` | `"arm" | "thumb" | null` |  |
| `fetchargs` | `Array<string> | null` |  |
| `regs` | `Array<"r0" | "r1" | "r2" | "r3" | "r4" | "r5" | "r6" | "r7" | "r8" | "r9" | "r10" | "r11" | "r12" | "sp" | "lr" | "pc" | "cpsr"> | null` |  |
| `ret` | `boolean` |  |
| `pid` | `number | null` |  |

応答:

```ts
LinuxProbe
```

## linux.probe.list

種類: `read`。付けたプローブ（場所・作った行または計画・取り寄せたバイナリ）。ptrace していたプロセスが終わったものは ended（終了コード・シグナル）が付く（エージェントからは消えている。linux.probe.remove で一覧から外す）。

| 引数 | 型 | 必須 |
|---|---|---|
| `target` | `string` | ○ |

応答:

```ts
{ probes: Array<LinuxProbe> }
```

## linux.probe.remove

種類: `control`。プローブを外す。終わったプロセスのものは一覧から外すだけ（ended を返す）。エージェントが先に終わっていたら agent_running=false を返す（ターゲットのアプリにブレークポイントの命令が残っている恐れ）。

| 引数 | 型 | 必須 |
|---|---|---|
| `target` | `string` | ○ |
| `name` | `string` | ○ |

応答:

```ts
{ removed: string; ended?: { code: number | null; signal: number | null } | { agent: string }; agent_running?: false }
```

## linux.sync

種類: `control`。ネットワークの時刻合わせ: エージェントの時計（CLOCK_MONOTONIC_RAW）を count 回問い、（ターゲットの時刻、PC の往復の中点、往復の半分）の組を足す。出来事の換算は隣り合う組の内挿。

| 引数 | 型 | 必須 |
|---|---|---|
| `target` | `string` | ○ |
| `count` | `number` | ○ |

応答:

```ts
{ pairs: Array<{ target_ns: number; host_s: number; half_rtt_s: number }>; half_rtt_s: { min: number; max: number } | null }
```

## linux.trace.start

種類: `control`。出来事の受け取りを始める: uprobe か ftrace のイベントがあれば trace_clock を mono_raw にして（無ければ今の時計のまま、その名前を返す）trace_pipe を読む。sync_interval_s ごとに 1 回時刻合わせをする。

| 引数 | 型 | 必須 |
|---|---|---|
| `target` | `string` | ○ |
| `sync_interval_s` | `number` | ○ |

応答:

```ts
{ trace_clock: string | null; trace_pipe: boolean }
```

## linux.trace.stop

種類: `control`。出来事の受け取りと時刻合わせを止める（プローブは残す）。最後に 1 回時刻合わせをしてから、残りの出来事を記録に入れる（それでも組の外のものは outside=true で入る）。

| 引数 | 型 | 必須 |
|---|---|---|
| `target` | `string` | ○ |

応答:

```ts
{ stopped: true; pairs: number }
```

## linux.events.read

種類: `read`。受けた出来事（since の番号より後を limit 件）: 方式・プローブ・pid / tid・ターゲットの時刻 target_ns と時計の名前・引数 / レジスタ。読む時点の時刻合わせの組で換算した PC の時刻 host_s と sync（往復の半分 half_rtt_ns・最も近い組までの距離・組の範囲の外 outside）を付ける（組が 2 つ未満なら付けない）。最近の 100000 件だけ持つ（あふれた数 dropped）。記録中に受けた出来事だけを、その記録に、後ろに組ができたものから内挿で換算して stream "<target>.ptrace" / "<target>.uprobe" に入れる（sync に記録の時計の不確かさ clockmap_uncertainty_ticks を別に足す。記録を止める前に組を 1 つ置いて残りを入れる）。記録中に受けたのに入れられなかった数（組が 2 つ未満・記録の時計の点が無い・記録が替わった）は not_recorded。

| 引数 | 型 | 必須 |
|---|---|---|
| `target` | `string` | ○ |
| `since` | `number` |  |
| `limit` | `number` |  |

応答:

```ts
{ records: Array<LinuxEvent>; next: number; dropped: number; not_recorded: number }
```

## linux.ring.open

種類: `control`。道 B: エージェントに、物理番地 phys から size バイトをリングとして開かせる（/dev/mem を mmap。cached=false なら O_SYNC。エージェントは sudo で起動しておく）。番地はカーネルが予約した所を明示する（Pi 2B は device tree の reserved-memory、PYNQ は mem=）。以後 ptrace の当たりは、道 A と同じ通しの番号 seq でリングにも書かれる。 道 A（SSH）への書き込みは待たない: 詰まって書けない行は捨てて数え、次に書けたときに出来事 a_dropped で知らせる（道 A が詰まっても ptrace と道 B は止まらない）。

| 引数 | 型 | 必須 |
|---|---|---|
| `target` | `string` | ○ |
| `phys` | `number` | ○ |
| `size` | `number` | ○ |
| `cached` | `boolean` | ○ |
| `linger` | `boolean` | ○ |

応答:

```ts
LinuxRing
```

## linux.ring.read

種類: `control`。道 B: リングの新しい記録を JTAG で読む（古い方から max_records 個まで）。access=halt: target.connect でつないだ Cortex-A のコアを止め、そのコアに読ませ、すぐ再開する（止めるので振る舞いが変わる。止めていた時間 halted_ms・読んだ語数 words・コア core を返す）。番号の合わない記録 torn・CRC の合わない記録 corrupt・追い越されて読めなかった範囲 lost を事実で返す。頭（CRC 付き）が読めなければ header_error に事実を入れて何も読まない（次の読みで続く）。records の agent_seq はエージェントの通しの番号（linux.events.read の seq とは別）。読んだ出来事は linux.events.read に path b で入り、道 A と同じ seq なら 1 つにする（paths に a・b）。access=halt は cached=false のリングを、access=memap は cached=true のリングを断る（キャッシュの見え方が合わず古い値を読みうる）。止める要求の後に失敗しても、必ず再開を試みる。access=memap は linux.memap.connect の口で止めずに読む（halted_ms は 0、read_ms は読みの時間）。

| 引数 | 型 | 必須 |
|---|---|---|
| `target` | `string` | ○ |
| `access` | `"halt" | "memap"` | ○ |
| `max_records` | `number` | ○ |
| `address` | `number` | ○ |

応答:

```ts
{ records: Array<{ agent_seq: number; probe: string; duplicate: boolean }>; lost: Array<[number, number]>; torn: Array<number>; corrupt: Array<number>; head: number | null; left: number; header_error: string | null; halted_ms: number; read_ms: number; words: number; core: number | null; access: "halt" | "memap"; kept_halted: boolean | null }
```

## linux.ring.watch

種類: `control`。道 B: interval_s ごとに linux.ring.read を続ける。止めた回数・時間の合計と最大・欠けの数・止まった理由は linux.agent.status の ring_stats。

| 引数 | 型 | 必須 |
|---|---|---|
| `target` | `string` | ○ |
| `interval_s` | `number` | ○ |
| `access` | `"halt" | "memap"` | ○ |
| `max_records` | `number` | ○ |
| `address` | `number` | ○ |

応答:

```ts
{ watching: true }
```

## linux.ring.unwatch

種類: `control`。道 B: linux.ring.watch を止める（ring_stats を返す）。

| 引数 | 型 | 必須 |
|---|---|---|
| `target` | `string` | ○ |

応答:

```ts
{ watching: false; ring_stats: LinuxRingStats }
```

## linux.gdb.batch

種類: `control`。止めて調べる（AI 向け）（対象の上の gdb を ssh で使う。JTAG・全コア停止は使わない: gdb の attach は ptrace でそのプロセスのスレッドを 1 つずつ止め（同じ時点ではない）、他のプロセスとコアは走り続ける。全コアを JTAG で止めて同じ時点で調べるなら linux.halt.debug.*）: ターゲットの gdb を -batch -nx で走らせ、commands（例 ["bt", "info registers"]）を順に実行した出力をそのまま返す（exit_code・stdout・stderr・走らせた command）。pid につける間はプロセスが止まる。core なら binary と core のファイルを読む。命令は 1 つずつ -ex の引数にし、シェルの単語として引用する。ターゲットの timeout -s TERM timeout_s で gdb を終わらせ（つけたプロセスから外れる）、時間切れなら timed_out=true と途中までの出力を返す。

| 引数 | 型 | 必須 |
|---|---|---|
| `target` | `string` | ○ |
| `commands` | `Array<string>` | ○ |
| `timeout_s` | `number` | ○ |
| `pid` | `number | null` |  |
| `core` | `string | null` |  |
| `binary` | `string | null` |  |

応答:

```ts
{ exit_code: number | null; timed_out: boolean; stdout: string; stderr: string; command: string }
```

## linux.gdb.serve

種類: `control`。止めて調べる（人・手元の gdb 向け）: ターゲットの gdbserver（標準入出力、--once --attach）を pid につけ、PC の 127.0.0.1:port でgdb の接続を 1 つ待って中継する（gdb で target remote 127.0.0.1:port。gdbserver はつないだときに起動）。

| 引数 | 型 | 必須 |
|---|---|---|
| `target` | `string` | ○ |
| `pid` | `number` | ○ |
| `port` | `number` | ○ |

応答:

```ts
{ port: number; command: string; connect: string }
```

## linux.gdb.status

種類: `read`。gdb の中継の状態: つながっているか・gdbserver が動いているか・終了コード・送受したバイト数・閉じた側・stderr の最後。

| 引数 | 型 | 必須 |
|---|---|---|
| `target` | `string` | ○ |

応答:

```ts
LinuxGdbRelayStatus
```

## linux.gdb.stop

種類: `control`。gdb の中継を止める（gdbserver も終わり、プロセスは gdbserver が外れて続く）。

| 引数 | 型 | 必須 |
|---|---|---|
| `target` | `string` | ○ |

応答:

```ts
LinuxGdbRelayStatus
```

## linux.ftrace.enable

種類: `control`。カーネルの出来事: tracefs のイベント（例 "sched/sched_switch"・"irq/irq_handler_entry"）を有効にする。出来事は linux.trace.start で開く trace_pipe から受け、method "ftrace"（comm・pid・cpu・行の k=v のままの args・trace_clock の時刻、行の文字のままの text）で linux.events.read と記録の stream "<target>.ftrace" に入る。 syscalls の sys_enter_X・sys_exit_X は行に sys_X と出る。カーネルのバッファがあふれた「LOST n EVENTS」は probe "trace_lost" の出来事として残し、形の分からない行・有効にしていないイベントの行の数は linux.agent.status の trace_unparsed・trace_unrouted。切断すると有効にしたイベントを無効にする。

| 引数 | 型 | 必須 |
|---|---|---|
| `target` | `string` | ○ |
| `events` | `Array<string>` | ○ |

応答:

```ts
{ enabled: Array<string>; tracefs: string }
```

## linux.ftrace.disable

種類: `control`。tracefs のイベントを無効にする。

| 引数 | 型 | 必須 |
|---|---|---|
| `target` | `string` | ○ |
| `events` | `Array<string>` | ○ |

応答:

```ts
{ enabled: Array<string> }
```

## linux.log.read

種類: `read`。journal を読む（journalctl -o json。since_cursor が無ければ新しい方から lines 行、あれば since_cursor の後の古い方から lines 行。unit で絞る）。各行は JSON のまま（MESSAGE・__CURSOR・__MONOTONIC_TIMESTAMP 等）。次の since_cursor は next_cursor。時刻は起動ごと（_BOOT_ID）の CLOCK_MONOTONIC（µs。前の起動の行は前の起動の時計）で、出来事の CLOCK_MONOTONIC_RAW とは別の時計（換算しない）。JSON でない行の数は bad_lines。

| 引数 | 型 | 必須 |
|---|---|---|
| `target` | `string` | ○ |
| `lines` | `number` | ○ |
| `unit` | `string | null` |  |
| `since_cursor` | `string | null` |  |

応答:

```ts
{ entries: Array<{ [key: string]: Json }>; next_cursor: string | null; clock: "CLOCK_MONOTONIC"; exit_code: number; stderr: string; bad_lines: number; command: string }
```

## linux.memap.connect

種類: `control`。道 B の止めずに読む口を開く: Vivado の xsdb（hw_server）で、ボードの USB の JTAG から DAP の AHB-AP で物理メモリを読む（CPU は止めない。Zynq-7000 の PYNQ-Z2 で使う）。xsdb の場所・JTAG のケーブルのシリアル・チェーンにあるはずの装置の IDCODE（例 Zynq-7020 の PL 0x23727093）を明示する（違うボードを読まない）。

| 引数 | 型 | 必須 |
|---|---|---|
| `target` | `string` | ○ |
| `xsdb` | `string` | ○ |
| `cable_serial` | `string` | ○ |
| `chain_idcode` | `number` | ○ |
| `timeout_s` | `number` | ○ |

応答:

```ts
{ tool: "xsdb"; cable_serial: string; chain_idcode: number; access: string; timeout_s: number }
```

## linux.memap.connect_jtag

種類: `control`。道 B の止めずに読む口を、fprobe の JTAG の口につないだ DAP の MEM-AP で開く（xsdb 不要。Zynq-7000 の PYNQ-Z2 を PJTAG で Primer の J12 につないだとき）。チェーンにあるはずの DP の IDCODE（Zynq-7000 は 0x4BA00477）・MEM-AP の番号（Zynq の AHB-AP は 0）・TCK の周波数を明示する（違うターゲットを読まない）。開いている間、プローブの生の DAP をこの口が使う（jtag.scan 等とは同時に使えない。linux.memap.disconnect で解放）。CPU は止めない。

| 引数 | 型 | 必須 |
|---|---|---|
| `target` | `string` | ○ |
| `chain_idcode` | `number` | ○ |
| `ap` | `number` | ○ |
| `frequency_hz` | `number` | ○ |

応答:

```ts
{ via: "probe_jtag"; chain_idcode: number; device_index: number; ap: number; ap_idr: number; frequency_hz: number }
```

## linux.memap.disconnect

種類: `control`。止めずに読む口を閉じる（xsdb を終える）。

| 引数 | 型 | 必須 |
|---|---|---|
| `target` | `string` | ○ |

応答:

```ts
{ memap: null }
```

## sampler.trigger_set

種類: `control`。DAP サンプラが読んだ address の語が (値 xor value) and mask = 0 に入った瞬間（前の標本が不一致・今の標本が一致）にハードのクロストリガ（源 sampler_match）: halt = 割り込み書き込み（既定は MCU の停止）、la = ロジアナのトリガ。例: MCU が止まった = address 0xE000EDF0（DHCSR）・mask = value = 0x20000（S_HALT）。サンプラに address を読ませるのは別の道具（watch 等。この API はサンプラの持ち主にならない）。読んでいなければ sampler.trigger_status の seen が 0 のまま。有効にした後の最初の標本では発火しない。ケイパビリティ sampler_match が要る。

| 引数 | 型 | 必須 |
|---|---|---|
| `address` | `number` | ○ |
| `mask` | `number` | ○ |
| `value` | `number` | ○ |
| `halt` | `boolean` | ○ |
| `la` | `boolean` | ○ |

応答:

```ts
{ address: number; mask: number; value: number; halt: boolean; la: boolean }
```

## sampler.trigger_status

種類: `read`。サンプラの値の一致のトリガのレジスタの事実: enabled・address・mask・value・halt・la・matched（最後の標本が一致）・seen_any・seen（有効にした後に比べた標本の数）・count（発火の回数）・time（最後に発火した標本を受けた時刻。タイムベースの下位 48 ビット = イベントの時刻と同じ幅）・prev_time（その直前の不一致の標本の時刻。条件に入ったのは prev_time と time の間のどこか）。

応答:

```ts
{ enabled: boolean; address: number; mask: number; value: number; halt: boolean; la: boolean; matched: boolean; seen_any: boolean; seen: number; count: number; time: number; prev_time: number }
```

## sampler.trigger_clear

種類: `control`。サンプラの値の一致のトリガをやめる（比べるのを止め、経路の sampler_match を外す。ほかの源の経路は触らない）。

応答:

```ts
{ enabled: false }
```

## jtag.sample_delay_set

種類: `control`。JTAG の TDO の取り込みを、次の TCK の立ち下がりの直前から cycles 周期（sys、0〜7）遅らせる。TDO が立ち下がりから（1 ビットの長さ − 1）周期（sys。今の回路は 2 × half_period − 1）より遅れて届くとき（Pi 2B の 14.3 MHz 以上で見た。1 ビットずれて読める）に使う。どの値がよいかは jtag.sample_delay_scan の事実で決める。readback = 読み返した値（古い回路は欄が無く 0）。

| 引数 | 型 | 必須 |
|---|---|---|
| `cycles` | `number` | ○ |

応答:

```ts
{ cycles: number; readback: number }
```

## jtag.sample_delay_scan

種類: `control`。TCK frequency_hz で、取り込みの遅れ 0〜7 のそれぞれについて、チェーンの IDCODE を reads 回読み、expected_idcodes（TDO 側から。呼び手が明示）と一致した回数 matches と、見えた値 observed（値の組と回数）を返す。applied = 遅れの欄を書いて読み返せた。どれを使うかは決めない（終わったら元の遅れに戻す。使うには jtag.sample_delay_set）。未接続のときだけ。 実際の TCK: half_period（SWJ_Clock と同じ整数の計算）と tck_hz = sys / (2 × half_period + extra)。extra はデバイスが申告する 1 ビットの余分な周期（今の回路は 0、ケイパビリティに申告の無い古い回路は 1）。

| 引数 | 型 | 必須 |
|---|---|---|
| `frequency_hz` | `number` | ○ |
| `expected_idcodes` | `Array<number>` | ○ |
| `reads` | `number` | ○ |

応答:

```ts
{ frequency_hz: number; half_period: number; tck_hz: number; expected_idcodes: Array<number>; delays: Array<{ delay: number; applied: boolean; reads: number; matches: number; observed: Array<{ idcodes: Array<number>; count: number }> }>; restored_delay: number }
```

## linux.core.status

種類: `read`。core dump の設定の事実: pattern（/proc/sys/kernel/core_pattern。pipe = "|" で始まる = 別のプログラムに渡す）・uses_pid・suid_dumpable、pid を渡せばそのプロセスの上限（limit_soft・limit_hard、バイト。null = unlimited）・cwd・exe（pattern が相対なら core は cwd に置かれる）。

| 引数 | 型 | 必須 |
|---|---|---|
| `target` | `string` | ○ |
| `pid` | `number | null` |  |

応答:

```ts
LinuxCoreStatus
```

## linux.core.pattern_set

種類: `control`。/proc/sys/kernel/core_pattern を pattern にする（システム全体の設定。sudo が要る。戻すには previous を渡し直す）。例 "/tmp/core.%e.%p"。previous = 前の値、pattern = 書いた後に読み返した値。

| 引数 | 型 | 必須 |
|---|---|---|
| `target` | `string` | ○ |
| `pattern` | `string` | ○ |

応答:

```ts
{ previous: string; pattern: string }
```

## linux.core.limit_set

種類: `control`。プロセス pid の core の大きさの上限（RLIMIT_CORE）を変える（prlimit。soft・hard はバイトか "unlimited"）。そのプロセスの後の落ち方で core が書かれる。戻り値は変えた後の linux.core.status。

| 引数 | 型 | 必須 |
|---|---|---|
| `target` | `string` | ○ |
| `pid` | `number` | ○ |
| `soft` | `number | "unlimited"` | ○ |
| `hard` | `number | "unlimited"` | ○ |

応答:

```ts
LinuxCoreStatus
```

## linux.core.fetch

種類: `control`。ターゲットのファイル path（core 等）を PC の local_path に写す（sudo なら sudo で読む）。bytes・sha256 を返す。中身の解析はターゲットの gdb で（linux.gdb.batch の core・binary）。

| 引数 | 型 | 必須 |
|---|---|---|
| `target` | `string` | ○ |
| `path` | `string` | ○ |
| `local_path` | `string` | ○ |

応答:

```ts
{ path: string; local_path: string; bytes: number; sha256: string }
```

## linux.perf.stat

種類: `control`。perf stat で events（例 "cycles"・"instructions"・"task-clock"）を duration_s 秒数える（pid か、無ければ全体）。rows = 行ごとの値（--no-scale: 数えた生の値。引き延ばさない）・単位・走った時間と割合（数えられなければ value = null と counted）。perf_metric は perf が計算した比。ターゲットに perf が要る（tools.perf）。

| 引数 | 型 | 必須 |
|---|---|---|
| `target` | `string` | ○ |
| `events` | `Array<string>` | ○ |
| `duration_s` | `number` | ○ |
| `pid` | `number | null` |  |

応答:

```ts
{ rows: Array<{ event: string; value: number | null; unit: string; run_time: number | null; run_pct: number | null; counted?: string; perf_metric?: { value: number | null; unit: string } }>; exit_code: number; stderr: string; command: string }
```

## linux.perf.record

種類: `control`。perf record で duration_s 秒、frequency_hz で標本を取り（pid か、無ければ全体。時計は CLOCK_MONOTONIC_RAW、CPU の番号つき）、標本を出来事（method "perf"、probe = 関数の名前、ip・dso・pid・tid・cpu・period・event。時刻は µs の精度 = resolution_ns 1000）にして、ほかの出来事と同じ時間軸に入れる。PC に持ってくるのは perf script の頭から max_samples 行まで（script_lines = 全行数）。record_exit_code・script_exit_code・samples・perf_reported_samples・unparsed と unparsed_examples・evicted（押し出したほかの出来事の数）。

| 引数 | 型 | 必須 |
|---|---|---|
| `target` | `string` | ○ |
| `duration_s` | `number` | ○ |
| `frequency_hz` | `number` | ○ |
| `pid` | `number | null` |  |
| `max_samples` | `number` | ○ |

応答:

```ts
{ samples: number; unparsed: number; unparsed_examples: Array<string>; perf_reported_samples: number | null; record_exit_code: number | null; script_exit_code: number | null; script_lines: number | null; evicted: number; exit_code: number; stderr: string; command: string }
```

## linux.halt.open

種類: `control`。JTAG で全コアを止めて Linux のアプリをデバッグする口を開く（CLI python -m fprobe.linux_halt の共通の引数と一対一。既定の値は無い）。backend=fprobe なら、開いている間プローブの生の DAP を占有する（target.connect・jtag.scan 等の線を直接動かす操作・linux.memap.connect_jtag・構成の書き替えは断る。応答の occupies）。開くと全コアの停止デバッグ（HDBGen）を立てる（止めはしない）。応答: 開いた事実（コアの数・HW の bp/wp の数・debug_ap・discovery・CTI・見張り・FPGA の時刻の有無）、渡した設定、ジャーナルの事実（死んだデバッガが残したかもしれない BKPT の物理番地。戻すのは linux.halt.recover）。

| 引数 | 型 | 必須 |
|---|---|---|
| `backend` | `"fprobe" | "openocd"` | ○ |
| `jtag_khz` | `number` | ○ |
| `offsets` | `string` | ○ |
| `page_offset` | `number` | ○ |
| `phys_offset` | `number` | ○ |
| `linear_map_end` | `number` | ○ |
| `max_tasks` | `number` | ○ |
| `max_threads` | `number` | ○ |
| `mmu_format` | `"short" | "lpae"` | ○ |
| `journal` | `string` | ○ |
| `fpga_request_times` | `boolean` | ○ |
| `part` | `string` |  |
| `fpga_halt_watch_bound_ns` | `number | "unmeasured"` |  |
| `fpga_halt_watch_poll_s` | `number` |  |
| `cti_board` | `"zynq7000"` |  |
| `cti_request_bound_ns` | `number` |  |
| `cti_event_bound_ns` | `number | "unmeasured"` |  |
| `cti_hw_wait_s` | `number` |  |
| `cti_ack_polls` | `number` |  |
| `ocd_board` | `"rpi2b" | "zynq7000"` |  |
| `ocd_port` | `number` |  |
| `ocd_timeout_s` | `number` |  |
| `hw_slots` | `number` |  |
| `watch_slots` | `number` |  |

応答:

```ts
{ backend: "openocd"; n_cores: number; hw_slots: number; watch_slots: number; actual_jtag_khz: number; options: HaltOptions; journal: HaltJournal; occupies: { probe_dap: boolean; refused_while_open: Array<"target.connect" | "lowlevel" | "linux.memap.connect_jtag" | "probe.design.switch">; device_lock: "per_call" | "openocd_path" }; opened_by: { id: number; name: string; role: "human" | "ai" | "script" } | null; design_switch?: DesignSwitch } | { backend: "fprobe"; part: string; n_cores: number; hw_slots: number; watch_slots: number; debug_ap: number; discovery: "apb_rom_table" | "ahb_system_map"; cti: { board: string; apb_offset: number; event_trigin: number } | null; fpga_halt_watch: { bound_ns: number | "unmeasured"; poll_s: number } | null; fpga_request_times: { event_log: boolean } | null; options: HaltOptions; journal: HaltJournal; occupies: { probe_dap: boolean; refused_while_open: Array<"target.connect" | "lowlevel" | "linux.memap.connect_jtag" | "probe.design.switch">; device_lock: "per_call" | "openocd_path" }; opened_by: { id: number; name: string; role: "human" | "ai" | "script" } | null; design_switch?: DesignSwitch }
```

## linux.halt.close

種類: `control`。閉じる。gdb のサーバが動いていれば止め、後始末（BKPT・HW の bp/wp を外し、全コアを走らせ、停止デバッグを切る）が終わるまで待つ（時間で打ち切らない）。コアの走り・止まりは変えない（halt_all で止めたままのコアは止まったまま。応答の halted は閉じる前に読んだ各コアの状態）。

応答:

```ts
{ closed: false; open: false } | { closed: true; gdb: HaltGdbStatus | null; halted: Array<boolean> | null; debug: HaltDebugClose | null }
```

## linux.halt.status

種類: `read`。読むだけ（コアを止めない。CLI の status）: 開いているか・開いた事実・設定・占有・ジャーナル・停止デバッグを立てているか・各コアが走っているか・残っている HW の bp/wp（cores）・gdb のサーバの事実。gdb のサーバが動いている間はコアを読まない（cores = null と理由 cores_unread）。OpenOCD の経路はコアを読まない（同じく）。

応答:

```ts
{ open: false } | { open: true; backend: "openocd"; n_cores: number; hw_slots: number; watch_slots: number; actual_jtag_khz: number; options: HaltOptions; halting_debug: boolean; journal: HaltJournal; gdb: HaltGdbStatus | null; cores: Array<{ core: number; running: boolean; breakpoints: Array<number>; watchpoints: Array<number> }> | null; occupies: { probe_dap: boolean; refused_while_open: Array<"target.connect" | "lowlevel" | "linux.memap.connect_jtag" | "probe.design.switch">; device_lock: "per_call" | "openocd_path" }; opened_by: { id: number; name: string; role: "human" | "ai" | "script" } | null; cores_unread?: string; debug: { pid: number; comm: string; closed: boolean; running: boolean; breakpoints: number } | null } | { open: true; backend: "fprobe"; part: string; n_cores: number; hw_slots: number; watch_slots: number; debug_ap: number; discovery: "apb_rom_table" | "ahb_system_map"; cti: { board: string; apb_offset: number; event_trigin: number } | null; fpga_halt_watch: { bound_ns: number | "unmeasured"; poll_s: number } | null; fpga_request_times: { event_log: boolean } | null; options: HaltOptions; halting_debug: boolean; journal: HaltJournal; gdb: HaltGdbStatus | null; cores: Array<{ core: number; running: boolean; breakpoints: Array<number>; watchpoints: Array<number> }> | null; occupies: { probe_dap: boolean; refused_while_open: Array<"target.connect" | "lowlevel" | "linux.memap.connect_jtag" | "probe.design.switch">; device_lock: "per_call" | "openocd_path" }; opened_by: { id: number; name: string; role: "human" | "ai" | "script" } | null; cores_unread?: string; debug: { pid: number; comm: string; closed: boolean; running: boolean; breakpoints: number } | null }
```

## linux.halt.halt_all

種類: `control`。全コアを止める（止めたまま。linux.halt.resume_all で走らせる。CLI の halt）。停止の報告 stop をそのまま返す: method（sequential / simultaneous）・cores_halted・skew_upper_bound_ns（コア間の時間差の上限。null は分からない、0 ではない）・basis（上限の根拠）・assumptions（上限が頼った仮定。空なら仮定なし）・bound_without_assumptions_ns（仮定を使わない上限。仮定があるときだけ）。gdb のサーバが動いている間は断る。

応答:

```ts
{ stop: HaltStopReport }
```

## linux.halt.resume_all

種類: `control`。全コアを走らせる（CLI の resume）。gdb のサーバが動いている間は断る。

応答:

```ts
{ resumed: Array<number> }
```

## linux.halt.processes

種類: `control`。プロセスの一覧（スレッドグループのリーダー。idle を除く。並びはカーネルのタスク一覧の順。has_mm = false はカーネルスレッド。CLI の ps）。全コアが止まっていれば止め直さずに読み、走らせない（stop = null・resumed = false）。そうでなければ全コアを止めて読み、走らせる（stop = その停止の報告）。

応答:

```ts
{ processes: Array<{ tgid: number; comm: string; has_mm: boolean }>; stop: HaltStopReport | null; resumed: boolean }
```

## linux.halt.threads

種類: `control`。プロセス pid のスレッド（tid・comm・task_struct の番地。カーネルのスレッド一覧の順。CLI の threads）。止め方は linux.halt.processes と同じ。

| 引数 | 型 | 必須 |
|---|---|---|
| `pid` | `number` | ○ |

応答:

```ts
{ pid: number; comm: string; pgd: number; threads: Array<{ tid: number; comm: string; task: number }>; stop: HaltStopReport | null; resumed: boolean }
```

## linux.halt.recover

種類: `control`。死んだデバッガ（落ちたデーモン・強制終了された CLI）が残した BKPT を、ジャーナルの元の命令で戻す（今も BKPT がある所だけ: restored。BKPT がもう無かった所は書かない: stale。CLI の recover）。止め方は linux.halt.processes と同じ。

応答:

```ts
{ restored: number; stale: number; journal_before: HaltJournal; stop: HaltStopReport | null; resumed: boolean }
```

## linux.halt.rescue

種類: `control`。強制終了されたデバッガがコアを止めたまま残したとき（CLI の rescue）: ターゲットのメモリは触らず、各コアの bp/wp を消し、停止デバッグを切り、止まっていたコアを再開する（CTI の設定なら先に CTI の停止要求を解く）。メモリに残った BKPT は linux.halt.recover が戻す。fprobe の経路だけ。

応答:

```ts
{ resumed: Array<number>; cti_released: boolean; breakpoints_cleared: true; watchpoints_cleared: true; halting_debug: false; journal: HaltJournal }
```

## linux.halt.gdb.serve

種類: `control`。プロセス pid の gdb RSP サーバをデーモンの中で開く（127.0.0.1:port。手元の gdb が target remote 127.0.0.1:port でつなぐ。CLI の attach）。順は CLI と同じ: 全コアを止めてプロセスを読み、ジャーナルの BKPT を戻し、全コアを走らせて gdb を待ち、つないだら全コアを止めて応える（all-stop）。gdb のサーバが動いている間、コアの操作（halt_all 等）は断り、status はコアを読まない。gdb が去る（detach・切断）・linux.halt.gdb.stop・close・デーモンの終了で、BKPT と HW の bp/wp を外し、全コアを走らせ、停止デバッグを切る。

| 引数 | 型 | 必須 |
|---|---|---|
| `pid` | `number` | ○ |
| `port` | `number` | ○ |
| `skew_limit_ns` | `number` | ○ |
| `thread_id` | `"stack" | "contextidr"` | ○ |
| `kernel_isa` | `"arm" | "thumb"` | ○ |
| `max_step_retries` | `number` | ○ |
| `bp_unmapped` | `"error" | "hw"` | ○ |
| `log` | `string` |  |

応答:

```ts
{ port: number; pid: number; comm: string; pgd: number; threads: number; recovered: { restored: number; stale: number; journal_before: HaltJournal }; stop_for_attach: HaltStopReport | null; state: "listening" | "serving" | "ended"; ended_by: "detach" | "disconnect" | "stop" | "error" | null; error: string | null; cleanup_errors: Array<string>; cleanup_reconnected: boolean; cleanup_errors_after_reconnect: Array<string>; attach_stop: HaltStopReport | null; last_stop: { method: "sequential" | "simultaneous"; cores_halted: Array<number>; skew_upper_bound_ns: number | null; basis: string; assumptions: Array<string>; bound_without_assumptions_ns: number | null; skew_verdict: "within" | "exceeded" | "unknown"; skew_limit_ns: number } | null; stats: { counters: { foreign_bp_hits: number; foreign_watch_hits: number; kernel_mode_watch_hits: number; foreign_exit_hits: number; thread_exits: number; own_stops_without_breakpoint: number }; skew: { event: { stops: number; max_ns: number | null; unknown: number; exceeded: number }; interrupt: { stops: number; max_ns: number | null; unknown: number; exceeded: number }; request: { stops: number; max_ns: number | null; unknown: number; exceeded: number } }; resumes_cut_short: number } | null; connect: string }
```

## linux.halt.gdb.status

種類: `read`。gdb のサーバの事実（コアは読まない）: state（listening / serving / ended）・ended_by（detach / disconnect / stop / error）・error・cleanup_errors（後始末の失敗）・cleanup_reconnected（後始末が DAP の転送の誤りで失敗したので DP をつなぎ直してもう 1 度後始末したか）・cleanup_errors_after_reconnect（その 2 度目の失敗）・attach_stop（gdb がつないだときの停止）・last_stop（最後の停止の報告と skew_limit_ns との比較 skew_verdict）・stats（停止の種類ごとの上限の最大・分からない・超えた回数等）。開いたことが無ければ gdb = null。

応答:

```ts
{ gdb: HaltGdbStatus | null }
```

## linux.halt.gdb.stop

種類: `control`。gdb のサーバを止める（gdb の接続を切る）。後始末（BKPT・HW の bp/wp を外し、全コアを走らせ、停止デバッグを切る）が終わるまで待つ（時間で打ち切らない）。止めた後の gdb のサーバの事実を返す。

応答:

```ts
{ gdb: HaltGdbStatus | null }
```

## linux.halt.debug.attach

種類: `control`。AI が JTAG の全コア停止で止めて調べる（gdb を使わない）: 全コアを止めてプロセス pid を読み、前のデバッガが残した BKPT を journal で戻し、ELF の読み込み位置を補助ベクタ（mm->saved_auxv）から求め、全コアを止めたまま返す。linux.halt.open の後。gdb のサーバ（linux.halt.gdb.serve）とは同時に開けない。開いている間は linux.halt.halt_all・resume_all・processes・threads・recover・rescue も断る（linux.halt.debug.close で閉じる）。流れの例: attach → debug.break.insert（"race.c:11"）→ debug.run（wait_s）→ 当たったら debug.threads（variables=true, expressions=["counter"]）で全スレッドの行と変数を同じ時点で 1 回で読む → debug.run（wait_s=0）で再開 → debug.close。

| 引数 | 型 | 必須 |
|---|---|---|
| `pid` | `number` | ○ |
| `elf` | `string` | ○ |
| `skew_limit_ns` | `number` | ○ |
| `thread_id` | `"stack" | "contextidr"` | ○ |
| `kernel_isa` | `"arm" | "thumb"` | ○ |
| `max_step_retries` | `number` | ○ |
| `bp_unmapped` | `"error" | "hw"` | ○ |
| `max_halt_s` | `number` | ○ |
| `fetch_from_target` | `string | null` |  |
| `breakpoint_kind` | `"sw" | "hw" | null` |  |
| `step_max_instructions` | `number | null` |  |
| `step_timeout_s` | `number | null` |  |

応答:

```ts
{ pid: number; comm: string; elf: HaltDebugElf; image: HaltDebugImage; libraries: HaltDebugLibraries; running: boolean; closed: boolean; max_halt_s: number; skew_limit_ns: number; breakpoints: Array<HaltDebugBreakpoint>; attach_stop: HaltStopVerdict | null; last_stop: HaltDebugEvent | null; auto_resumed: Array<{ at_unix_s: number; halted_s: number; result?: HaltDebugEvent; error?: string }>; stats: { counters: { foreign_bp_hits: number; foreign_watch_hits: number; kernel_mode_watch_hits: number; foreign_exit_hits: number; thread_exits: number; own_stops_without_breakpoint: number }; skew: { event: { stops: number; max_ns: number | null; unknown: number; exceeded: number }; interrupt: { stops: number; max_ns: number | null; unknown: number; exceeded: number }; request: { stops: number; max_ns: number | null; unknown: number; exceeded: number } }; resumes_cut_short: number }; close: HaltDebugClose | null; threads: Array<{ tid: number; comm: string }>; recovered: { restored: number; stale: number; journal_before: HaltJournal }; stop_for_reading: HaltStopReport | null; fetched: { path: string; local_path: string; bytes: number; sha256: string } | null; last_run: HaltDebugEvent | null; last_threads: HaltDebugThreads | null; breakpoint_kind: "sw" | "hw" | null; sources_fetched: Array<{ name: string; target: string; path: string; local_path: string; bytes: number; sha256: string } | { name: string; target: string; path: string; error: string }>; step_max_instructions: number | null; step_timeout_s: number | null }
```

## linux.halt.debug.status

種類: `control`。linux.halt.debug の持ち主の事実（止まっているか・ブレークポイント・最後の停止・自動の再開・停止の数え）。コアは触らない。

応答:

```ts
HaltDebugStatus
```

## linux.halt.debug.break.insert

種類: `control`。ブレークポイントを置く（全コアが止まっているときだけ）。location: 関数名（行表の prologue_end があれば前置きの後）・"ファイル:行"（その行の最小の番地）・番地（"0x…"。実行時の番地）。置けた実行時の番地・リンク時の番地・方式・幅・関数・ファイル・行を返す。置けなければ理由（命令の無い行・名前が無い・未マップ・HW の空きなし）を誤りで返す。

| 引数 | 型 | 必須 |
|---|---|---|
| `location` | `string` | ○ |
| `kind` | `"sw" | "hw"` | ○ |

応答:

```ts
HaltDebugBreakpoint
```

## linux.halt.debug.break.remove

種類: `control`。ブレークポイントを外す（全コアが止まっているときだけ）。

| 引数 | 型 | 必須 |
|---|---|---|
| `id` | `number` | ○ |

応答:

```ts
{ removed: HaltDebugBreakpoint }
```

## linux.halt.debug.run

種類: `control`。全コアを再開して、対象のプロセスが止まる（ブレークポイント・終了・シグナル）まで最大 wait_s 秒待つ。当たれば全コアを止め、止めの事実（方式・コア間のずれの上限と根拠・当たったスレッド・番地・行）を返す。時間切れは kind=timeout（当たらなかった）を返し、全コアは走らせたまま（次の run は再開せずに待ちを続ける）。wait_s=0 は再開だけ（いまある当たりは拾う）。

| 引数 | 型 | 必須 |
|---|---|---|
| `wait_s` | `number` | ○ |

応答:

```ts
HaltDebugEvent
```

## linux.halt.debug.halt

種類: `control`。走っている全コアを止める（要求の停止。止まっていれば誤り）。同じ瞬間の当たりがあれば、その当たりを返す。

応答:

```ts
HaltDebugEvent
```

## linux.halt.debug.threads

種類: `control`。全コアが止まっている間に（コアを動かさずに）、プロセスの全スレッドの tid・どのコアにいたか（on_core。いなければ null）・モード・pc・関数・ファイルと行・レジスタ（registers_from: core = コアのレジスタ / pt_regs = カーネルに入ったときに保存したユーザのレジスタ）を 1 回で読む。variables=true でフレーム 0 の引数・局所変数、expressions で各スレッドのフレーム 0 で評価した式（大域変数は全スレッドで同じ時点の値）。読めないものは理由を返す。stop はどの停止の時点の値か。

| 引数 | 型 | 必須 |
|---|---|---|
| `variables` | `boolean` | ○ |
| `expressions` | `Array<string>` | ○ |
| `max_elements` | `number` | ○ |

応答:

```ts
HaltDebugThreads
```

## linux.halt.debug.read

種類: `control`。1 つのスレッドの指定のフレーム（0 = いま。1 以上は CFI で巻き戻した呼び出し元）で式を読む（大域・静的・局所・引数。C の式）。全コアが止まっているときだけ。式ごとに値か理由。

| 引数 | 型 | 必須 |
|---|---|---|
| `expressions` | `Array<string>` | ○ |
| `tid` | `number` | ○ |
| `frame` | `number` | ○ |
| `max_elements` | `number` | ○ |

応答:

```ts
{ tid: number; frame: { index: number; pc: number; function: string | null; offset: number | null; file: string | null; line: number | null } | null; values: Array<HaltDebugValue> }
```

## linux.halt.debug.memory

種類: `control`。プロセスの仮想メモリを読む（全コアが止まっているときだけ。置いた BKPT は元の命令で見せる）。未マップなら data=null と理由。

| 引数 | 型 | 必須 |
|---|---|---|
| `address` | `number` | ○ |
| `length` | `number` | ○ |

応答:

```ts
{ address: number; length: number; data: string | null; reason: string | null }
```

## linux.halt.debug.step

種類: `control`。スレッド tid を 1 行進める（他のコアは止めたまま 1 命令ずつ）。始めと違う行の文の始まりに着いたら止める。over = 呼び出しは戻り先まで走らせる、into = 行の情報のある関数に入ったら前置きの後で止める（ELF の外は戻り先まで）。max_instructions で止まらなければ reached_new_line=false と位置を返す。途中の当たり・終了はその停止を返す。 timeout_s（使う側が明示）を過ぎたら全コアを止めたまま timed_out = true（呼び出しを踏み越えている間も効く）。

| 引数 | 型 | 必須 |
|---|---|---|
| `tid` | `number` | ○ |
| `mode` | `"into" | "over"` | ○ |
| `max_instructions` | `number` | ○ |
| `timeout_s` | `number` | ○ |

応答:

```ts
HaltDebugEvent
```

## linux.halt.debug.select

種類: `control`。今のスレッドを選ぶ（画面のデバッガのレジスタ・呼び出し履歴・変数・ステップ・target.status の linux.tid がこのスレッドになる。次に止まると止まったスレッドに戻る）。コアは動かさない。人の Threads の選択と同じ。

| 引数 | 型 | 必須 |
|---|---|---|
| `tid` | `number` | ○ |

応答:

```ts
{ tid: number; tid_from: "selected" }
```

## linux.halt.debug.close

種類: `control`。閉じる: ブレークポイントを全部外し（journal も片付く）、全コアを走らせ、停止デバッグを切る。DAP の誤りで失敗したら DP をつなぎ直してもう 1 度だけ後始末する。

応答:

```ts
HaltDebugClose
```

## 応答の共通の形

### ActivityEntry

操作の記録 1 件（result と error はどちらか一方）

```ts
{ seq: number; time: string; client: string; role: "human" | "ai" | "script"; via: "api" | "gdb" | "tcp" | "hook"; method: string; params: { [key: string]: Json }; automated?: boolean; source?: string; result?: Json; error?: string; seconds: number; held_s: number }
```

### Basis

時刻の範囲の根拠（fprobe.timing の各関数の basis: 起点・前後の読み・段数・範囲の端を決めたものの名前。関数ごとに欄が違う）

```ts
{ start_time: number; before: number | null; after: number | null; delay_ticks: number; span_ticks: number; bounds: { t_lo: "start_time"; t_hi: "start_time" } } | { start_time: number; before: number | null; after: number | null; latency: number; lanes: number; bounds: { t_lo: "start_time"; t_hi: "start_time" }; fine_sample?: number } | { before: number; after: number; trigger_time: number | null; delay_ticks: number; span_ticks: number; bounds: { t_lo: "before"; t_hi: "after" | "trigger" }; start_valid?: boolean } | { t_lo: "previous_transfer" | null; t_hi: "returning_transfer" } | { t: "read_request_point"; t_lo: "jtag_update_dr" | "swd_header_start" | null; t_hi: "jtag_returning_update_dr" | "swd_returning_ack" | "returning_transfer" | null; waits: number | null; output_ticks: number | null } | { t_lo: "jtag_update_dr" | "swd_header_start" | "previous_transfer" | null; t_hi: "returning_transfer"; output_ticks: number | null }
```

### BreakpointAction

ブレークポイントの動作（条件・無視する回数・ログの式・当たった回数・最後の誤り）

```ts
{ condition: string | null; ignore_count: number; log: Array<string> | null; message: string | null; hits: number; last_error: string | null }
```

### BreakpointList

置いているブレークポイント（enabled = false は無効にして機器から外したもの。kind = 接続中の置き方、tool = 道具が置いたもの）とウォッチポイント

```ts
{ breakpoints: Array<{ address: number; location: string; enabled: boolean; where: string; function?: string; file?: string; line?: number; kind?: "hw" | "flash" | "sw"; action?: BreakpointAction; tool?: string; at?: { file: string; path: string; line: number } }>; watchpoints: Array<Watchpoint> }
```

### Build

ビットストリームの版（U49。fprobe_gw build がビルドのたびに書く）。id は入力の要約（ビルドディレクトリの

```ts
{ id: string; time: number; git: string | null; dirty: boolean }
```

### ByteLine

バイトのイベントの端子の上の点の範囲（µs。申告が無ければ範囲が null）

```ts
{ point: string | null; t_lo_us: number | null; t_hi_us: number | null; from: string | null; unknown: Array<string> }
```

### Caller

呼び出し元（デーモンが渡す {id, name, role}。デーモンを通さない止め方は id が null）

```ts
{ id: number | null; name: string; role: string }
```

### Capabilities

```ts
{ protocol_version: number | null; board: string | null; design: string | null; sys_clock_hz: number | null; timebase_hz: number | null; host_link: number | null; channels: Array<number>; la_channels: number; la_depth: number; la_sample_hz: number; mem_bytes: number; mem_test: boolean; xtrig: boolean; trig_seq: boolean; aux: boolean; sampler: boolean; jtag: boolean; inject: boolean; outputs: Array<Output>; output_actions: number; la_filter: boolean; la_pulse: boolean; la_stream_events: boolean; la_stream_ps: boolean; la_pack: boolean; la_testgen: boolean; fpga_obs: boolean; obs_match: boolean; sampler_match: boolean; sync: boolean; event_byte_pack: boolean; watch_start: boolean; dap_line_time: boolean; la_delay_internal: number; la_delay_deep: number; la_phase_span: number; la_channel_timing: Array<[number | null, number | null, number | null]>; build: Build | null; la_layout: Array<LaChannel>; swo_manchester: boolean; jtag_bit_extra_cycles: number; min_half_period: number; swd_sample_delay_max: number; uart_flow: Array<number>; la_testgen_hz: number; la_fine: number | null; la_stream_bytes_per_s: number; la_stream_usage: boolean; la_stream_gaps: boolean; timing: { [key: string]: TimingPoint }; unknown: { [key: string]: string }; dap_output_ticks: number | null }
```

### CaptureInfo

最後の捕捉の情報（source = la.capture / la.deep.capture / file / testpoint）

```ts
{ id: number; length: number; sample_period_ns: number; trigger_index: number; channels: number; source: string }
```

### CodeAtSample

PC のサンプル 1 つ（DWT_PCSR の読み。読めなかった・0xFFFFFFFF なら pc = null と reason）

```ts
{ t: number; dt_s: number; value: number; pc: number | null; function: string | null; offset: number | null; file: string | null; line: number | null; where: string | null; reason?: "read_failed" | "pcsr_invalid"; ack?: number }
```

### CodeAtTargetState

記録に残るターゲットの状態の出来事（recording.code_at。本文はデーモンの見張りの target.state = target.status の形と run・link_lost のまま。t は記録の時刻、dt_s は問い合わせの時刻との差）

```ts
{ t: number; dt_s: number; uncertainty_s: number | null; state?: CoreState; target?: string; frequency_hz?: number; wire?: "swd" | "jtag"; core?: number; arch?: string; hart?: number; debug_module?: DebugModuleInfo; architecture?: "armv7-a" | "armv6"; debug_version?: number; breakpoints?: number; watchpoints?: number; core_count?: number; debug_base?: number; apb_ap?: number; discovery?: "ahb_system_map" | "rom_table" | "part_table"; jtag_chain?: Array<number>; tap?: number; pc?: number; where?: string; function?: string; file?: string; line?: number; halt_reason?: "request" | "breakpoint" | "bkpt_instruction" | "watchpoint" | "vector_catch" | "external"; halt_bits?: Array<"HALTED" | "BKPT" | "DWTTRAP" | "VCATCH" | "EXTERNAL">; debug_mode?: "monitor"; fatal?: FatalState; rescue_dp?: number; cores?: Array<CoreEntry>; run?: number; link_lost?: { at: number; changed: { [key: string]: [Json, Json] }; version: { before: string | null; after: string | null; changed: boolean | null; latest_build: boolean | null } | null } }
```

### ControlCounters

CONTROL の数え（fprobe.control.ControlChannel.counters。流れの間の差）: read_retries = 応答が時間切れで送り直した READ（使う側が control_read_retries で明示したときだけ）、stale_responses = 読み捨てた前の要求の遅れた応答

```ts
{ read_retries: number; stale_responses: number }
```

### ControlState

操作権の状態（paused_by = 一時停止した利用者の名前、held = 保留している要求の数）

```ts
{ paused: boolean; paused_by: string | null; held: number }
```

### CoreEntry

複数ドロップの部品の 1 つのコアの状態（読めなければ error だけ）

```ts
{ core: number; selected: boolean; state: CoreState; pc?: number; where?: string; function?: string; file?: string; line?: number } | { core: number; selected: false; error: string }
```

### CoreState

コアの状態（pyOCD の Target.State の名前）

```ts
"RUNNING" | "HALTED" | "RESET" | "SLEEPING" | "LOCKUP"
```

### DapConfigDetail

PHY の設定（イベント 16）

```ts
{ half_period: number; idle_cycles: number; turnaround: number; data_phase: boolean; swd_sample_delay: number; jtag_sample_delay: number; jtag: boolean; enable: boolean; dr_before: number; dr_after: number; ir_before: number; ir_after: number; ir_len: number }
```

### DataGap

記録の変数のデータの欠け（fprobe.correlate。reason は画面で訳す）

```ts
{ reason: "reads_missing" | "read_failed" | "fpga_lost" | "fpga_untimed" | "pin_skew_unknown" | "pins_missing" | "events_error" | "recording_dropped"; t0: number; t1: number; count?: number; upper_bound?: boolean; kind?: string; error?: string | null; variable?: string }
```

### DebugMode

デバッグの方式（available = false はファームにモニタが無い: reason）。left は debug_mode.set がモニタ方式から戻したときだけ

```ts
{ mode: "halt" | "monitor"; available: false; reason: string; left?: { stop: "running" } | { stop: "breakpoint_at_pc" | "halt_request"; pc_before: number; pc_after: number } } | { mode: "halt" | "monitor"; available: true; box: number; priority: number; keeps_running: Array<{ exception: number; name: string; priority: number }>; prio_bits: number; fpu: boolean; stack_size: number; stack_overflow: boolean; state?: "stopped" | "running"; stops?: number; left?: { stop: "running" } | { stop: "breakpoint_at_pc" | "halt_request"; pc_before: number; pc_after: number } }
```

### DebugModuleInfo

RISC-V の Debug Module（fprobe.riscv_dm.DmInfo.as_dict）

```ts
{ version: string; progbuf_size: number; data_count: number; system_bus: boolean; harts: Array<number> }
```

### DecoderInfo

解読器 1 つ（fprobe.decode.registry.Decoder.describe。origin = builtin / python / ファイルの絶対パス。params の type = 設定の型（分からなければ null）、default は省略できる設定だけ）

```ts
{ name: string; lower: Array<string>; summary: string; params: Array<{ name: string; required: boolean; nullable: boolean; type: "channel" | "channels" | "integer" | "number" | "boolean" | "string" | "object" | null; default?: string | number | boolean | null }>; default_kinds: Array<string> | null; origin: string }
```

### DesignSwitch

構成（ビットストリーム）を書き替えた事実（fprobe.probe_design._switch_design）

```ts
{ before: string; after: string; bitstream: string; seconds: number; outputs: { restored: Array<number>; failed: { [key: string]: string } }; recording_stopped: string | null; release_failures: Array<{ step: string; error: string }> }
```

### DeviceAddress

機器のつなぎ方（usb = USB のシリアル番号、serial = COM ポート、eth = IP。分からなければ address は null）

```ts
{ link: "usb" | "serial" | "eth"; address: string | null }
```

### DiagnoseStep

接続の診断の 1 段（fprobe.diagnose.Step。data は段ごとの事実、無ければ省く）

```ts
{ name: "voltage"; status: "ok" | "fail" | "skip"; message: string; data?: VoltageReading } | { name: "pins"; status: "ok" | "fail" | "skip"; message: string; data: { swclk: number; swdio: number; nreset: number } } | { name: "swd"; status: "ok" | "fail" | "skip"; message: string; data: { attempts: Array<{ frequency_hz: number; method: string; ack: "ok" | "wait" | "fault" | "no_ack" | "protocol_error" | "other"; ignored_targetsel?: true; rescue_dp?: string }>; frequency_hz?: number; targetsel?: number } } | { name: "jtag"; status: "ok" | "fail" | "skip"; message: string; data: { scan: JtagChainScan; scans?: Array<{ frequency_hz: number; devices: number }>; frequency_hz?: number } } | { name: "dp_errors"; status: "ok" | "fail" | "skip"; message: string; data: { ctrl_stat: number } } | { name: "dp"; status: "ok" | "fail" | "skip"; message: string } | { name: "power"; status: "ok" | "fail" | "skip"; message: string; data: { ctrl_stat: number; debug_power: boolean; system_power: boolean } } | { name: "aps"; status: "ok" | "fail" | "skip"; message: string } | { name: "ap_idr"; status: "ok" | "fail" | "skip"; message: string; data: { ap: number } } | { name: "ap_disabled"; status: "ok" | "fail" | "skip"; message: string; data: { ap: number } } | { name: "core"; status: "ok" | "fail" | "skip"; message: string; data?: { value: number; implementer: number; partno: number; core: string; variant: number; revision: number; architecture: number; ap: number; dhcsr: number; halted: boolean; sleeping: boolean; lockup: boolean; debug_enabled: boolean } } | { name: "part"; status: "ok" | "fail" | "skip"; message: string; data?: Part } | { name: "protection"; status: "ok" | "fail" | "skip"; message: string; data?: { ap: number; approtect: true } | { rdp_level: 0 | 1 | 2; option_bytes: number } }
```

### Diagnosis

接続の診断（fprobe.diagnose.Diagnosis.as_dict）

```ts
{ reached: boolean; frequencies_hz: Array<number>; protocol: "swd" | "jtag" | null; frequency_hz: number | null; dp: { dpidr: DpIdr; jtag_dp_index?: number; targetid?: { value: number; designer: string; designer_code: number; partno: number; revision: number }; power?: { ctrl_stat: number; debug_power: boolean; system_power: boolean } } | null; aps: Array<{ ap: number; value: number; present: boolean; kind: "none" | "mem-ap" | "jtag-ap" | "other"; class: number; type: number; bus: string | null; variant: number; revision: number; designer: string; designer_code: number; csw?: number; device_enabled?: boolean; base?: number; rom_table?: RomComponent } | { ap: number; kind: "unreadable"; error: string }>; core: { value: number; implementer: number; partno: number; core: string; variant: number; revision: number; architecture: number; ap: number; dhcsr: number; halted: boolean; sleeping: boolean; lockup: boolean; debug_enabled: boolean } | null; part: Part | null; jtag: JtagChainScan | null; steps: Array<DiagnoseStep> }
```

### Distribution

値の分布（fprobe.profile.stats.Distribution.as_dict。値が無ければ count = 0 だけ）

```ts
{ count: number; min?: number; max?: number; mean?: number; stdev?: number; median?: number; p90?: number; p99?: number }
```

### DpIdr

DP の DPIDR を解いたもの（coresight_id.decode_dpidr）

```ts
{ value: number; valid: boolean; designer: string; designer_code: number; version: number; min: boolean; partno: number; revision: number; adi: "ADIv5" | "ADIv6" }
```

### ElfLoaded

読み込んだ ELF（入口・読み込む区画とソースの一覧）

```ts
{ path: string; entry: number; segments: Array<{ address: number; bytes: number }>; files: Array<{ name: string; path: string; exists: boolean; built_path?: string }>; same_name: { [key: string]: Array<string> } }
```

### ElfNotice

ELF の更新の知らせ（通知 elf.changed と同じ）

```ts
{ path: string; time: string; reloaded: boolean; error: string | null; image_changed?: boolean; target_image: "current" | "stale" | "unknown"; message: string }
```

### ElfStatus

読み込んだ ELF と更新の見張りの様子

```ts
{ path: string | null; loaded_at: string | null; watching: boolean; auto_reload: boolean; interval_s: number; file_changed: boolean; target_image: "current" | "stale" | "unknown"; changes: number; reloads: number; last: ElfNotice | null; error: string | null }
```

### EtmElement

解読したパケット 1 つ（fprobe.trace.elements の要素の as_dict。raw はバイトの 16 進）

```ts
{ kind: "async"; offset: number; raw: string; skipped: number } | { kind: "trace_info"; offset: number; raw: string; cycle_counting: boolean; cc_threshold: number | null } | { kind: "trace_on"; offset: number; raw: string } | { kind: "address"; offset: number; raw: string; address: number; isa: string; form: string } | { kind: "context"; offset: number; raw: string; unchanged: boolean; non_secure: boolean | null; exception_level: number | null; vmid: number | null; context_id: number | null } | { kind: "atoms"; offset: number; raw: string; pattern: string } | { kind: "exception"; offset: number; raw: string; number: number; name: string; return_address: number | null; fault_pending: boolean; address_kind: number } | { kind: "exception_return"; offset: number; raw: string } | { kind: "function_return"; offset: number; raw: string } | { kind: "timestamp"; offset: number; raw: string; value: number; cycles: number | null } | { kind: "cycle_count"; offset: number; raw: string; count: number | null } | { kind: "event"; offset: number; raw: string; events: number } | { kind: "overflow"; offset: number; raw: string } | { kind: "discard"; offset: number; raw: string } | { kind: "ignore"; offset: number; raw: string } | { kind: "decode_error"; offset: number; raw: string; message: string } | { kind: "truncated"; offset: number; raw: string } | { kind: "unsynced"; offset: number; raw: string }
```

### EtmInstruction

再構成した命令 1 つ（index = 通し番号。ブロックの最後なら branch と、成立した直接の分岐の target）

```ts
{ index: number; address: number; size: number; mnemonic: string; operands: string; function?: string; offset?: number; file?: string; line?: number; branch?: "taken" | "not_taken" | "exception"; target?: number }
```

### EtmMarker

命令の流れの中の出来事（index = その前に実行した命令の数）

```ts
{ kind: "exception_return" | "trace_on" | "overflow"; index: number } | { kind: "timestamp"; index: number; value: number; cycles?: number } | { kind: "event"; index: number; events: number } | { kind: "decode_error" | "truncated" | "unsynced"; index: number; offset: number; message: string } | { kind: "exception"; index: number; number: number; name: string; return_address: number | null; fault_pending?: true } | { kind: "lost"; index: number; address: number; reason: string }
```

### Evaluation

C の式を評価して読んだ値（address はメモリ上の値のとき）

```ts
{ expression: string; type: string; address?: number; value: Value }
```

### EventWindowInfo

出来事の窓の範囲・出どころ・読めなかった出来事の数（duration_s は閉じた後）

```ts
{ t0: number; t1: number | null; source: "recording" | "event_log"; timebase_hz: number; lost_events: number; duration_s?: number; notes: Array<string> }
```

### ExceptionStats

割り込み 1 つの統計（fprobe.profile.exceptions.ExceptionProfile.exceptions の 1 行。時間はクロック、seconds・rate_hz は clock_hz があるとき）

```ts
{ exception: number; name: string; count: number; inclusive: Distribution; self: Distribution; total_inclusive: number; total_self: number; max_at: number; max_end: number; load: number | null; seconds?: { inclusive: Distribution; self: Distribution }; rate_hz?: number | null }
```

### ExpressionWrite

式に書いて読み直した値（debug.write_expression）。via = 選んだ道（halted = 止まったフレームで、running = 止めずに watch.write と同じ道）

```ts
{ expression: string; type: string | null; address?: number; value: Value; via: "halted" | "running"; atomic?: boolean }
```

### FatalState

走っているコアが致命的エラーで回り続けている様子（fprobe.liveness）

```ts
{ kind: "lockup" | "pc_in" | "tick_stopped"; alert: boolean; since: number; function?: string; evidence: Array<{ clue: "lockup" } | { clue: "pc_in"; function: string; valid: number; pc_samples: number } | { clue: "pc_range"; function: string; bytes: number; valid: number; pc_samples: number } | { clue: "tick_stopped"; tick: number }> }
```

### FaultCause

故障・停止の理由のビット 1 つ（register = CFSR の部分か HFSR・DFSR）

```ts
{ bit: string; register: string; meaning: string; hint?: string }
```

### FaultReport

止まったコアの故障の解析（fprobe.fault.analyze。registers・locations・write_one_to_clear は ARMv7-M だけ、frame は例外の中で止まったときだけ（LR が EXC_RETURN でなければ null））

```ts
{ architecture: "ARMv6-M / v8-M Baseline" | "ARMv7-M / v8-M Mainline"; exception: { number: number; name: string; fault: boolean } | null; causes: Array<FaultCause>; faulted: boolean; halt_reasons: Array<FaultCause>; registers?: { CFSR: number; HFSR: number; DFSR: number; MMFAR: number | null; BFAR: number | null; CPACR: number }; locations?: { CFSR: { address: number; size: number }; HFSR: { address: number; size: number }; DFSR: { address: number; size: number }; MMFAR: { address: number; size: number }; BFAR: { address: number; size: number }; CPACR: { address: number; size: number } }; write_one_to_clear?: Array<string>; fault_address?: { address: number; where?: string }; frame?: { stack: "PSP" | "MSP"; sp: number; sp_source: "psp" | "entry_sp" | "msp_now"; exc_return: number; fp_context: boolean; registers: { r0: number; r1: number; r2: number; r3: number; r12: number; lr: number; pc: number; xpsr: number }; caller_sp: number; locations: { r0: { address: number; size: number }; r1: { address: number; size: number }; r2: { address: number; size: number }; r3: { address: number; size: number }; r12: { address: number; size: number }; lr: { address: number; size: number }; pc: { address: number; size: number }; xpsr: { address: number; size: number } }; pc: { address: number; where?: string }; lr: { address: number; where?: string } } | null }
```

### FaultSummary

故障で止まったときの原因の要約（target.fault の causes の bit と、積まれたフレームの PC の場所。フレームが無ければ null）

```ts
{ causes: Array<string>; at: { address: number; where?: string } | null }
```

### FlashComparison

ファイルとターゲットのメモリの比較（fprobe.flash.Comparison）

```ts
{ match: boolean; bytes: number; differing_bytes: number; differences: Array<{ address: number; length: number; expected: string; actual: string }>; more_differences: number; read: Rate }
```

### FlashImage

書く中身（fprobe.flash.Image.describe）

```ts
{ source: string; format: string; bytes: number; segments: Array<{ address: number; bytes: number }> }
```

### FlashRegion

Flash の領域（fprobe.flash.FlashMemory.describe）

```ts
{ name: string; start: number; size: number; page_size: number | null; sectors: Array<{ start: number; count: number; size: number }>; erased_value: number; boot: boolean; algorithm: string; usable: boolean }
```

### FpgaObsDeclared

観測の回路の申告（fprobe.fpga_obs.FpgaObs.open。遅れは周期、範囲は端を含む。標本型は範囲が null）

```ts
{ id: number; mode: "stream" | "snapshot"; version: number; width: number; groups: number; fifo_depth: number; clock_hz: number; sync_delay: number; sample_delay: number; lost_bits: number; sync_delay_lo: number | null; sync_delay_hi: number | null; sample_delay_lo: number | null; sample_delay_hi: number | null }
```

### FpgaSignalDefs

FPGA の観測の組に付ける信号の定義（組の番号 → 信号の並び。enum は値 → 名前）

```ts
{ groups: { [key: string]: Array<{ name: string; lsb: number; width: number; format: "hex" | "dec" | "bin" | "enum"; enum: { [key: string]: string } }> } }
```

### HaltDebugBreakpoint

linux.halt.debug のブレークポイント

```ts
{ id: number; location: string; address: number; link_address: number; kind: "sw" | "hw"; requested_kind: "sw" | "hw"; width: 2 | 4; prologue_end: boolean | null; function: string | null; file: string | null; line: number | null }
```

### HaltDebugClose

linux.halt.debug の後始末（BKPT・HW の bp/wp を外し、全コアを走らせ、停止デバッグを切った）

```ts
{ closed: true; breakpoints_removed: number; cleanup_errors: Array<string>; cleanup_reconnected: boolean; cleanup_errors_after_reconnect: Array<string> }
```

### HaltDebugElf

```ts
{ path: string; bytes: number; sha256: string; elf_type: string; e_entry: number; has_dwarf: boolean }
```

### HaltDebugEvent

linux.halt.debug の止まった（か、走らせたまま返った）事実。address・function・file・line は止まったスレッドの pc（ELF の外なら null）。stop は全コアの停止（走っていれば null）

```ts
{ kind: "swbreak" | "hwbreak" | "watch" | "trap" | "int" | "exit" | "signal" | "timeout" | "step"; tid: number | null; address: number | null; function: string | null; file: string | null; line: number | null; breakpoint: number | null; running: boolean; stop: HaltStopVerdict | null; code?: number; signal?: number; watch_address?: number; access?: string; candidates?: number; reached_new_line?: boolean; instructions?: number; stepped_over_calls?: number; from_address?: number; from_function?: string | null; from_file?: string | null; from_line?: number | null; while_running?: { counters: { [key: string]: number }; foreign_hits: Array<{ seq: number; core: number; tid: number; address: number; moe: number }>; thread_id_method: "stack" | "contextidr"; target_threads: Array<number> }; timed_out?: boolean; steps_done?: number }
```

### HaltDebugImage

実行ファイルの読み込み位置（補助ベクタから）と、ELF が動いているものと同じかの事実（phdr_match: AT_PHDR のメモリと ELF のプログラムヘッダの一致、entry_match: AT_ENTRY == e_entry + load_bias）

```ts
{ at_phdr: number | null; at_entry: number | null; at_phnum: number | null; e_entry: number; elf_type: string; pt_phdr_vaddr: number | null; load_bias: number | null; basis: "AT_PHDR - PT_PHDR.p_vaddr" | "AT_ENTRY - e_entry" | null; phdr_match: boolean | null; phdr_unreadable: string | null; entry_match: boolean | null }
```

### HaltDebugLibraries

動的リンカの r_debug → link_map の鎖（各ライブラリの名前と読み込み位置のずれ）。たどれなければ reason

```ts
{ r_debug: number | null; libraries: Array<{ name: string; load_bias: number; link_map: number; name_truncated_at?: number }> | null; reason: string | null }
```

### HaltDebugStatus

linux.halt.debug の持ち主の事実

```ts
{ pid: number; comm: string; elf: HaltDebugElf; image: HaltDebugImage; libraries: HaltDebugLibraries; running: boolean; closed: boolean; max_halt_s: number; skew_limit_ns: number; breakpoints: Array<HaltDebugBreakpoint>; attach_stop: HaltStopVerdict | null; last_stop: HaltDebugEvent | null; auto_resumed: Array<{ at_unix_s: number; halted_s: number; result?: HaltDebugEvent; error?: string }>; stats: { counters: { foreign_bp_hits: number; foreign_watch_hits: number; kernel_mode_watch_hits: number; foreign_exit_hits: number; thread_exits: number; own_stops_without_breakpoint: number }; skew: { event: { stops: number; max_ns: number | null; unknown: number; exceeded: number }; interrupt: { stops: number; max_ns: number | null; unknown: number; exceeded: number }; request: { stops: number; max_ns: number | null; unknown: number; exceeded: number } }; resumes_cut_short: number }; close: HaltDebugClose | null; last_run: HaltDebugEvent | null; last_threads: HaltDebugThreads | null; options: { pid: number; elf: string; skew_limit_ns: number; thread_id: string; kernel_isa: string; max_step_retries: number; bp_unmapped: string; max_halt_s: number; fetch_from_target: string | null; breakpoint_kind: "sw" | "hw" | null; step_max_instructions: number | null; step_timeout_s: number | null } }
```

### HaltDebugThreads

```ts
{ pid: number; threads: Array<{ tid: number; comm: string; on_core: number | null; mode: "user" | "kernel" | null; registers_from: "core" | "pt_regs"; registers: { r0: number; r1: number; r2: number; r3: number; r4: number; r5: number; r6: number; r7: number; r8: number; r9: number; r10: number; r11: number; r12: number; sp: number; lr: number; pc: number; cpsr: number } | null; pc: number | null; function: string | null; file: string | null; line: number | null; error?: string; arguments?: Array<Variable> | null; locals?: Array<Variable> | null; variables_error?: string; expressions?: Array<HaltDebugValue> }>; stop: HaltStopVerdict | null; load_bias: number }
```

### HaltDebugValue

式 1 つの値（Evaluation）か、読めなかった理由（error）

```ts
Evaluation | { expression: string; error: string }
```

### HaltGdbStatus

linux.halt の gdb のサーバの事実（state・ended_by・後始末の誤り・最後の停止・数え）

```ts
{ port: number; pid: number; comm: string; pgd: number; threads: number; recovered: { restored: number; stale: number; journal_before: HaltJournal }; stop_for_attach: HaltStopReport | null; state: "listening" | "serving" | "ended"; ended_by: "detach" | "disconnect" | "stop" | "error" | null; error: string | null; cleanup_errors: Array<string>; cleanup_reconnected: boolean; cleanup_errors_after_reconnect: Array<string>; attach_stop: HaltStopReport | null; last_stop: { method: "sequential" | "simultaneous"; cores_halted: Array<number>; skew_upper_bound_ns: number | null; basis: string; assumptions: Array<string>; bound_without_assumptions_ns: number | null; skew_verdict: "within" | "exceeded" | "unknown"; skew_limit_ns: number } | null; stats: { counters: { foreign_bp_hits: number; foreign_watch_hits: number; kernel_mode_watch_hits: number; foreign_exit_hits: number; thread_exits: number; own_stops_without_breakpoint: number }; skew: { event: { stops: number; max_ns: number | null; unknown: number; exceeded: number }; interrupt: { stops: number; max_ns: number | null; unknown: number; exceeded: number }; request: { stops: number; max_ns: number | null; unknown: number; exceeded: number } }; resumes_cut_short: number } | null }
```

### HaltJournal

ブレークポイントのジャーナルのファイルの事実（entries = null は読めなかった: error）

```ts
{ path: string; exists: boolean; entries: Array<{ pa: number; width: number; owners: Array<[number, number]> }> | null; error?: string }
```

### HaltOptions

渡した設定（渡していないものは無い）

```ts
{ backend: "fprobe" | "openocd"; jtag_khz: number; offsets: string; page_offset: number; phys_offset: number; linear_map_end: number; max_tasks: number; max_threads: number; mmu_format: "short" | "lpae"; journal: string; fpga_request_times: boolean; part?: string; fpga_halt_watch_bound_ns?: number | "unmeasured"; fpga_halt_watch_poll_s?: number; cti_board?: string; cti_request_bound_ns?: number; cti_event_bound_ns?: number | "unmeasured"; cti_hw_wait_s?: number; cti_ack_polls?: number; ocd_board?: string; ocd_port?: number; ocd_timeout_s?: number; hw_slots?: number; watch_slots?: number }
```

### HaltStopReport

1 回の全コアの停止の事実（fprobe.linux_halt.core_access.StopReport。上限 null = 分からない）

```ts
{ method: "sequential" | "simultaneous"; cores_halted: Array<number>; skew_upper_bound_ns: number | null; basis: string; assumptions: Array<string>; bound_without_assumptions_ns: number | null }
```

### HaltStopVerdict

全コアの停止の事実（HaltStopReport）と、使う側が渡した skew_limit_ns との比較。根拠の文 basis は、バックエンドが fpga_request_times・FPGA の停止の見張り・CTI の有無で書き分けたものをそのまま

```ts
{ method: "sequential" | "simultaneous"; cores_halted: Array<number>; skew_upper_bound_ns: number | null; basis: string; assumptions: Array<string>; bound_without_assumptions_ns: number | null; skew_verdict: "within" | "exceeded" | "unknown"; skew_limit_ns: number }
```

### HeapScan

アロケータ 1 つの走査（fprobe.heap.HeapScan.to_dict。histogram = 大きさの箱 "<=N" → 数、counters = アロケータの数え（free_bytes 等）、callers は記録中、changes は compare のとき）

```ts
{ allocator: "freertos" | "newlib_nano" | "newlib"; scheme: string; regions: Array<[number, number]>; total: number; used: number; free: number; used_blocks: number; free_blocks: number; largest_free: number; fragmentation: number; overhead: number; histogram: { [key: string]: number }; counters: { [key: string]: number }; complete: boolean; issues: Array<{ kind: string; address: number | null; detail: string; previous_used?: number | null }>; notes: Array<string>; blocks?: Array<{ address: number; size: number; used: boolean; pointer: number | null }>; blocks_truncated?: boolean; callers?: Array<{ caller: string; count: number; bytes: number }>; changes?: { first: true } | { first: false; new: Array<{ pointer: number; size: number }>; gone: Array<{ pointer: number; size: number }>; new_count: number; gone_count: number; new_bytes: number; gone_bytes: number; new_truncated: boolean; gone_truncated: boolean; limit: number } }
```

### Hook

```ts
{ id: number; event: "connect" | "disconnect" | "reset" | "flash" | "resume" | "halt"; phase: "before" | "after"; actions: Array<HookAction>; name: string; required: boolean; enabled: boolean }
```

### HookAction

```ts
{ evaluate: string } | { mark: string } | { call: string; params?: { [key: string]: Json } }
```

### InjectCheck

```ts
{ check: string; ok: boolean; value?: Json; expected?: Json; error?: string }
```

### InjectRun

```ts
{ name: string; sha256: string; requirements: { [key: string]: string }; verdict: "pass" | "warn" | "no_data" | "not_triggered" | "error" | "inject_error" | "fail"; counts: { pass: number; fail: number; not_triggered: number; inject_error: number; error: number }; trials: Array<InjectTrial> }
```

### InjectTrial

```ts
{ id: string; repeat: number; requirement: Array<string>; description: string; when: { [key: string]: Json }; injected: boolean; t: number | null; timeout_s: number; observe_s: number; previous?: number; note?: string; address?: number; value?: number; verdict: "pass" | "fail" | "not_triggered" | "inject_error" | "error"; error?: string; checks?: Array<InjectCheck>; restore_error?: string }
```

### Instruction

1 命令か命令の間のデータ（fprobe.disasm.Instruction.as_dict。bytes は 16 進）

```ts
{ address: number; bytes: string; mnemonic: string; operands: string; kind: "code" | "data"; target: number | null; target_name: string | null; literal: number | null; literal_value: number | null; literal_name: string | null; flow: "call" | "jump" | "return" | ""; conditional: boolean; function: string | null; offset: number | null; file: string | null; line: number | null; new_line: boolean; data_basis: "mapping" | "branch_table" | "given" | "undecodable" | null; in_flash: boolean | null }
```

### IntervalReport

区間の統計（profile.timing.IntervalReport.as_dict。時刻・時間はタイムベースの刻み。統計は count > 0 のとき、seconds は timebase_hz があるとき。intervals = [開始, 時間]）

```ts
{ count: number; unit: "ticks"; timebase_hz: number | null; min?: number; max?: number; mean?: number; stdev?: number; median?: number; p90?: number; p99?: number; min_at?: number; max_at?: number; histogram?: Array<{ lo: number; hi: number; count: number }>; longest?: Array<{ t: number; t_end: number; duration: number; bin: number }>; seconds?: { min: number; max: number; mean: number; stdev: number; median: number; p90: number; p99: number }; intervals: Array<[number, number]>; interval_bins: Array<number>; intervals_total?: number }
```

### ItmPacket

解読した ITM / DWT のパケット 1 つ（type がパケットの種類。タイムスタンプのパケットは各パケットの local_time・global_time に入る）

```ts
{ type: "Sync"; skipped: number; local_time: number | null; global_time: number | null } | { type: "Resync"; skipped: number; local_time: number | null; global_time: number | null } | { type: "Overflow"; local_time: number | null; global_time: number | null } | { type: "Instrumentation"; port: number; value: number; size: number; local_time: number | null; global_time: number | null } | { type: "EventCounter"; flags: number; local_time: number | null; global_time: number | null } | { type: "ExceptionTrace"; number: number; action: "enter" | "exit" | "return"; name: string; local_time: number | null; global_time: number | null } | { type: "PcSample"; pc: number | null; local_time: number | null; global_time: number | null } | { type: "DataTracePc"; comparator: number; pc: number; local_time: number | null; global_time: number | null } | { type: "DataTraceAddress"; comparator: number; address_low: number; local_time: number | null; global_time: number | null } | { type: "DataTraceValue"; comparator: number; write: boolean; value: number; size: number; local_time: number | null; global_time: number | null } | { type: "Extension"; value: number; source: number; local_time: number | null; global_time: number | null } | { type: "DecodeError"; reason: string; data: string; local_time: number | null; global_time: number | null }
```

### Json

任意の JSON の値（利用者が渡した値をそのまま返す等、形を決めない所だけ）

```ts
string | number | boolean | null | Array<Json> | { [key: string]: Json }
```

### JtagChainDevice

```ts
{ index: number; idcode: number | null; name: string | null; ir_length: number | null; ir_length_source: "table" | "measured" | "ambiguous" | "unknown"; ir_candidates?: Array<number>; designer?: string; partno?: number; version?: number }
```

### JtagChainScan

JTAG のチェーンの走査（jtag_chain.ChainScan.as_dict）

```ts
{ devices: Array<JtagChainDevice>; count: number; total_ir_length: number | null; ir_capture: number | null; jtag_chain: Array<number> | null; tdo: "ok" | "stuck_high" | "stuck_low"; notes: Array<string> }
```

### LaCaptureInfo

最後の捕捉の情報（id = 捕捉の通し番号、source = la.capture / la.deep.capture / la.load 等）

```ts
{ id: number; length: number; sample_period_ns: number; trigger_index: number; channels: number; source: string }
```

### LaChannel

ロジアナの ch 1 本（ケイパビリティ LA_LAYOUT。U22）。kind: "pin" = 拡張ヘッダ（デバッグコネクタ）のピン

```ts
{ channel: number; kind: "pin" | "internal" | "input" | "none"; index: number }
```

### LaChannelTiming

ロジアナの ch ごとの時刻の事実（fprobe.timing.la_channel_timing。skew_ticks・span_ticks が null なら分からない）

```ts
{ channel: number; kind: "pin" | "internal" | "input" | "none"; skew_ticks: number | null; span_ticks: number | null; from: Array<"LA_CH_TIMING" | "LA_DELAY" | "config"> }
```

### LaRecordingStatus

ロジアナの流し続けを記録へ入れている様子（fprobe.recording_stream.LaRecordingStream.status）

```ts
{ running: boolean; blocks: number; error: string | null; events: number | null; samples?: number; duration_s?: number; mb_per_s?: number; overrun?: boolean; sample_period_ns?: number; link_buffer?: StreamBuffer | null; gaps?: number; gap_samples?: number; unplaced_beats?: number; usage?: LaStreamUsage; anchor?: { ticks: number; uncertainty_ticks: number; method: "circuit" | "trigger" | "host"; t_lo: number; t_hi: number; basis: Basis | null }; control?: ControlCounters }
```

### LaStreamUsage

流している間のリングと帯域の使用量（fprobe.la.LaStream.usage。数えの無い古い回路は欄が null で unknown に名前）

```ts
{ t_s: number; received_bytes: number; unknown: Array<string>; overrun: boolean; ring_beats: number | null; ring_unread_beats: number | null; ring_peak_beats: number | null; encoded_bytes: number | null; interval_s: number; received_bytes_per_s: number | null; encoded_bytes_per_s: number | null }
```

### LastConnect

```ts
{ target: string; frequency_hz: number; connect_mode: string; jtag_chain: Array<number> | null; script: string | null; core: number | null } | null
```

### LinkLost

Primer の USB が切れて開き直した事実（changed = 前と変わった機能 {名前: [前, 後]}、version = 版の比較）

```ts
{ at: number; changed: { [key: string]: [Json, Json] }; version: { before: string | null; after: string | null; changed: boolean | null; latest_build: boolean | null } | null }
```

### LinuxAgentDeploy

```ts
{ binary: string; compiler: string; source_sha256: string }
```

### LinuxAgentStatus

```ts
{ running: boolean; exit_code: number | null; stderr_tail: string; watchdog_s: number | null; event_errors: number; last_event_error: string | null; bad_lines: number; deployed: LinuxAgentDeploy | null; sync_error: string | null; ring: LinuxRing | null; ring_stats: LinuxRingStats; trace_unparsed: number; trace_unrouted: number }
```

### LinuxAttaching

linux.halt.debug.attach が進行中（まだ止めて調べる対象が無い。pid・始めた UNIX 秒）

```ts
{ pid: number; since_unix_s: number }
```

### LinuxCapabilities

Linux のターゲットの能力の事実（fprobe.linux.caps.parse_caps。読めなかったものは null）

```ts
{ kernel: string | null; machine: string | null; config_source: string | null; config: { CONFIG_UPROBES: string | null; CONFIG_UPROBE_EVENTS: string | null; CONFIG_FTRACE: string | null; CONFIG_KPROBES: string | null; CONFIG_BPF_SYSCALL: string | null; CONFIG_BPF_JIT: string | null; CONFIG_DEBUG_INFO_BTF: string | null; CONFIG_PERF_EVENTS: string | null; CONFIG_HAVE_HW_BREAKPOINT: string | null; CONFIG_DEVMEM: string | null; CONFIG_STRICT_DEVMEM: string | null; CONFIG_DEBUG_FS: string | null }; tracefs: string | null; uprobe_events: boolean; kprobe_events: boolean; trace_clocks: Array<string> | null; perf_event_paranoid: number | null; ptrace_scope: number | null; tools: { gcc: string | null; gdbserver: string | null; gdb: string | null; perf: string | null; bpftrace: string | null }; uid: number | null; sudo_nopasswd: boolean }
```

### LinuxCoreStatus

```ts
{ pattern: string; pipe: boolean; uses_pid: number; suid_dumpable: number; process: { pid: number; limit_soft: number | null; limit_hard: number | null; cwd: string | null; exe: string | null } | null }
```

### LinuxEvent

Linux のターゲットの出来事（method ごとの形。host_s・sync は時刻合わせの組で換算できたときだけ）

```ts
{ method: "ptrace"; probe: string; tid: number; target_ns: number; regs: { [key: string]: number }; clock: "CLOCK_MONOTONIC_RAW"; paths: Array<"a" | "b">; agent_seq: number | null; seq: number; host_s?: number; sync?: LinuxSync } | { method: "uprobe" | "ftrace"; probe: string; pid: number; comm: string; cpu: number; target_ns: number; args: { [key: string]: string }; text: string; clock: string | null; agent_seq: null; seq: number; host_s?: number; sync?: LinuxSync } | { method: "perf"; probe: string; pid: number; tid: number; cpu: number; comm: string; period: number; event: string; ip: number; dso: string; target_ns: number; clock: "CLOCK_MONOTONIC_RAW"; resolution_ns: 1000; agent_seq: null; seq: number; host_s?: number; sync?: LinuxSync } | { method: "agent"; probe: string; target_ns: null; detail: { [key: string]: Json }; agent_seq: null; seq: number }
```

### LinuxGdbRelayStatus

```ts
{ port: number | null; connected: boolean; target_running: boolean; exit_code: number | null; bytes_to_target: number; bytes_from_target: number; closed_by: "error" | "gdb" | "target" | "stop" | null; error: string | null; stderr_tail: string }
```

### LinuxProbe

```ts
{ method: "uprobe"; name: string; binary: string; sysroot_file: string; sha256: string; site: { offset: number; vaddr: number; thumb: boolean; machine: string }; line: string } | { method: "ptrace"; name: string; binary: string; sysroot_file: string; sha256: string; site: { offset: number; vaddr: number; thumb: boolean; machine: string }; plan: { path: string; offset: number; thumb: boolean; length: number; insn: string }; pid: number; regs: Array<string>; bp: number; address: number | null; threads: number | null; ended?: { code: number | null; signal: number | null } | { agent: string } }
```

### LinuxRing

```ts
{ phys: number; size: number; cached: boolean; linger: boolean; capacity: number; first: number }
```

### LinuxRingStats

```ts
{ reads: number; halted_ms_total: number; halted_ms_max: number; records: number; lost: number; torn: number; corrupt: number; header_errors: number; error: string | null }
```

### LinuxSourceFetchError

Linux のプロセス: attach のときに対象から取り寄せられなかったソース（error = 理由）

```ts
{ target: string; path: string; error: string }
```

### LinuxSourceFetched

Linux のプロセス: attach のとき（コアを止める前）に対象から取り寄せたソースの写し（target = linux.connect のターゲット、path = 対象の上の道（行表の場所）、local_path = 手元の写し）

```ts
{ target: string; path: string; local_path: string; bytes: number; sha256: string }
```

### LinuxSync

対象の時刻を PC の時刻へ換算した根拠（往復の半分・近い組までの時間・組の範囲の外か）

```ts
{ method: "network"; half_rtt_ns: number; nearest_pair_ns: number; outside: boolean }
```

### LinuxTargetFacts

target.status の linux 欄: Linux のプロセスに linux.halt.debug.attach している間の事実（今の対象がそのプロセスであること）。tid = 今のスレッド（決まり方は tid_from）。pc・場所・レジスタ・呼び出し履歴・変数はこのスレッドのもの。stop_kind = 最後の停止の種類（linux.halt.debug の kind。attach なら attach。走っていれば null）。registers_from = core（コアのレジスタ）/ pt_regs（カーネルに入ったときに保存したユーザのレジスタ）。pc_error = pc を読めなかった理由。load_bias = 実行時の番地 − リンク時の番地

```ts
{ pid: number; comm: string; tid: number | null; thread_comm: string | null; threads: Array<{ tid: number; comm: string }>; stop_kind: string | null; registers_from: "core" | "pt_regs" | null; pc_error: string | null; elf: string; load_bias: number; tid_from: "selected" | "last_stop" | "rule" | null; breakpoint_edit_requires_halt: boolean }
```

### Listing

逆アセンブル（命令は offset 番目から limit 件、pc_index はその中の PC の位置）

```ts
{ memory: "target" | "snapshot" | "elf"; sync: "function" | "mapping" | "start"; sync_skipped: SyncSkipped; total: number; offset: number; pc_index: number | null; instructions: Array<Instruction> }
```

### LogAddress

本文の中の 0x の数のうち実行区画に入る値（fprobe.logfmt.addresses.AddressHit）

```ts
{ value: number; address: number; where: string; function?: string; offset?: number; file?: string; line?: number }
```

### LogRecord

解読したログの 1 行（fprobe.logfmt.record.LogRecord.to_dict。kind = 重要度・log・undecoded。args は形式の値のまま、raw は解読できなかったバイトの 16 進）

```ts
{ format: string; kind: string; text: string; level?: string; id?: number; template?: string; timestamp?: number; timestamp_text?: string; location?: { file?: string | null; line?: number; module?: string; symbol?: string }; channel?: string; error?: string; args?: Array<Json>; addresses?: Array<LogAddress>; raw?: string; notes?: Array<string>; cut_at?: number; continued?: true }
```

### LogState

ログの受け取りの様子（log_status は一度も始めていなければ running = false だけ）

```ts
{ running: false } | { running: boolean; format: string; source: "rtt" | "swo" | "uart"; settings: { channel: number | string | null; port: number; framing: string | null; annotate: boolean; swo_baud?: number | null }; records: number; kept: number; kinds: { [key: string]: number }; bytes: number; errors: number; recorded: boolean; elapsed_s: number; warnings: Array<string>; error?: string; window?: EventWindowInfo }
```

### MeasureChannel

チャンネル 1 本の測定（周波数・周期・デューティ・high / low の幅）

```ts
{ initial: number | null; rises: number; falls: number; high_ratio: number | null; frequency_hz: number | null; duty: number | null; duty_min: number | null; duty_max: number | null; period: MeasureSeries; high: MeasureSeries; low: MeasureSeries }
```

### MeasureRelation

チャンネル間の時間差（delay: from の辺から次の to の辺まで、setup_hold: 時計の辺の前後のデータの変化。violations は setup_min_ns・hold_min_ns を渡したときだけ）

```ts
{ type: "delay"; from: number; from_edge: "rise" | "fall" | "any"; to: number; to_edge: "rise" | "fall" | "any"; count: number; min_ns?: number; max_ns?: number; mean_ns?: number; stdev_ns?: number; min_at?: number; max_at?: number; unmatched: number } | { type: "setup_hold"; clock: number; clock_edge: "rise" | "fall"; data: Array<number>; setup_min_ns: number | null; hold_min_ns: number | null; setup: MeasureSeries; hold: MeasureSeries; violation_count?: number; violations?: Array<{ at: number; setup_ns?: number; hold_ns?: number }>; violations_limit?: number; violations_truncated?: boolean }
```

### MeasureSeries

時間の列の統計（la_measure の _Series.stats。無ければ count = 0 だけ。*_at は位置）

```ts
{ count: 0 } | { count: number; min_ns: number; max_ns: number; mean_ns: number; stdev_ns: number; min_at: number; max_at: number }
```

### OptionBytes

STM32 のオプションバイトを名前で解いたもの（stm32_option_bytes.decode）

```ts
{ family: "f4" | "l4" | "g4" | "g0"; address: number; raw: number; fields: { [key: string]: number }; rdp_level: 0 | 1 | 2; descriptions: { [key: string]: string } }
```

### Output

クロストリガの動作「出力の端子」の端子の候補 1 本（index = CSR xtrig_out の ctrl.pin の番号）。

```ts
{ index: number; channel: number; pull_up: boolean }
```

### Part

判別した部品（coresight_id.Part.as_dict）

```ts
{ vendor: string; name: string; source: string; id: number; candidates: Array<string>; suggested_target: string | null; option_family?: string }
```

### ProfileCategories

サンプルの分類（code = 関数の中、sleep = コアの眠り、wfi = WFI / WFE の上、unknown = 関数の外）

```ts
{ code: { samples: number; fraction: number }; sleep: { samples: number; fraction: number }; wfi: { samples: number; fraction: number }; unknown: { samples: number; fraction: number } }
```

### ProfileExceptionRow

```ts
{ exception: number; name: string; samples: number; fraction: number }
```

### ProfileFunctionRow

```ts
{ function: string; address: number; size: number; samples: number; fraction: number; active_fraction: number }
```

### ProfileInstructionRow

```ts
{ address: number; function: string; offset: number | null; file: string | null; line: number | null; text: string | null; samples: number; fraction: number; active_fraction: number }
```

### ProfileLineRow

```ts
{ function: string; file: string | null; line: number | null; samples: number; fraction: number; active_fraction: number }
```

### ProfileRow

プロファイルの表の 1 行（by: function / line / instruction / exception）

```ts
ProfileFunctionRow | ProfileLineRow | ProfileInstructionRow | ProfileExceptionRow
```

### ProfileSummary

プロファイルの要約（itm は swo_utilization・dropped_packets・例外トレースなら exceptions、pcsr は window・sampling）

```ts
{ running: boolean; method: "itm" | "pcsr"; rate_hz: number; total: number; active: number; unavailable: number; overflows: number; categories: ProfileCategories; by: "function" | "line" | "instruction" | "exception"; notes: Array<string>; rows: Array<ProfileRow>; rows_total: number; swo_utilization?: number | null; dropped_packets?: number; exceptions?: Array<{ exception: number; name: string; count: number; total_self: number; load: number | null }>; exceptions_total?: number; window?: EventWindowInfo; sampling?: { requested_hz: number | null; expected_hz: number; achieved_hz: number | null; samples: number; swd_hz: number | null; swclk_hz: number | null; round_us: number | null; swd_hz_for_rate: number | null } }
```

### ProfileWindow

集計に使ったサンプルの区間（t0・t1 はサンプルの時刻の単位 time_unit、t0_s・t1_s は最初のサンプルからの秒）

```ts
{ samples: number; t0: number | null; t1: number | null; t0_s: number; t1_s: number | null; time_unit: "timebase" | "core_cycles" }
```

### ProgramReport

Flash の書き込みの結果（fprobe.flash.ProgramReport）

```ts
{ bytes: number; programmed_bytes: number; programmed_pages: number; skipped_bytes: number; erase_method: "sector" | "chip"; erase: { bytes: number; seconds: number; bytes_per_s: number | null; sectors: number }; program: Rate; analyze_s: number; ram_bytes: number; seconds: number; bytes_per_s: number | null }
```

### Quantiles

分位（最小・中央値・90 % 点・最大）

```ts
{ min: number; median: number; p90: number; max: number }
```

### Rate

速さ（バイト数・秒・バイト/秒。秒が 0 なら null）

```ts
{ bytes: number; seconds: number; bytes_per_s: number | null }
```

### RecordingCoverage

範囲の出来事・波形が残っているか（長期の記録の消した・間引いた分割）

```ts
{ complete: boolean; gaps: Array<{ segment: number; t0: number | null; t1: number | null; state: string; dropped: Array<string> }> }
```

### RecordingEnded

記録を終えた理由（以後の行は入らない）と事実

```ts
{ reason: "timebase_restart" | "design_switch" | "link_lost"; last_ticks?: number | null; ticks_after?: number; before?: string; after?: string }
```

### RecordingEvent

記録の出来事 1 件（fprobe.recording.Event。body は stream・kind ごとの中身）

```ts
{ t: number; stream: string; kind: string; a: number; b: number; body: Json; t_end: number | null; uncertainty: number | null }
```

### RecordingInfo

規則・証跡が当てた記録（name = recording_list の name、span = 出来事の時刻の範囲 [最初, 最後]）

```ts
{ name: string; title: string | null; board: string | null; design: string | null; elf: string | null; timebase_hz: number; span: [number, number] | null }
```

### RegisterGuard

書き込みアルゴリズムの間の守り（fprobe.flash.RegisterGuard の {enter, exit}。渡されたまま）

```ts
{ enter?: Array<{ op: "write" | "wait"; address: number; mask: number; value: number } | { op: "restore" | "wait_restored"; address: number; mask: number }>; exit?: Array<{ op: "write" | "wait"; address: number; mask: number; value: number } | { op: "restore" | "wait_restored"; address: number; mask: number }> }
```

### RelayStatus

PYNQ の中継の状態（fprobe.eth_link.decode_relay_status。古い中継は後ろの名前の欄が無い。知らない値は unknown_fields）

```ts
{ version: number; token: number; zc_state: string; tcp_path: string; connections?: number; zc_copied_total?: number; tcp_bytes?: number; tcp_calls?: number; tcp_eagain?: number; copy_ring_full?: number; ps_bad_la_positions?: number; ps_bad_ev_positions?: number; pl_stale_writes?: number; zc_calls?: number; zc_completed?: number; zc_copied_calls?: number; zc_copied_bytes?: number; zc_unknown_completions?: number; zc_enobufs?: number; zc_eagain?: number; zc_bytes?: number; zc_bounce_bytes?: number; zc_chunks?: number; zc_latency_count?: number; zc_latency_sum_ns?: number; zc_latency_max_ns?: number; zc_outstanding_calls?: number; zc_outstanding_max?: number; zc_la_held_beats?: number; zc_la_held_max_beats?: number; zc_ev_held_beats?: number; zc_ev_held_max_beats?: number; zc_desc_full?: number; zc_calls_full?: number; zc_abandoned_chunks?: number; zc_abandoned_bytes?: number; unknown_fields: { [key: string]: number } }
```

### ReturnValue

関数の戻り値（fprobe.calling.read_return_value。型の分からない関数は値にせず r0・r1 そのまま）

```ts
{ type: string; value: Value | null; raw?: number } | { type: null; r0: number; r1: number }
```

### RomComponent

ROM テーブルの部品（coresight_id.Component.as_dict）

```ts
{ address: number; name: string; class: number; part: number; designer: string; designer_code: number; revision: number; devarch?: number; error?: string; truncated?: "depth" | "limit"; walk?: { depth: number; limit: number; components: number; truncated: boolean }; children?: Array<RomComponent> }
```

### RtosHeap

RTOS のヒープの要約（fprobe.rtos.model.HeapInfo.to_dict。分からない値は null）

```ts
{ scheme: string | null; total: number | null; free: number | null; min_free: number | null; allocations: number | null; frees: number | null; free_blocks: number | null; largest_free_block: number | null; address: number | null; notes: Array<string>; source: string | null; handed_out: number | null; used: number | null; peak_used: number | null }
```

### RtosLoadSeries

窓ごとの CPU 負荷の推移（fprobe.rtos.analysis.load_series。区間が空なら windows は空で idle_tasks が無い）

```ts
{ window: number; window_us: number | null; windows: Array<{ t0: number; t1: number; covered: number; load: number | null; isr: number | null; other: number | null; tasks: { [key: string]: number } }>; peak: null | { t0: number; t1: number; load: number }; mean_load: number | null; idle_tasks?: Array<string>; windows_total?: number; limit?: number }
```

### RtosObject

カーネルのオブジェクト（fprobe.rtos.model.KernelObject.to_dict。fields は RTOS・種類ごとの中身で形は種類で変わる）

```ts
{ handle: number; kind: string; name: string; source: string; fields: { [key: string]: Json }; waiters: Array<{ task: string; handle: number; priority: number | null; reason: string; bits?: number; all?: boolean; clear_on_exit?: boolean; flags?: number; options?: number; clear?: boolean; request?: number; on_clear?: boolean }>; aliases?: Array<string>; notes?: Array<string> }
```

### RtosStack

タスクのスタック（fprobe.rtos.model.StackInfo.to_dict。番地とバイト数。分からない値は null）

```ts
{ base: number; end: number | null; pointer: number | null; untouched: number | null; fill: "intact" | "none" | "unknown"; overflow: boolean; fill_run_above: { offset: number; length: number; searched: number } | null; size: number | null; used: number | null; peak_used: number | null }
```

### RtosStats

タスクの表（fprobe.rtos.timeline.Timeline.stats）

```ts
{ method: string; resolution: number | null; lower_bound: boolean; covered: number; switches: number; gaps: number; tasks: { [key: string]: RtosTaskStats } }
```

### RtosTask

タスク 1 つ（fprobe.rtos.model.Task.to_dict。locations = 値を読んだメモリの場所）

```ts
{ handle: number; name: string; priority: number; state: "running" | "ready" | "blocked" | "suspended" | "deleted"; stack: RtosStack; base_priority?: number; number?: number; wait?: RtosWait; notes?: Array<string>; locations?: { [key: string]: { address: number; size: number; member: string } } }
```

### RtosTaskStats

タスク 1 つの統計（fprobe.rtos.timeline.Timeline.stats の tasks の 1 行。時間はタイムベース、*_us は時間軸の道具が足す µs）

```ts
{ handle: number; total: number; cpu: number; runs: number; max_run: number | null; min_run: number | null; mean_run: number | null; cut_runs: number; max_cut_run: number | null; short_runs: number; total_us?: number | null; max_run_us?: number | null; min_run_us?: number | null; mean_run_us?: number | null; max_cut_run_us?: number | null }
```

### RtosTimeline

タスクの時間軸の要約と区間（rtos.timeline.get / stop）

```ts
{ running: boolean; method: string; timebase_hz: number; t0: number; t1: number; resolution: number | null; resolution_us: number | null; lower_bound: boolean; stats: RtosStats; window: EventWindowInfo; warnings: Array<string>; runs_total: number; offset?: number; runs?: Array<{ task: string; handle: number; t0: number; t1: number; duration_us: number; uncertainty: number | null; samples?: number }>; gaps?: Array<{ t0: number; t1: number; reason: string }>; recorded?: boolean; notes?: Array<string>; exceptions?: { activations: number; inconsistencies?: number; overflows?: number }; kernel?: { events: number; lost_records: number; gaps: number }; words_samples?: number }
```

### RtosTimelineState

取っている時間軸の様子（running は常に true。sampled は address・rate_hz、itm は port・resolution・exceptions）

```ts
{ running: true; method: "sampled" | "itm"; t0: number; timebase_hz: number; source: "recording" | "event_log"; tasks_known: number; warnings: Array<string>; address?: number; rate_hz?: number; port?: number; resolution?: number | null; exceptions?: boolean; memory?: { stacks: Array<string>; heap: boolean; rate_hz: number }; mutexes?: Array<string> }
```

### RtosWait

タスクが待っているもの（fprobe.rtos.model.WaitInfo.to_dict。分からない欄は無い）

```ts
{ reason: string; object?: number; name?: string; kind?: string; holder?: string; list?: number; wake_tick?: number; remaining_ticks?: number; overdue_ticks?: number }
```

### RuleMeasured

規則の実測（規則の種類 kind で欄が決まる）

```ts
{ count: number; min_us?: number; max_us?: number; mean_us?: number; stdev_us?: number; unmatched: number; open: number } | { count: number; min_us?: number; max_us?: number; mean_us?: number; stdev_us?: number; jitter_us?: number } | { triggers: number; completed: number; failed: number } | { count: number; windows?: number; min_per_window?: number; max_per_window?: number; window_t0?: number; window_us?: number } | { count: number; min?: number; max?: number } | { transitions: number; states: number; illegal: number }
```

### RuleResult

```ts
{ id: string; kind: "interval" | "period" | "sequence" | "count" | "never" | "value" | "states"; description: string; requirement: Array<string>; severity: "error" | "warning"; verdict: "pass" | "warn" | "no_data" | "fail"; expected: { min_us?: number; max_us?: number; period_us?: number; tolerance_us?: number; within_us?: number; steps?: number; count?: number; min?: number; max?: number; window_us?: number; equals?: Json; allowed?: Array<Array<Json>>; initial?: Json }; measured: RuleMeasured; violations: Array<RuleViolation>; violations_total: number; violations_limit: number; baseline?: { measured: RuleMeasured; delta: { [key: string]: number } } }
```

### RuleViolation

```ts
{ t: number; t_end: number | null; at_us: number; kind: "too_short" | "too_long" | "no_end" | "missing_step" | "forbidden" | "count" | "occurred" | "not_equal" | "not_number" | "out_of_range" | "initial" | "illegal_transition"; message: string; value: Json }
```

### RulesCheck

```ts
{ verdict: "pass" | "warn" | "no_data" | "not_triggered" | "error" | "inject_error" | "fail"; counts: { pass: number; fail: number; warn: number; no_data: number }; ruleset: { name: string; sha256: string; source: string | null; requirements: { [key: string]: string } }; recording: RecordingInfo; window: { t0: number | null; t1: number | null }; rules: Array<RuleResult>; checked_at: string; baseline?: RecordingInfo; marked?: number }
```

### SamplerStatus

DAP サンプラの状態（fprobe.sampler.Sampler.status）

```ts
{ rounds: number; errors: number; overruns: number; last_ack: number; busy: boolean; held: boolean }
```

### SamplingPlan

PC サンプリングの計画（fprobe.profile.dwt.SamplingPlan.as_dict。itm の欄・pcsr の欄はその方法のときだけ値）

```ts
{ method: "itm" | "pcsr"; rate_hz: number; writes: Array<{ address: number; value: number; mask: number; note: string }>; warnings: Array<string>; interval_cycles: number | null; cyctap: number | null; postpreset: number | null; acpr: number | null; swo_baud: number | null; baud_error: number | null; bytes_per_s: number | null; utilization: number | null; fits: boolean; fitted: boolean; exception_rate_hz: number | null; sampler_ranges: Array<[number, number]> | null; period_s: number | null; requested_rate_hz: number | null; swd: { swd_hz: number; swclk_hz: number; transfers: number; round_us: number; max_rate_hz: number; swd_hz_for_rate: number | null } | null }
```

### ScriptCheck

接続の手順のスクリプトの検査（connect_scripts.ScriptCheck.as_dict）

```ts
{ path: string; ok: boolean; hooks: Array<{ name: string; line: number; params: Array<string>; summary: string }>; commands: Array<string>; functions: Array<{ name: string; line: number }>; problems: Array<string>; error: string | null }
```

### ScvdNode

SCVD の表示の木の 1 つ（property・value・children。alert は SCVD の alert、notes は読めない・式の誤り・上限で止めたこと）

```ts
{ property: string; value: string; children?: Array<ScvdNode>; alert?: true; notes?: Array<string> }
```

### SessionClient

つないでいる利用者（WebSocket の利用者だけ notifications_dropped・notify_limit を持つ）

```ts
{ id: number; name: string; role: "human" | "ai" | "script"; via: "api" | "gdb" | "tcp" | "hook"; automated: boolean | null; notifications_dropped?: number; notify_limit?: number }
```

### SourceList

ELF の行番号の情報が参照するソース（built_path = ビルドした場所に無く ELF の隣のファイルに置き換えたときの元の場所。same_name = 同じ名前の別のファイル）

```ts
{ files: Array<{ name: string; path: string; exists: boolean; built_path?: string; fetched?: LinuxSourceFetched; fetch_error?: LinuxSourceFetchError }>; same_name: { [key: string]: Array<string> } }
```

### StackBounds

スタックの範囲と、それぞれの出どころ

```ts
{ top: number; bottom: number; size: number; nominal_size: number | null; top_source: string; bottom_source: string; notes: Array<string> }
```

### Stop

止まった様子（reason = breakpoint / watchpoint / step / user / timeout / vector_catch / external / exit 等。elapsed_s は PC の時計。exit はセミホスティングの SYS_EXIT で exit_code 付き。故障なら fault）

```ts
{ reason: string; pc: number; where: string; function?: string; file?: string; line?: number; elapsed_s: number; time_source: "host"; halt_bits?: Array<string>; exit_code?: number; fault?: FaultSummary }
```

### StreamBuffer

PC の受け取りの貯め場の事実（fprobe.frame_link.StreamBuffer.as_dict）

```ts
{ capacity_bytes: number; used_bytes: number; peak_bytes: number; full_count: number; full_s: number; backpressure: boolean; dropped_frames: number; dropped_bytes: number; lost_frames: number }
```

### StreamLog

流れをファイルに書いている記録（fprobe.streams.StreamLogger.info）

```ts
{ stream: string; path: string; timestamps: boolean; written: number }
```

### StreamServed

TCP で公開している流れ（fprobe.streams.StreamServer.info）

```ts
{ stream: string; port: number; address: string; protocol: "raw" | "telnet"; writable: boolean; published: number; connections: number; rejected: number; errors: Array<string>; peers: Array<{ address: string; sent: number; received: number; dropped: number; queued: number }> }
```

### SvdFieldValue

```ts
{ name: string; bits: string; value: number; enum: string | null }
```

### SvdRegisterValue

読んだ周辺レジスタの値とフィールド（size はビット）

```ts
{ path: string; address: number; size: number; value: number; fields: Array<SvdFieldValue> }
```

### SwoComparator

DWT の比較器の使い方（fprobe.swo_trace.ComparatorUse.as_dict）

```ts
{ index: number; role: "data_trace" | "value_watch" | "link"; name: string; function: number; comp: number; mask: number; address: number | null; size: number; access: string; emits: Array<string>; value: number | null; action: string | null }
```

### SwoDataAccess

データトレース・値一致の 1 アクセス（t はタイムベース、time_us は窓の始めから）

```ts
{ t: number | null; time_us: number | null; name: string; role: string; comparator: number; write: boolean | null; address: number | null; size: number | null; raw: number | null; value: Value | null; complete: boolean; local_time?: number; type?: string; pc?: number; where?: string; function?: string; file?: string; line?: number }
```

### SwoTrace

swo_configure の結果（swo_trace・swo_unconfigure）

```ts
{ running: boolean; core: string; total: number; accesses: Array<SwoDataAccess>; counters: { [key: string]: number }; exceptions: { counts: { [key: string]: number }; events: number; events_dropped: number; events_limit: number }; lost: number; outside: number; unknown: number; overflows: number; decode_errors: number; dropped: number; untimed: number; comparators: Array<SwoComparator>; warnings: Array<string>; window?: EventWindowInfo }
```

### SyncSkipped

確かな境界があったのに揃えなかった理由（揃えたか境界が無ければ null）

```ts
{ from: number; kind: "function" | "mapping"; reason: "too_far" | "not_aligned" | "unreadable" | "pc_in_data"; distance: number; limit: number } | null
```

### TargetStatus

target.status の形: 状態・つないでいる先・停止中なら PC と場所と止まった理由

```ts
{ state: CoreState; target?: string; frequency_hz?: number; wire?: "swd" | "jtag"; core?: number; arch?: string; hart?: number; debug_module?: DebugModuleInfo; architecture?: "armv7-a" | "armv6"; debug_version?: number; breakpoints?: number; watchpoints?: number; core_count?: number; debug_base?: number; apb_ap?: number; discovery?: "ahb_system_map" | "rom_table" | "part_table"; jtag_chain?: Array<number>; tap?: number; pc?: number; where?: string; function?: string; file?: string; line?: number; halt_reason?: "request" | "breakpoint" | "bkpt_instruction" | "watchpoint" | "vector_catch" | "external"; halt_bits?: Array<"HALTED" | "BKPT" | "DWTTRAP" | "VCATCH" | "EXTERNAL">; debug_mode?: "monitor"; fatal?: FatalState; rescue_dp?: number; cores?: Array<CoreEntry>; linux?: LinuxTargetFacts }
```

### TestpointTree

呼び出しの文脈の木の節

```ts
{ function: string; count: number; inclusive_s: number; exclusive_s: number; cut: number; children: Array<TestpointTree>; children_omitted?: number }
```

### TimelineTile

時間軸のタイル（JSON の部分。区画はバイナリのフレームの result.sections・画面では data）

```ts
{ format: number; source: "capture" | "recording"; t0: number; t1: number; tick_s: number; extent: [number, number] | null; wave: { mode: "none"; channels: Array<number> } | { mode: "raw"; channels: Array<number>; edges: number; pieces: Array<{ t0: number; t1: number; tps: number; length: number; s0: number; s1: number; channels: Array<number>; levels: Array<number> }>; names?: Array<string> } | { mode: "summary"; channels: Array<number>; edges?: number; start: number; bin: number; bins: number; names?: Array<string> }; lanes: Array<{ stream: string; mode: "raw"; count: number; items: number; kinds: Array<string>; rows: Array<{ id: string; rule: string; type: string; title: string | null; unit?: string; jump?: string; hidden?: true }>; labels: Array<string>; carry_from: number | null; carry_complete: boolean; signals_error?: string } | { stream: string; mode: "summary"; count: number; start: number; bin: number; bins: number; rows: Array<{ id: string; rule: string; type: string; title: string | null; unit?: string; jump?: string; hidden?: true }>; signals_error?: string }>; capture?: CaptureInfo; coverage?: RecordingCoverage }
```

### TimingPoint

1 つの情報源の時刻の意味（TLV TIMING の 1 件。正は gateware/fprobe_gw/timing.py）: 時刻 = 点 + delay（unit の単位。

```ts
{ source: string; point: string; unit: string; delay: number | null; span: number | null; excludes: Array<string> }
```

### TraceEvent

トレースの出来事 1 つ（fprobe.tracefmt.model.TraceEvent.to_dict。ts = ターゲットの時計、args は形式の値のまま。trace.events は時刻の対応があれば t（タイムベース）と task_name も）

```ts
{ ts: number; kind: string; name: string; segment?: number; task?: number; args?: { [key: string]: Json }; text?: string; t?: number; task_name?: string }
```

### TraceState

トレースの取り込みの様子（出来事の種類ごとの数・タスク・失った数・時刻の区切り・時刻の対応）

```ts
{ running: boolean; source: "rtt" | "swo" | "uart" | "memory" | "file"; settings: { channel?: number | string; port?: number; send_start?: boolean; description?: string; metadata?: string; ctf_header?: string; ts_hz?: number; swo_baud?: number; period_s?: number; address?: number; string_limit?: number; snapshot?: boolean; path?: string }; bytes: number; format: string; events: number; kinds: { [key: string]: number }; tasks: Array<{ id: number; name?: string; priority?: number; stack_base?: number; stack_size?: number }>; lost: number; errors: number; notes: Array<string>; ts_wraps: number; timed: boolean; segments: number; breaks: Array<{ segment: number; reason: string; init_count?: number | null; previous_ts?: number; ts?: number; mismatch?: number; since_ts?: number | null }>; recorded_events: number; warnings: Array<string>; elapsed_s: number; ts_wrap_min_back?: number | null; assumptions?: Array<string>; ts_first?: number; ts_last?: number; duration_s?: number; ts_hz?: number; cpu_hz?: number; system?: { [key: string]: Json }; clock?: { anchors: Array<{ segment: number; anchor_ts: number; anchor_t: number; uncertainty: number | null }>; anchor_bound: "upper"; assumptions: Array<string>; unknown: Array<string>; timebase_hz: number; ts_hz: number; basis: "anchor_per_segment_declared_rate" }; unplaced_events?: number; error?: string; window?: EventWindowInfo }
```

### TraceTimeline

取り込んだトレースから作った時間軸（made = false なら reason）

```ts
{ made: false; reason: string } | { made: true; runs: number; switches: number; gaps: number; isr: number | null; recorded: boolean; timebase_hz: number | null; idle_tasks: { matched: { [key: string]: Array<number> }; unmatched: Array<string> } | null; priority_order: "higher_is_more_urgent" | "lower_is_more_urgent" | null; stats: RtosStats }
```

### UartFlowState

UART のフロー制御の設定と制御線の状態

```ts
{ auto: boolean; rts_channel: number | null; cts_channel: number | null; pins: Array<number>; cts: boolean; rts: boolean; host_rts: boolean; host_break: boolean }
```

### UiCursors

```ts
{ selection: number | null; mouse: number | null; reference: number | null }
```

### UiDashboard

操作パネルの配置（version 1、widgets の各部品は文字列の id・kind・expr を必ず持つ。部品の中身は画面が決めるので形を決めない）。null = まだ無い

```ts
Json
```

### UiEvent

```ts
{ stream: string; t: number; t_end?: number; kind?: string; text?: string; seq?: number; row?: string } | null
```

### UiPointer

窓・AI ごとのマウスのカーソル

```ts
{ key: string; t: number; source: "capture" | "recording" | null; name: string | null; role: string | null; client: number | null; time: number }
```

### UiSampling

```ts
{ expressions: Array<{ expr: string; plot: boolean; axis: "left" | "right" }>; rate_hz: number | null } | null
```

### UiState

画面の状態（ui.state.get。rev は変わるたびに 1 増える）

```ts
{ rev: number; view: UiView; cursors: UiCursors; rows: Array<string> | null; event: UiEvent; frame: number; sampling: UiSampling; dashboard: UiDashboard; fpga_signals: FpgaSignalDefs | null; system: UiSystem; pointers: Array<UiPointer>; panel: { id: string; seq: number } | null }
```

### UiSystem

```ts
{ watches: Array<{ name: string; address: number }>; conditions: Array<{ name: string; expr: string }> } | null
```

### UiView

```ts
{ source: "capture" | "recording"; t0: number; t1: number } | null
```

### UnwindStop

CFI の巻き戻しをそこで止めた理由（fprobe.dwarf.unwind.Unwinder.unwind）

```ts
{ reason: "max_frames"; max_frames: number } | { reason: "no_cfi"; pc: number } | { reason: "not_available"; detail: string } | { reason: "return_address"; value: number } | { reason: "cfa_below_sp"; cfa: number; sp: number } | { reason: "pc_zero" } | { reason: "repeated_frame"; pc: number; sp: number }
```

### Value

DWARF の型で読んだ値（fprobe.dwarf.values.decode の形。kind で種類が決まる）

```ts
{ kind: "int"; value: number } | { kind: "float"; value: number | "nan" | "inf" | "-inf" } | { kind: "bool"; value: boolean } | { kind: "bytes"; hex: string } | { kind: "enum"; name: string | null; value: number } | { kind: "pointer"; address: number; to: string; deref?: boolean; symbol?: string; string?: string | null; string_end?: "limit" | "unreadable"; string_limit?: number } | { kind: "struct"; members: { [key: string]: Value } } | { kind: "array"; element_type?: string; length: number; items: Array<Value>; string?: string } | { kind: "unavailable"; reason: string }
```

### Variable

引数・ローカル変数 1 つ（line = 宣言の行、address = メモリ上なら番地）

```ts
{ name: string; type: string; value: Value; line?: number; address?: number }
```

### VoltageReading

ターゲットの電圧の読み（aux_mcu.AuxReading.as_dict）

```ts
{ volts: number | null; over_range: boolean; live: boolean; samples: number; samples_before: number; window_s: number; errors: number }
```

### VoltageStats

電圧の統計（fprobe.voltage.stats。V。値が無ければ min・max・mean は null）

```ts
{ count: number; min: number | null; max: number | null; mean: number | null }
```

### WatchedTargetState

見張りが最後に見たターゲットの状態（target.state の通知と同じ: target.status の応答 + run = コアを動かし得る操作が終わった回数。未接続は DISCONNECTED と、USB を開き直した後なら link_lost）

```ts
{ state: "DISCONNECTED"; run: number; link_lost?: LinkLost; linux_attaching?: LinuxAttaching } | { state: CoreState; target?: string; frequency_hz?: number; wire?: "swd" | "jtag"; core?: number; arch?: string; hart?: number; debug_module?: DebugModuleInfo; architecture?: "armv7-a" | "armv6"; debug_version?: number; breakpoints?: number; watchpoints?: number; core_count?: number; debug_base?: number; apb_ap?: number; discovery?: "ahb_system_map" | "rom_table" | "part_table"; jtag_chain?: Array<number>; tap?: number; pc?: number; where?: string; function?: string; file?: string; line?: number; halt_reason?: "request" | "breakpoint" | "bkpt_instruction" | "watchpoint" | "vector_catch" | "external"; halt_bits?: Array<"HALTED" | "BKPT" | "DWTTRAP" | "VCATCH" | "EXTERNAL">; debug_mode?: "monitor"; fatal?: FatalState; rescue_dp?: number; cores?: Array<CoreEntry>; run: number; linux?: LinuxTargetFacts }
```

### WatchedVariable

```ts
{ name: string; address: string | null; size: number; type: string | null; method: "sampler" | "host" }
```

### Watchpoint

```ts
{ address: number; size: number; kind: "read" | "write" | "access" }
```

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
