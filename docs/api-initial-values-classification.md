# 必須の引数の最初の値の振り分け（2026-11-04）

CLAUDE.md の基準（値が間違っていたときに何が起きるか。A・B は入れる、C・D・E は入れない）で、仕様 host/api/fprobe_api.json の
必須で examples・default の無い引数 265 個の振り分け。A・B の値は、根拠（同じ名前・同じ意味の引数の値、説明に書かれた
意味）があるものだけ入れ、根拠の無いものは「要相談」にした。A・B は 2026-11-04 に利用者の確認を経て仕様の examples に足した。

画面の扱い: A・B は欄に最初から値を入れる（見えて・変えられ・明示して送る）。C・D・E は空から。C・D のうち受け付ける値が
決まっているもの（列挙・真偽・API が一覧を返すもの: 流れの名前・解読器・レジスタ等）は、選択肢として見せる（最初は空）。

前の一覧（docs/api-required-without-example.md の 269 個）との差は、つなぎ方の手続き session.* の 4 個（画面・MCP の
欄に出ないので除いた）。target.connect の target・frequency_hz は 2026-11-04 に必須＋ examples にした（A。この一覧には出ない）。

| 区分 | 件数 |
|---|---|
| A. 間違えるとすぐ失敗が見える | 6 |
| B. 道具の設定で、どの対象にも通じる | 5 |
| 要相談（区分は A・B だが、値の根拠が仕様に無い） | 13 |
| C. もっともらしい誤った結果が黙って出る | 5 |
| D. 対象・目的ごとに違う | 218 |
| E. 書き込む・壊す | 18 |

## A. 間違えるとすぐ失敗が見える → 仕様の examples に値を入れる

| 引数 | 型 | 入れる値 | 理由 |
|---|---|---|---|
| `linux.halt.debug.step` . `max_instructions` | integer | `200000` | 上限に達すれば応答に出る。同じ意味の attach の step_max_instructions の examples 200000 |
| `linux.halt.debug.step` . `timeout_s` | number | `5` | 超えれば reason=timeout。attach の step_timeout_s の examples 5 |
| `linux.halt.gdb.serve` . `port` | integer | `0` | 0 = OS が空いているポートを選ぶ（説明に明記） |
| `linux.halt.gdb.serve` . `max_step_retries` | integer | `8` | 上限を超えれば誤りが見える。attach の max_step_retries の examples 8 |
| `linux.halt.gdb.serve` . `bp_unmapped` | enum ['error', 'hw'] | `"error"` | error = 断る（失敗が見える側）。attach の bp_unmapped の examples と同じ |
| `linux.memap.connect_jtag` . `frequency_hz` | integer | `25000000` | 速すぎれば接続が失敗する。同じ線の linux.halt.open の jtag_khz の examples 25000 kHz |

## B. 道具の設定で、どの対象にも通じる → 仕様の examples に値を入れる

| 引数 | 型 | 入れる値 | 理由 |
|---|---|---|---|
| `debug.disasm.range` . `length` | integer | `64` | 表示する長さ。どの対象でも通じる（Disassembly の前後の量と同じ程度） |
| `fpga_obs.count` . `last` | integer | `2000` | 数える窓の件数。同じ名前の fpga_obs.read の last の default 2000 |
| `linux.halt.debug.memory` . `length` | integer | `64` | 読む長さ（どの対象でも通じる） |
| `target.memory.read` . `length` | integer | `64` | 読む長さ（どの対象でも通じる） |
| `target.memory.view` . `length` | integer | `256` | 表示の長さ（Memory パネルの 1 画面程度） |

## 要相談（区分は A・B だが、値の根拠が仕様に無い）

| 引数 | 型 | 入れる値 | 理由 |
|---|---|---|---|
| `jtag.sample_delay_scan` . `reads` | integer |  | 各遅れで読む回数（B だが、何回が妥当かの根拠が仕様に無い） |
| `linux.gdb.batch` . `timeout_s` | number |  | A（超えれば時間切れの誤りが見える）だが、gdb の命令の長さは中身しだいで根拠のある値が無い |
| `linux.gdb.serve` . `port` | integer |  | A（使用中なら失敗が見える）だが、仕様の minimum が 1 で 0（OS が選ぶ）を受け付けない。決まった番号の根拠が無い |
| `linux.halt.debug.run` . `wait_s` | number |  | A（時間切れは応答の reason に見える）。値は要相談（走らせて待つ間はコアは走っているので Pi のウォッチドッグの 15 秒とは別） |
| `linux.log.read` . `lines` | integer |  | B（表示する行数）。値の根拠が仕様に無い（events.read の last 200 に揃える案） |
| `linux.memap.connect` . `timeout_s` | number |  | A（超えれば誤り）。hw_server の起動の長さは環境しだいで根拠のある値が無い |
| `linux.perf.record` . `frequency_hz` | integer |  | B（標本の頻度）。慣習の 99 Hz 等は経験則なので、入れるかは要相談 |
| `linux.perf.record` . `max_samples` | integer |  | B（件数の上限）。watch.trigger の max_samples default 100000 に揃える案 |
| `linux.ring.read` . `max_records` | integer |  | B だが「止めている時間の上限になる」（halt のとき）。値は要相談 |
| `linux.ring.watch` . `interval_s` | number |  | B（読む間隔）。debug.elf.watch の interval_s default 1.0 に揃える案 |
| `linux.ring.watch` . `max_records` | integer |  | B だが「止めている時間の上限になる」（halt のとき）。値は要相談 |
| `linux.sync` . `count` | integer |  | B（同期の回数）。値の根拠が仕様に無い |
| `linux.trace.start` . `sync_interval_s` | number |  | B（同期の間隔）。値の根拠が仕様に無い |

## C. もっともらしい誤った結果が黙って出る → 入れない（選べるものは選択肢を見せる）

| 引数 | 型 | 入れる値 | 理由 |
|---|---|---|---|
| `linux.halt.gdb.serve` . `skew_limit_ns` | integer |  | 「同時」とみなす判定の閾値（判定の基準そのもの） |
| `linux.halt.gdb.serve` . `thread_id` | enum ['stack', 'contextidr'] |  | contextidr を選ぶと当たりが黙って捨てられた（backlog 1）。対象のカーネルで決まる |
| `linux.ring.open` . `cached` | boolean |  | 写像の仕方を違えると古い値を黙って読む |
| `recording.correlate` . `window_s` | number |  | 結果の直前を見る窓（解析の結論が窓で変わる） |
| `uart.configure` . `baud` | integer |  | 対象の UART の速さ。違えば化けた文字（解読の baud と同じ） |

## E. 書き込む・壊す → 入れない

| 引数 | 型 | 入れる値 | 理由 |
|---|---|---|---|
| `debug.variable.write` . `value` | integer |  | 書き込む値 |
| `debug.write_expression` . `value` | integer|number|string |  | 書き込む値 |
| `fpga_obs.register_write` . `value` | integer |  | 書き込む値 |
| `gpio.sequence` . `states` | array |  | 線を動かす |
| `gpio.set` . `drive` | integer |  | 線を動かす |
| `gpio.set` . `enable` | integer |  | 線を動かす |
| `jtag.clock` . `tms` | string |  | 線を直接動かすビット列 |
| `linux.halt.debug.break.insert` . `kind` | enum ['sw', 'hw'] |  | sw は対象のメモリ（命令）に BKPT を書き換える |
| `probe.csr.write` . `words` | array |  | 書き込む値 |
| `target.ap.write` . `value` | integer |  | 書き込む値 |
| `target.dp.write` . `value` | integer |  | 書き込む値 |
| `target.memory.write` . `address` | integer |  | 書き込む番地 |
| `target.memory.write` . `data_hex` | string |  | 書き込む中身 |
| `target.option_bytes.write` . `fields` | object |  | オプションバイトを書く（戻せないことがある） |
| `target.registers.write` . `value` | integer |  | 書き込む値 |
| `target.speed_test` . `address` | integer |  | その番地に書いて測る（RAM の番地は対象ごと） |
| `uart.write` . `data` | string |  | 対象へ送る |
| `watch.write` . `value` | anyOf |  | 書き込む値 |

## D. 対象・目的ごとに違う → 入れない

- **見る信号のグループ（構成・目的で決まる）**（1）: `fpga_obs.select.group`
- **何をきっかけにするか（目的。選択肢を見せる）**（1）: `hooks.add.event`
- **前か後か（目的。選択肢を見せる）**（1）: `hooks.add.phase`
- **動作の中身**（1）: `hooks.add.actions`
- **校正したい速さそのもの（目的）**（1）: `jtag.sample_delay_scan.frequency_hz`
- **ボード・配線ごとの校正値**（1）: `jtag.sample_delay_set.cycles`
- **使う人の決める上限**（2）: `linux.core.limit_set.soft`、`linux.core.limit_set.hard`
- **into / over（目的）**（1）: `linux.halt.debug.step.mode`
- **対象のカーネルの命令セット**（1）: `linux.halt.gdb.serve.kernel_isa`
- **uprobe / ptrace（目的・対象のカーネルで決まる）**（1）: `linux.probe.add.method`
- **対象の共有メモリの大きさ**（1）: `linux.ring.open.size`
- **切れた後に続けるか（目的）**（1）: `linux.ring.open.linger`
- **halt（コアを止める）/ memap（止めない）。目的と対象で決まる**（2）: `linux.ring.read.access`、`linux.ring.watch.access`
- **ファームウェアの出す形式**（2）: `log.decode.format`、`log.start.format`
- **書き出す形式（目的）**（4）: `profile.export.format`、`recording.export.format`、`testpoint.export.format`、`trace.snapshot.format`
- **当たったら止めるか（目的）**（1）: `sampler.trigger_set.halt`
- **当たったらロジアナを起こすか（目的）**（1）: `sampler.trigger_set.la`
- **対象の SPI の線（配線）**（1）: `spi.transfer.sck`
- **halt / monitor（目的）**（1）: `target.debug_mode.set.mode`
- **どの例外で止めるか（目的）**（1）: `target.vector_catch.catch`
- **配線のしかた**（1）: `testpoint.start.method`
- **読むファイルの形式**（1）: `trace.import.format`
- **対象の RTOS のトレースの形式**（1）: `trace.start.format`
- **順序の条件の中身（目的）**（1）: `xtrig.set_sequence.stages`

**使う人・対象・目的で決まる（パス・名前・式・番地・時刻・選ぶもの）**（引数の名前ごと）

- `target`（34）: `linux.agent.deploy`、`linux.agent.start`、`linux.agent.status`、`linux.agent.stop`、`linux.capabilities`、`linux.connect`、`linux.core.fetch`、`linux.core.limit_set`、`linux.core.pattern_set`、`linux.core.status`、`linux.disconnect`、`linux.events.read`、`linux.ftrace.disable`、`linux.ftrace.enable`、`linux.gdb.batch`、`linux.gdb.serve`、`linux.gdb.status`、`linux.gdb.stop`、`linux.log.read`、`linux.memap.connect`、`linux.memap.connect_jtag`、`linux.memap.disconnect`、`linux.perf.record`、`linux.perf.stat`、`linux.probe.add`、`linux.probe.list`、`linux.probe.remove`、`linux.ring.open`、`linux.ring.read`、`linux.ring.unwatch`、`linux.ring.watch`、`linux.sync`、`linux.trace.start`、`linux.trace.stop`
- `path`（22）: `debug.elf.load`、`hooks.load`、`hooks.save`、`la.decoder_load`、`la.export`、`la.load`、`linux.core.fetch`、`profile.export`、`recording.export`、`rules.report`、`snapshot.open`、`snapshot.restore`、`stream.log_start`、`target.flash`、`target.flash.read`、`target.flash.verify`、`target.pack.load`、`target.peripheral.read`、`target.peripheral.write`、`target.program_ram`、`testpoint.export`、`trace.import`
- `name`（12）: `debug.elf.upload`、`debug.source.read`、`debug.type_info`、`debug.variable.read`、`debug.variable.write`、`linux.probe.add`、`linux.probe.remove`、`recording.open`、`target.flash.algo.remove`、`target.flash.upload`、`target.registers.write`、`watch.series`
- `address`（10）: `debug.disasm.range`、`linux.halt.debug.memory`、`linux.ring.read`、`linux.ring.watch`、`probe.csr.read`、`probe.csr.write`、`sampler.trigger_set`、`target.memory.read`、`target.memory.view`、`xtrig.set_match`
- `location`（7）: `debug.breakpoint.enable`、`debug.breakpoint.set`、`debug.disasm.function`、`debug.symbol.info`、`debug.watchpoint.clear`、`debug.watchpoint.set`、`linux.halt.debug.break.insert`
- `expression`（5）: `debug.evaluate`、`debug.expression.check`、`debug.write_expression`、`rtos.read`、`watch.write`
- `pid`（5）: `linux.core.limit_set`、`linux.gdb.serve`、`linux.halt.debug.attach`、`linux.halt.gdb.serve`、`linux.halt.threads`
- `start`（5）: `cycles.measure`、`recording.intervals`、`recording.transactions`、`rtos.timeline.interval`、`timing.measure`
- `channel`（4）: `xtrig.output_disable`、`xtrig.output_enable`、`xtrig.output_release`、`xtrig.output_set`
- `end`（4）: `cycles.measure`、`recording.intervals`、`rtos.timeline.interval`、`timing.measure`
- `register`（4）: `target.ap.read`、`target.ap.write`、`target.dp.read`、`target.dp.write`
- `stream`（4）: `stream.close`、`stream.log_start`、`stream.log_stop`、`stream.serve`
- `condition`（3）: `fpga_obs.trigger_set`、`recording.condition.parse`、`recording.trigger_set`
- `events`（3）: `linux.ftrace.disable`、`linux.ftrace.enable`、`linux.perf.stat`
- `expressions`（3）: `linux.halt.debug.read`、`linux.halt.debug.threads`、`watch.set`
- `pattern`（3）: `linux.core.pattern_set`、`target.pack.find`、`target.pack.install`
- `tid`（3）: `linux.halt.debug.read`、`linux.halt.debug.select`、`linux.halt.debug.step`
- `addr`（2）: `fpga_obs.register_read`、`fpga_obs.register_write`
- `chain_idcode`（2）: `linux.memap.connect`、`linux.memap.connect_jtag`
- `data_base64`（2）: `debug.elf.upload`、`target.flash.upload`
- `t0`（2）: `recording.density`、`recording.waveform`
- `t1`（2）: `recording.density`、`recording.waveform`
- `task`（2）: `rtos.backtrace`、`rtos.read`
- `text`（2）: `log.annotate`、`recording.mark`
- `vcd_path`（2）: `la.raw_to_vcd`、`la.stream`
- `a_us`（1）: `watch.measure`
- `array`（1）: `watch.buffer_start`
- `b_us`（1）: `watch.measure`
- `binary`（1）: `linux.probe.add`
- `cable_serial`（1）: `linux.memap.connect`
- `commands`（1）: `linux.gdb.batch`
- `conditions`（1）: `fpga_obs.count`
- `core`（1）: `target.core.select`
- `effect`（1）: `recording.correlate`
- `elf`（1）: `linux.halt.debug.attach`
- `event`（1）: `rules.states`
- `expected_idcodes`（1）: `jtag.sample_delay_scan`
- `field`（1）: `rules.states`
- `flm`（1）: `target.flash.algo.add`
- `function`（1）: `debug.call`
- `head`（1）: `watch.buffer_start`
- `host`（1）: `linux.connect`
- `id`（1）: `linux.halt.debug.break.remove`
- `journal`（1）: `linux.halt.open`
- `local_path`（1）: `linux.core.fetch`
- `mask`（1）: `sampler.trigger_set`
- `offsets`（1）: `linux.halt.open`
- `out_dir`（1）: `testpoint.instrument`
- `paths`（1）: `scvd.load`
- `peripheral`（1）: `target.svd.registers`
- `phys`（1）: `linux.ring.open`
- `protocol`（1）: `la.decode`
- `query`（1）: `recording.find`
- `raw_path`（1）: `la.raw_to_vcd`
- `script`（1）: `target.script.check`
- `signals`（1）: `fpga_obs.signals_set`
- `source`（1）: `terminal.read`
- `source_dir`（1）: `testpoint.instrument`
- `sql`（1）: `recording.query`
- `t`（1）: `recording.code_at`
- `user`（1）: `linux.connect`
- `value`（1）: `sampler.trigger_set`
- `variable`（1）: `recording.durations`
- `x`（1）: `watch.xy`
- `xsdb`（1）: `linux.memap.connect`
- `y`（1）: `watch.xy`

## 前に足した examples の見直し（2026-11-04。利用者の一任で、同じ基準で外した）

| 引数 | 外した examples | 区分 | 理由 |
|---|---|---|---|
| `linux.halt.debug.attach` . `skew_limit_ns` | 50000000 | C | 「同時」とみなす判定の閾値 |
| `linux.halt.debug.attach` . `thread_id` | stack | C | contextidr で当たりが黙って捨てられた（backlog 1）。対象のカーネルで決まる |
| `linux.halt.debug.attach` . `kernel_isa` | arm | D | 対象のカーネルの命令セット |
| `linux.halt.debug.attach` . `breakpoint_kind` | sw | E | sw は対象のメモリ（命令）を書き換える |
| `linux.halt.debug.attach` . `max_halt_s` | 120 | E | 全コアを止めたままにする上限。ウォッチドッグのある Pi は約 15 秒でリセットされる（backlog 104） |
| `linux.halt.open` . `page_offset`・`phys_offset`・`linear_map_end` | Pi 2B の値 | D | 対象のカーネルの設定（違えば番地の読み替えが狂う） |
| `linux.halt.open` . `mmu_format`・`part` | short・bcm2836 | D | 対象のカーネル・部品で決まる |

残したもの（A・B）: linux.halt.open の backend・jtag_khz・max_tasks・max_threads・fpga_request_times、attach の max_step_retries・bp_unmapped・step_max_instructions・step_timeout_s、la.capture の rate_hz・pretrigger。

要相談の 13 個は、根拠のある値が無いので空のまま（空が安全側。値を決めたら足す）。
