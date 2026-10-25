# ホスト側モジュール地図（host/fprobe/）

`host/fprobe/` の構造。各ファイルの責務は、そのファイルの先頭の docstring を要約したもの（詳細は各ファイル）。
ここに無いファイルは `ls host/fprobe` で見える。ファイルが多いので、層ごとにまとめ、同種のものは列挙を省く。

## 1. 全体の形と依存の向き

```
 AI(MCP クライアント)        人（画面・CLI）・スクリプト
        │ stdio                    │ WebSocket / Python
  mcp_client.py (fprobe-mcp)       │
  mcp_server.py (入口の別名)        │
        │ DaemonClient(api/client.py)
        ▼                          ▼
  ┌──────────── api/daemon.py (fprobe-daemon) ────────────┐
  │ JSON-RPC 2.0 over WebSocket。spec = host/api/fprobe_api.json
  │ arbiter（機器の操作を 1 本の作業スレッドで順に）/ activity（操作の記録）
  │ method.impl の名前で  getattr(Probe, impl)(**params)  を呼ぶ
  └───────────────────────────┬────────────────────────────┘
                              ▼
                     probe.py  Probe（mixin 群を継承）
                              │ probe_*.py の XxxTools mixin
                              ▼
        解析・ターゲット操作の部品（debug / flash / rtos / profile / recording / correlate / obs_* / la / …）
                              ▼
        低レベル（session / device / control / transport / frame_link / serial_link / eth_link / events / caps / generated）
                              ▼
                     USB・UART・UDP のホストリンク → fprobe のゲートウェア
```

向きのルール（docstring に書かれているもの）:

- 公開口 → Probe → 部品 → 低レベル、の一方向。`mcp_client.py` は `Probe` を import しない
  （MCP は薄い転送のクライアントで、機器には触らない。`probe.py` の docstring）。
  `probe.py`（Probe 本体）を import するのは `api/daemon.py` と `mcp_server.py`（入口の別名）だけ。部品（`probe_*` 以外のファイル）は `probe_*` を import しない
  （`probe_names.py` は mixin でなく `mcp_client` / `daemon` / `probe_device` が使う普通のモジュール）。mixin どうしの import は一部にある
  （`probe_window`・`probe_inspect`・`probe_observe` の定数や部品を `probe_cycles` / `probe_formats` / `probe_debug` / `probe_gdb` が使う等）。
- `probe_host.py` の `ProbeHost`（`typing.Protocol`）は、mixin が頼る共通の属性（`_lock`・`session`・`target`・`dbg`・`_caps()`・`_recorder` など）を
  型で並べたもの。実行時は何もしない（継承しない）。他の `probe_*` を import しない（輪を作らない）。
- `generated.py` と `caps.py` の定数はゲートウェア側が正（`python -m fprobe_gw gen` で再生成。直接編集しない）。

## 2. 公開口

| ファイル | 責務 |
|---|---|
| `mcp_client.py` | `fprobe-mcp` 本体。MCP サーバ（stdio）。デーモンが無ければ子プロセスで起動し、`fprobe_api.json` の各メソッドを道具にして転送する。切断時の再接続（`ReconnectingClient`）、AI への通知を次の結果に添える（`Notices`、現在の対象は `elf.changed`） |
| `mcp_server.py` | `fprobe-mcp` の入口（pyproject の scripts）と、`Probe` の旧い import 先。`main`・`Probe` 等を再公開するだけ |
| `mcp_instructions.py` | MCP の案内文（`INSTRUCTIONS`）。道具の名前・引数は API の仕様が正で、ここは使い方の流れだけ |
| `api/spec.py` | `host/api/fprobe_api.json` の読み込み（`Method`・`Spec`）と引数の JSON Schema 検査 |
| `api/daemon.py` | `fprobe-daemon`。`Probe` を 1 つ握り、JSON-RPC 2.0 over WebSocket で全機能を出す（`--gdb` で同じセッションの GDB サーバも） |
| `api/client.py` | デーモンの同期クライアント（`DaemonClient`）。MCP・CLI・スクリプトが使う |
| `api/arbiter.py` | 操作の調停。機器（fprobe・pyOCD）はスレッド安全でないので、操作を 1 本の作業スレッドで順に実行する |
| `api/activity.py` | 操作の記録（1 操作 = 1 件、JSON Lines に追記、購読者へ配る） |
| `api/binary.py` | バイナリの応答（WebSocket のバイナリのフレーム。画面用で MCP の道具には出ない） |
| `api/gen.py` / `api/__main__.py` | 仕様から TypeScript の型（`host/api/generated/fprobe_api.ts`）と `docs/api.md` を生成（`python -m fprobe.api gen`） |
| `__main__.py` | `fprobe` CLI。`Device`・`LogicAnalyzer` を直接使う経路と `run` / `merge` 等 |
| `home.py` | 利用者ごとの保存先（`FPROBE_HOME`、既定 `~/.fprobe`） |

## 3. Probe と mixin（`probe.py` と `probe_*.py`）

`probe.py` の `Probe` は `class Probe(InspectTools, ObserveTools, … DeviceTools)` と mixin を継承し、`__init__` で状態を作って
各 mixin の `_init_<名前>()` を呼ぶ。`Probe` 自身は機器の開閉（`session`・`target`・`dbg`・`close`・`_caps`）だけを持つ。
mixin は単独では動かず、Probe に混ぜて使う（`self._lock` の中で機器に触る）。

| 領域 | mixin を持つファイル（クラス名は多くが `XxxTools`） |
|---|---|
| プローブ本体・構成 | `probe_device`（probe_info・csr_read/write・target_voltage）、`probe_design`（Primer の構成の切替）、`probe_system`（system_status）、`probe_events`（DAP のイベント記録） |
| ターゲット接続・低レベル | `probe_target`（target_*・registers・memory）、`probe_connect`（接続手順の上書き・ロック解除）、`probe_lowlevel`（診断・DP/AP・JTAG・ピン）、`probe_multicore`、`probe_monitor`（停止方式/モニタ方式） |
| デバッグ操作 | `probe_debug`（シンボル・ブレークポイント・実行）、`probe_execution`、`probe_breakpoints`、`probe_inspect`（止めて調べる）、`probe_gdb` / `probe_gdb_trace`、`probe_safety` |
| メモリ・Flash | `probe_flash`、`probe_flash_bp`、`probe_stack`、`probe_heap`、`probe_snapshot` |
| 観測（走らせたまま） | `probe_observe`（RTT・セミホスティング）、`probe_swo` / `probe_swo_rx`、`probe_etm`、`probe_cycles`、`probe_watch`（変数のライブ観測）、`probe_user`、`probe_testpoint`、`probe_window`（共通部品） |
| ロジアナ・クロストリガ | `probe_la`、`probe_decode`（通信の解読・ファイル入出力）、`probe_xtrig`、`probe_xtrig_out`、`probe_gpio`、`probe_uart` |
| FPGA 観測 | `probe_fpga_obs`（fpga_obs_*）、`probe_match_trigger`（fpga_obs_trigger_*・sampler_trigger_*） |
| 記録と解析 | `probe_recording`、`probe_recording_rtos`、`probe_longrec`、`probe_analysis`、`probe_rules`、`probe_profile` / `probe_profile_export`、`probe_rtos` / `probe_rtos_analysis`、`probe_timeline`、`probe_formats`（既存のログ・トレース形式の取り込み）、`probe_streams`、`probe_hooks` |
| 画面の補助 | `probe_ui_state`、`probe_terminal`、`probe_elf_files`、`probe_elf_watch` |
| その他 | `probe_linux`（組み込み Linux のアプリのデバッグ） |

## 4. 部品（Probe が使う実装。機器・記録・ELF を扱う）

| 系 | ファイル | 責務 |
|---|---|---|
| ELF・シンボル・DWARF | `symbols`、`dwarf/`、`disasm`、`callgraph`、`calling`、`stackusage`、`elfsize`、`halt_risk`、`memview`、`svd`、`packs` | 番地 ⇄ 名前、型・値・CFI、逆アセンブル、スタック・メモリ使用量、レジスタ定義 |
| デバッグ | `debug`、`stepping`、`thumb_step`、`flash`、`flash_bp`、`monitor`、`multidrop`、`raw_dap`、`jtag_chain`、`diagnose`、`coresight_id`、`fault`、`liveness`、`riscv_*`、`arm*_debug`、`arm_a_target`、`stm32_option_bytes`、`connect_scripts`、`hooks`、`semihosting`、`rtt`、`gdb/` | pyOCD の上のデバッグ操作と、pyOCD に無いコアの扱い |
| 観測・トレース | `itm`、`swo_trace`、`usertrace`、`trace/`、`tracefmt/`、`logfmt/`、`profile/`、`rtos/`、`testpoint/`、`sampler`、`watch_buffer`、`watch_trigger`、`streams` | SWO/ITM/ETM の解読、ログ形式、性能解析、RTOS の認識 |
| ロジアナ | `la`（設定・捕捉・読み出し）、`la_wave`（値の表現 Capture/Transitions/Waveform と拍の復号）、`la_vcd`（VCD 出力）、`la_measure`（測定）、`decode/`（通信の解読）、`sigrok`、`xtrig`、`gpio`、`spi`、`wiring` | `la.py` が `la_wave` / `la_vcd` を再公開する（`from .la import …` は従来どおり使える） |
| FPGA 観測 | `fpga_obs`（対象の回路の設定・操作 `FpgaObs`）、`obs_decoder`（イベント 8・9 から「fprobe の時刻の値の並び」を作り直す）、`obs_condition`（FPGA に書く一致への翻訳 `compile_match` とホストで数える `count_condition`）、`obs_value`（値の読み方と比べ方）、`fpga_signals`（信号の定義） | `fpga_obs.py` は `obs_decoder` / `obs_condition` を互換のため再公開 |
| 共通の時間軸の記録 | `recording`、`recording_long`、`recording_stream`、`recording_sql`、`recording_summary`、`collector`（デバイスの出来事を記録へ取り込む）、`timeline*`、`code_at`、`merge` / `timesync`（ボードの間の合成）、`export/` | デバイスのタイムベース上の 1 本の記録と、その読み出し・合成 |
| 解析 | `correlate`（結果の直前に多い出来事。原因の判定はしない）、`rules`、`inject`（故障注入の台本）、`heap`、`voltage`、`aux_mcu`、`aux_flash` | 記録・計測値の事実の集計 |
| プラグイン | `plugins/`（`heuristics`・`keymatrix`・`rp2350_trace`・`spiflash`・`stm32_flash_clock`・`zmk_health`） | 本体の汎用 API の上に載る、取り外せる層（特定の機器・用途のための処理、本体から外した見方） |
| 組み込み Linux | `linux/` | 組み込み Linux のアプリのデバッグ（R26） |

## 5. 低レベル（機器との通信）

| ファイル | 責務 |
|---|---|
| `session.py` | 1 つの USB（またはシリアル・Ethernet）ハンドルで fprobe 拡張（CSR・ロジアナ・UART 状態）と pyOCD（CMSIS-DAP）を同時に使う |
| `device.py` | `Device`: CSR の読み書き・ケイパビリティ・タイムベース |
| `control.py` | CONTROL チャンネル（CSR の READ / WRITE）の符号化と送受信 |
| `transport.py` | CONTROL のトランスポート（USB の pyusb + libusb-package、フレームのリンク） |
| `frame_link.py` / `serial_link.py` / `eth_link.py` | フレームのホストリンクの共通部（論理チャンネルの振り分け）と、UART（COBS）・UDP の実装 |
| `caps.py` | ケイパビリティ TLV の解析（未知の種類は読み飛ばす） |
| `events.py` | DAP トランザクションのイベント記録の復号（種類 8・9 の定数もここ）。`obs_decoder` が `events` に依る |
| `generated.py` | ゲートウェア由来の定数（CSR・イベント種類・ケイパビリティ）。自動生成 |
| `bitstreams.py` | Primer の構成（ビットストリーム）の選択と書き替え |

## 6. 新しい MCP ツール（API メソッド）を足す手順

MCP の道具は、実装のメソッドを手で登録するのでなく、**API の仕様（`host/api/fprobe_api.json`）に並んだメソッドから実行時に作られる**
（`mcp_client.build_client_server` が `spec.methods` を回し、`tool_function` が引数つきの関数にして `server.add_tool`）。

1. **実装を Probe のメソッドとして書く**。領域に合う `probe_*.py` の mixin に追加する（合う領域が無ければ新しい `probe_<名前>.py` に
   `XxxTools` クラスを作り、`probe.py` の import と `class Probe(...)` の継承に加え、状態が要るなら `_init_<名前>()` を `Probe.__init__` から呼ぶ）。
   機器に触るときは `with self._lock:` の中で `self.session` / `self.target` を使う。共通の部品の型は `probe_host.ProbeHost` に並べる。
2. **仕様 `host/api/fprobe_api.json` にメソッドを足す**。名前（`target.connect` のようにドット区切り。MCP の道具名は `.` を `_` にしたもの）、
   `kind`（`read` か `control`）、`summary`、`impl`（Probe のメソッド名）、`params`（JSON Schema）、`result`。
   必要に応じて `record`・`observes`（状態を変えない読み）・`binary`（画面用。MCP には出ない）・`caller`・`offline`・`irreversible`
   （`api/spec.py` の `Method`）。引数に `"x-mcp": false` を付けるとその引数は MCP の道具に出ない。
   `session.*` で始まるメソッドと `binary` のメソッドは MCP の道具にならない（`build_client_server`）。
3. **生成物を作り直す**: `python -m fprobe.api gen`（`host/fprobe/api/__main__.py`）が `host/api/generated/fprobe_api.ts` と `docs/api.md` を書く
   （`api/gen.py` の `write_all`）。`docs/api.md` は直接編集しない。
4. **テスト**: `host/tests/test_api_spec.py` が、(a) 仕様の `impl` が `Probe` に存在する、(b) 仕様の引数が `impl` の引数に含まれ、必須の引数は仕様でも必須、
   (c) 生成物（`write_all(check=True)`）が最新、を検査する。ここで落ちたら 2・3 を直す。取り返しのつかない操作（Flash 書き込み等）は仕様の `irreversible: true` で示し、同テストの `IRREVERSIBLE` の集合と一致させる（足したら集合も直す）。
5. AI への使い方の流れを書く必要があれば `mcp_instructions.py` の `INSTRUCTIONS`（道具の名前・引数の正は仕様）。

デーモンは受けたメソッドの `impl` 名で `getattr(backend, impl)` し、`fn(**params)` を調停の作業スレッドで実行する（`api/daemon.py` の `_call`）。
