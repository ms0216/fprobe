# 競合ツールの機能と fprobe の差（ギャップ分析）

- 作成日: 2026-10-17（ファイル名は 2026-10-18 の作業用）。ブランチ feat/f3-recording（b09d49c）の時点。
- 入力: 商用ツールの調査 `2026-10-18-competitors-commercial.md`（202 件）、メーカー・OSS 等の調査 `2026-10-18-competitors-vendor-oss.md`（242 件）。合わせて 444 件。
- 目的: 全 ID について fprobe が満たすか（上位互換か）を判定し、足りないものを実装の単位（バックログ）にまとめる。目標は「全ツールの上位互換」。
- 調べ方: fprobe の現状はコードで確かめた。主な根拠は次のとおり（以下、表では API 名だけを書く）。
  - API: `host/api/fprobe_api.json`（115 メソッド。`docs/api.md` と一致）、MCP の案内 `host/fprobe/mcp_server.py` の INSTRUCTIONS。
  - ホスト: `host/fprobe/`（debug.py は pyOCD の上に作られている。Flash 書き込みは pyOCD の FileProgrammer、RTT はホストが pyOCD 経由で周期的に読む）、
    `dwarf/`、`disasm.py`、`svd.py`、`memview.py`、`decode/`（uart・spi・i2c・swd）、`itm.py`（DWT のデータトレース・イベントカウンタのパケットは**解読だけ**）、
    `rtt.py`、`semihosting.py`、`recording.py`、`rtos/`（FreeRTOS・Zephyr）、`profile/`、`testpoint/`（**ライブラリと挿入の CLI だけで API・MCP には無い**）、`fault.py`。
  - ゲートウェア: `gateware/fprobe_gw/caps.py`（機能: XTRIG・TRIG_SEQ・AUX・SAMPLER）、`docs/registers.md`、`gateware/rtl/debug/dap_pkg.vhd`（CMSIS-DAP のコマンド: SWJ_Pins・JTAG_Sequence・SWO 等あり）、
    `gateware/rtl/debug/swo_receiver.vhd`（**UART = NRZ 方式だけ**）、`gateware/fprobe_gw/boards/primer20k.py`（デバッグコネクタは J12 の 8 本。ロジアナの外部入力はこの 8 本、内部 2 本）。
  - 画面: 指示では `host/gui/` だが、実体は `host/ui/`（Svelte）。使っている API は接続・書き込み・RAM 読み込み・停止/再開/1 命令/リセット・レジスタ・ブレークポイント/ウォッチポイント・ソース・呼び出し履歴・変数 1 つ・ロジアナの捕捉と波形・電圧・操作の記録と一時停止だけ。
  - 既存の比較 `2026-10-17-jlink-comparison.md`（実機確認の有無）。
- 判定の記号:
  - **◯** = 満たす・上回る（コードと API で確かめた）。
  - **△** = 一部。足りない点を書く。**△（画面なし）** = 機能は API にあるが、それを見る画面が無い。
  - **×** = 無い。
  - **対象外** = コンパイラ・IDE の編集機能・量産の書き込み機・特定メーカーのチップ専用・命令セットシミュレータ・現場（フリート）の運用など、プローブと解析の道具の役割の外。理由を書く。
  - **要ハード** = 追加の部品・配線が要る（電流計・給電スイッチ・レベル変換など。手持ちに無いもの）。
- 「足りない点」の `U..` はバックログの実装の単位（§2）、`G..` は画面の要件（§3）。
- 推測で ◯ にしていない。実機で確かめられていないもの・コードで確かめられないものは △ にして理由を書いた。

---

## 1. 判定表

### 1.1 商用ツール（202 件）

#### SEGGER

| ID | 機能 | 判定 | 根拠（fprobe の API・ファイル） | 足りない点 |
|---|---|---|---|---|
| SEG-01 | 対応ターゲット I/F（JTAG/cJTAG、SWD/SWO、ICSP、FINE、VCOM） | △ | SWD・JTAG（`target.connect` の jtag_chain、`rtl/debug/jtag_phy.vhd`）、SWO（UART 方式）、VCOM（CDC-ACM、`uart.*`） | cJTAG なし（U29）。ICSP・FINE はメーカー専用で対象外。Cortex-M のみ確認（U31〜U33） |
| SEG-02 | ターゲット I/F 速度 最大 50 MHz | △ | SWD 1 kHz〜25 MHz（全体設計 §1.2、swd-25mhz 仕様） | 50 MHz に届かない。上位機に対しては下回る（15 MHz 機は上回る） |
| SEG-03 | RAM へのダウンロード 最大 4 MB/s | △ | DAP_TransferBlock の読み出し 1.5 MB/s（目標値、jlink-comparison） | 書き込み速度は未計測。4 MB/s 機には届かない（U03 で計測） |
| SEG-04 | 速度最適化された Flash ローダ | △ | `target.flash`（pyOCD の FileProgrammer と pyOCD のアルゴリズム） | Flash 書き込み速度が未計測・未最適化（U03） |
| SEG-05 | メモリマップ QSPI NOR への書き込み | × | — | 外部 Flash のローダが無い（U03） |
| SEG-06 | 書き込みアルゴリズムの自作（DSK / Open Flash Loader） | △ | pyOCD のターゲット定義に CMSIS-Pack の FLM を足せる（`pyocd_targets.py`） | fprobe の API からアルゴリズムを選ぶ・足す口が無い（U03） |
| SEG-07 | SPI Flash の直接書き込み | × | — | U42 |
| SEG-08 | 対応電圧 1.2〜5 V・VTref の検出 | 要ハード | I/O は 3.3 V 固定。電圧は `probe.voltage`（補助 Pico の ADC）で測れる | レベル変換が要る（U39） |
| SEG-09 | ターゲットへの給電と入切 | 要ハード | — | 給電スイッチが要る（U39） |
| SEG-10 | ホスト接続（USB・Ethernet・WLAN） | ◯ | USB HS（ULPI）、Ethernet（`eth_link.py`、`rtl/eth/`）、シリアル（`serial_link.py`）。デーモンの `--lan` | WLAN は無いが Ethernet と LAN のデーモンで同じ用途を満たす |
| SEG-11 | 複数のデバッガの同時接続・マルチコア | △ | デーモンに人・AI・スクリプトが同時につながる（`api/arbiter.py`、`session.*`） | 外部の GDB・IDE とデーモンの同時利用ができない（USB を 1 つが握る、`session.py`）（U04）。マルチコアなし（U31） |
| SEG-12 | 接続スクリプト（特殊なターゲットの接続手順） | × | `target.connect` の connect_mode（halt / attach / under-reset）だけ | U30 |
| SEG-13 | プローブのモード切替（J-Link / CMSIS-DAP） | ◯ | 標準が CMSIS-DAP v2 で、拡張は同じ機器の別インターフェース（`session.py`）。切り替え自体が不要 | — |
| SEG-14 | シリアル番号・ニックネームで個体指定 | △ | `--serial` / `--port` / `--eth` で指定（`api/daemon.py`、`mcp_server.py`） | ニックネームが無い（U49） |
| SEG-15 | Unlimited Flash Breakpoints | × | ハードのブレークポイントだけ（pyOCD） | U28 |
| SEG-16 | ウォッチポイント・ベクタキャッチ | △ | `debug.watchpoint.set`（read / write / access）、停止理由に vector_catch はある（`debug.py`） | ベクタキャッチを設定する API が無い（U02） |
| SEG-17 | 実行制御・レジスタ・メモリ・停止理由 | △ | `target.halt/resume/step/reset/registers.*/memory.*`、`debug.run_until_halt` の reason | リセットの種類の選択が無い（U02）。行単位のステップは U01 |
| SEG-18 | DP / AP の直接アクセス・CP15 | × | `probe.csr.*` は fprobe 自身の CSR。ターゲットの DP/AP は API に無い | U29（CP15 は U32） |
| SEG-19 | JTAG の低レベル操作（IR/DR・チェーン長・TAP リセット） | △ | ゲートウェアは DAP_JTAG_Sequence を持つ（`dap_pkg.vhd`）ので OpenOCD 経由で可能な見込み | fprobe の API に無い。実機で未確認（U29） |
| SEG-20 | ピン単位の制御と状態の読み出し | △ | DAP_SWJ_Pins（`dap_cmd_sequence.vhd`）、電圧 `probe.voltage`、ロジアナで全ピンを観測 | API に無い（U29） |
| SEG-21 | 速度測定（R/W 速度・CPU クロック） | × | — | U29（R/W 速度）、U12（CPU クロック） |
| SEG-22 | RTT | △ | `rtt.start/read/write/status`（制御ブロックを ELF・RAM の走査で探す、上り・下り複数、`rtt.py`） | 速度が未計測。ホストが pyOCD で読む方式（U50） |
| SEG-23 | RTT の Telnet サーバ・Client・Logger | △ | 記録中は stream "rtt" に入り `recording.export` で書き出せる | TCP / Telnet で公開しない（U05） |
| SEG-24 | SWO 受信（最大 100 MHz） | △ | `swo.start/read/decode/text`、`rtl/debug/swo_receiver.vhd` | UART 方式だけ（Manchester なし）、最高速度が未計測（U51） |
| SEG-25 | HSS（変数の周期的な背景読み出し） | ◯ | `watch.set`（FPGA の DAP サンプラ、8 か所×16 語、デバイスの時刻付き。10 kHz を実機確認）、`rtl/debug/dap_sampler.vhd` | — |
| SEG-26 | VCOM（CDC-ACM・HW フロー制御） | △ | CDC-ACM の UART ブリッジ（`rtl/serial/uart_bridge.vhd`）、`uart.read/write` | HW フロー制御なし（U52） |
| SEG-27 | Monitor Mode Debugging | × | — | U37 |
| SEG-28 | オンチップトレース（ETB / MTB） | × | — | U35 |
| SEG-29 | STRACE | × | — | U35 |
| SEG-30 | 電流計測（Power Profiling） | 要ハード | — | U38 |
| SEG-31 | J-Link Commander（CLI・バッチ・終了コード） | △ | Python ライブラリ・JSON-RPC。CLI は `fprobe info / la` だけ（`fprobe/__main__.py`） | 消去・ベリファイ・保存（U03）、バッチ実行と終了コード（U07） |
| SEG-32 | GDB Server | △ | OpenOCD・pyOCD・probe-rs の GDB サーバで使える（実機試験あり、jlink-comparison） | デーモン（解析）と同時に使えない（U04） |
| SEG-33 | Remote Server（LAN・トンネル） | △ | Ethernet のホストリンク、デーモンの `--lan` とトークン | NAT 越しの中継が無い（U47） |
| SEG-34 | J-Link SDK | ◯ | Python ライブラリ `host/fprobe`、JSON-RPC API、MCP | — |
| SEG-35 | セミホスティング | ◯ | `debug.semihosting.configure/status`（出力・入力・ファイル・SYS_EXIT） | — |
| SEG-36 | サードパーティ経由で固有機能が使えない問題 | △ | 標準機能は CMSIS-DAP v2 で OpenOCD・pyOCD・probe-rs から使える | 標準ツールと fprobe の解析を同時に使えない（U04） |
| SEG-37 | ファーム・ライセンス管理・ネットワーク設定 | △ | `probe.info`（ボード・デザイン・ケイパビリティ）、ビットストリームは `fprobe_gw program` | 版・ビルド日時の申告なし（U49）。IP はビルド時固定。ライセンスは不要（対象外） |
| SEG-38 | J-Flash（量産向けの複数バンク書き込み） | 対象外 | — | 量産の書き込み機の領域 |
| SEG-39 | J-Flash Lite（簡易 GUI 書き込み） | ◯ | 画面から `target.flash` / `target.program_ram`（`host/ui/src/lib/debugger.ts` 等） | 消去・ベリファイの画面は G11 |
| SEG-40 | J-Flash SPI | × | — | U42 |
| SEG-41 | シリアル番号・パッチの書き込み | 対象外 | — | 量産の書き込み機の領域 |
| SEG-42 | Flasher（スタンドアロン書き込み器） | 対象外 | — | 量産の書き込み機（PC 無しの動作は役割外） |
| SEG-43 | ギャング書き込み | 対象外 | — | 量産 |
| SEG-44 | Authorized Flashing（回数制限） | 対象外 | — | 量産・委託生産の保護 |
| SEG-45 | Flasher Secure | 対象外 | — | 量産・委託生産の保護 |
| SEG-46 | Flasher のファイル操作・遠隔制御 | 対象外 | — | 量産の書き込み機 |
| SEG-47 | ストリーミングトレース（無制限） | × | — | ETM の受信（U34・U35）。STM32F401 は 4 ビットのトレース端子を持つので配線で可能な見込み |
| SEG-48 | トレースの速度・エンコーダ | × | — | U34・U35 |
| SEG-49 | ライブのコードカバレッジ・プロファイル | △ | `profile.start/result/coverage`（PC サンプリング。取っている最中も読める） | 命令トレースによる完全なカバレッジは無い（U35）。画面なし（G06） |
| SEG-50 | J-Trace が J-Link の機能を含む | △ | 個別の項目を参照（Flash・GDB・RTT は有り、Unlimited Flash BP は無し） | U28・U34 |
| SEG-51 | ソースレベルデバッグ（C/C++/Rust、ELF を読む） | △ | `debug.elf.load`、`debug.source.*`、DWARF（`dwarf/`）、画面のソース窓 | 式は C だけ。C++・Rust の DWARF は未確認（U01 と同時に確認）。内蔵エディタは対象外 |
| SEG-52 | 再ビルドの検出 | × | — | U46 |
| SEG-53 | RTOS 認識（タスク・メールボックス・キュー・メモリプール） | △ | `rtos.info/tasks/backtrace/read`（FreeRTOS・Zephyr。待っているキューの名前は出る） | キュー等のオブジェクトの一覧なし（U08）。画面なし（G05） |
| SEG-54 | データサンプリング・グラフ | △（画面なし） | `watch.set/values/series` | グラフの画面（G03） |
| SEG-55 | 命令トレース窓 | × | — | U35 |
| SEG-56 | Timeline（タスク切替・関数実行） | △（画面なし） | `rtos.timeline.*`、記録 `recording.*`。関数の遷移はテストポイントのライブラリ（`testpoint/analysis.py`） | 時間軸の画面（G01）。テストポイントの API（U17） |
| SEG-57 | Code Profile / Code Coverage | △（画面なし） | `profile.result`（by function / line / instruction）、`profile.coverage`（近似） | 画面（G06）。完全なカバレッジは U35・U17 |
| SEG-58 | Power Graph | 要ハード | — | U38 |
| SEG-59 | Debug Snapshots（保存・復元・オフライン） | × | 記録（`recording.*`）は保存・オフラインで開けるが、RAM・レジスタ・周辺の状態は残らない | U06 |
| SEG-60 | スクリプト・自動化 I/F・イベントハンドラ | △ | Python・JSON-RPC・MCP で全操作 | 停止・リセット等の前後に処理を挟むフックが無い（U44） |
| SEG-61 | 端末（RTT・SWO・セミホスティング） | △（画面なし） | `rtt.read`（channel semihosting も）、`swo.text` | 端末の画面（G04） |
| SEG-62 | Ozone-Sim（命令セットシミュレータ） | 対象外 | — | シミュレータはプローブの役割外 |
| SEG-63 | SystemView のイベントの時間軸（サイクル分解能・マルチコア） | △ | `rtos.timeline.*` の method itm（トレースフック `fprobe_trace.h`、ITM のローカル時刻）、`profile.exceptions` | 割り込みとタスクを 1 本に並べる画面（G01・G05）、マルチコア（U31） |
| SEG-64 | 記録モード（連続・シングルショット・ポストモーテム） | △ | 連続（itm で SWO を流す）。記録全体は `recording.*` | ターゲット RAM のリングを後で吸い出す方式が無い（U14） |
| SEG-65 | RTOS 対応（embOS・ThreadX・FreeRTOS・µC/OS・Zephyr・NuttX） | △ | FreeRTOS・Zephyr（`rtos/freertos.py`、`rtos/zephyr.py`。Zephyr は最小限） | U45 |
| SEG-66 | CPU 負荷 | △（画面なし） | `rtos.timeline.stop` のタスクごとの CPU 使用率 | 負荷の時間推移（U09）と画面（G05） |
| SEG-67 | DataPlot・ホスト整形の printf | △ | 変数は `watch.*` で時間軸に乗る | ターゲットが出す値のイベント（U16）、書式をホストで復元するログ（U15） |
| SEG-68 | ヒープ監視 | × | — | U27 |
| SEG-69 | SystemView のオーバーヘッド | △ | method sampled はターゲットの負荷 0（組み込み無し） | method itm のフックの負荷が未計測（U10 で計測） |
| SEG-70 | J-Scope（HSS / RTT で変数をグラフ） | △ | `watch.*`（8 か所）。RTT の数値は `rtt.read` の生データ | RTT の数値チャンネルの解釈（U16）、グラフ（G03）。変数の数は 8 か所まで |
| SEG-71 | RTT Viewer（仮想端末・ファイル記録・入力・アタッチ） | △ | `rtt.read/write`、記録の stream "rtt"、デーモンへのアタッチ | 仮想端末（制御コード）の解釈と端末の画面（G04） |
| SEG-72 | Embedded Studio のデバッガ | △ | デバッガ機能は各項目のとおり（RTOS・プロファイルあり） | 静的スタック・メモリ使用量の解析（U26）。IDE・emSim は対象外 |

#### IAR

| ID | 機能 | 判定 | 根拠（fprobe の API・ファイル） | 足りない点 |
|---|---|---|---|---|
| IAR-01 | I-jet の仕様（48 MHz・USB HS・SWO 60 Mbps・1.65〜5.5 V） | △ | USB HS、SWD 25 MHz、SWO は UART 方式 | 48 MHz に届かない、Manchester なし（U51）、電圧は要ハード（U39） |
| IAR-02 | ターゲット給電（最大 420 mA） | 要ハード | — | U39 |
| IAR-03 | 電流計測（160 µA、200 ksps） | 要ハード | — | U38 |
| IAR-04 | I-scope（任意点の電流・電圧） | 要ハード | 電圧だけ `probe.voltage`（1 ms ごと） | U38・U40 |
| IAR-05 | Power Log Setup（しきい値・電力で停止） | 要ハード | クロストリガで「条件で停止」する仕組みはある（`xtrig.*`） | 電流の入力（U38） |
| IAR-06 | 電力とソースの対応付け | 要ハード | 共通の時間軸（`recording.*`）はある | U38 |
| IAR-07 | Timeline 窓（割り込み・データ・イベント・電力を共通時間軸） | △（画面なし） | 記録が 1 本の時間軸に DAP・UART・SWO・RTT・電圧・サンプル・RTOS を持つ（`recording.events`） | 画面（G01）。電力は U38 |
| IAR-08 | Interrupt Log / Summary | △（画面なし） | `profile.exceptions`（回数・時間・最大の回の時刻）、`swo.decode` の例外トレース | 画面（G06） |
| IAR-09 | Data Log（変数アクセスを SWO で記録） | △ | `itm.py` は DWT のデータトレースのパケットを解読できる | DWT のコンパレータを設定する API が無い（U11） |
| IAR-10 | Event Log / Summary | △ | ITM の printf は `swo.text` | アプリのイベントの記録と集計（U16） |
| IAR-11 | SWO Trace 窓 | △（画面なし） | `swo.decode`（パケット一覧） | 画面（G04） |
| IAR-12 | Live Watch | △（画面なし） | `watch.set/values` | 画面（G03） |
| IAR-13 | ETM トレース・Function Trace | × | — | U34・U35 |
| IAR-14 | Function Profiler（区間に限定した集計） | △ | `profile.result`（by function） | 時間軸で選んだ区間に限る集計が無い（U18） |
| IAR-15 | Performance Monitoring（PMU・サイクル） | × | — | U12 |
| IAR-16 | Code Coverage 窓 | △ | `profile.coverage`（近似）。テストポイントの C0/C1（ライブラリ、実機確認） | 正確なカバレッジの API（U17）と画面（G06） |
| IAR-17 | コードブレークポイント | ◯ | `debug.breakpoint.set`（関数名・ファイル:行・番地、条件・無視回数） | — |
| IAR-18 | ログブレークポイント | ◯ | `debug.breakpoint.set` の log / message → `debug.breakpoint.log` | 当たるたびに数 ms 止まる点は同等 |
| IAR-19 | Trace Start / Stop トリガ | × | — | 命令トレースが前提（U35） |
| IAR-20 | データブレークポイント | ◯ | `debug.watchpoint.set` | — |
| IAR-21 | データログブレークポイント | △ | `watch.*` は値の推移を止めずに取れる（周期的） | アクセスごとの記録（DWT データトレース）が無い（U11） |
| IAR-22 | イミディエイトブレークポイント（シミュレータ） | 対象外 | — | シミュレータの機能 |
| IAR-23 | Flash ブレークポイント | × | — | U28 |
| IAR-24 | ベクタキャッチ・Breakpoint Usage | △ | `debug.breakpoint.list` | ベクタキャッチの設定（U02） |
| IAR-25 | 実行制御の細かな設定（ステップ中の割り込み禁止等） | △ | 切断・接続の方法（connect_mode） | ステップ中の割り込みマスク・リセット種別（U01・U02） |
| IAR-26 | Fault exception viewer | △（画面なし） | `target.fault`（CFSR・HFSR の各ビットの意味、MMFAR・BFAR、故障した行、その時のレジスタ）、`debug.run_until_halt` の要約 | 停止時に自動で開く画面（G08） |
| IAR-27 | スタック使用量の監視 | △ | RTOS のタスクは `rtos.tasks`（いま・最大・あふれ、`rtos/stackcheck.py`） | ベアメタル（MSP）のスタック（U26） |
| IAR-28 | 静的スタック使用量解析 | × | — | U26（コンパイラの .su・コールグラフの取り込み） |
| IAR-29 | C-RUN（実行時エラー検査） | 対象外 | — | コンパイラによる計装の機能 |
| IAR-30 | C-RUN の現地利用 | 対象外 | — | 同上 |
| IAR-31 | cspybat（バッチ実行・終了状態） | △ | Python・API で書ける | 書き込み→実行→終了コードの一発のコマンド（U07） |
| IAR-32 | C-SPY マクロ | △ | Python で全操作。ブレークポイントの条件・ログ | 停止時に任意の処理を走らせるフック（U44）。周辺の模擬は対象外 |
| IAR-33 | RTOS 認識プラグイン | △ | `rtos.tasks`（待っているもの付き） | キュー・セマフォ・メールボックスの一覧（U08） |
| IAR-34 | マルチコアデバッグ | × | — | U31 |
| IAR-35 | Session Overview / EmuDiag | △ | `session.status`、`probe.info` | 接続の診断（U29） |
| IAR-36 | 命令セットシミュレータ | 対象外 | — | シミュレータ |
| IAR-37 | 他社プローブ対応 | 対象外 | — | IDE 側の機能。fprobe は CMSIS-DAP v2 として IDE から使われる側（IAR での動作は未確認） |

#### Arm（Keil MDK・Development Studio）

| ID | 機能 | 判定 | 根拠（fprobe の API・ファイル） | 足りない点 |
|---|---|---|---|---|
| KEIL-01 | Event Recorder | × | — | U14（RAM のバッファを読んで解読） |
| KEIL-02 | Event Statistics（区間の時間・回数） | △ | `timing.measure`・`recording.intervals`（記録の 2 条件の区間）、テストポイントの区間（ライブラリ） | ターゲットの EventStart/Stop の取り込み（U16）。電流は U38 |
| KEIL-03 | Component Viewer（SCVD） | × | — | U14（SCVD の解釈） |
| KEIL-04 | System Analyzer（イベント・変数・電力を共通時間軸） | △（画面なし） | `recording.*` に各 stream が 1 本の時間軸で入る | 画面（G01）。電力は U38 |
| KEIL-05 | Logic Analyzer（SWV で 4 変数） | △（画面なし） | `watch.*`（8 か所、ウォッチポイントと排他にならない） | グラフ（G03） |
| KEIL-06 | printf Viewer・Trace Exceptions・Event Counters | △ | `swo.text`、`swo.decode`（例外トレース・EventCounter のパケットは解読） | イベントカウンタを有効にする API（U11・U12） |
| KEIL-07 | ウォッチ・メモリの実時間更新 | △（画面なし） | `watch.*`、`target.memory.view`（compare で差に印） | 画面（G02・G03） |
| KEIL-08 | ETM 命令トレース（ULINKpro） | × | — | U34・U35 |
| KEIL-09 | トレースポイント（TraceRun 等） | × | — | U35 |
| KEIL-10 | Code Coverage（ETM、分岐の成立） | △ | 近似（`profile.coverage`）、テストポイントの C1（ライブラリ） | U35・U17・U18（gcov 出力） |
| KEIL-11 | Performance Analyzer / Execution Profiler | △ | `profile.result`（関数・行の割合） | 呼び出し回数・行ごとの実行時間はトレースかテストポイントが要る（U17・U35） |
| KEIL-12 | ULINKplus の電力計測 | 要ハード | — | U38 |
| KEIL-13 | イベント単位のエネルギー集計 | 要ハード | — | U38 |
| KEIL-14 | 試験用 I/O（デジタル・アナログ）・絶縁 | △ | 汎用 I/O 8 本の CSR（`gpio` ブロック、`probe.csr.write` からだけ）、アナログ入力は補助 Pico | GPIO の API（U41）、アナログ入力（U40）。アナログ出力・絶縁は要ハード |
| KEIL-15 | デバッグスクリプト | ◯ | Python・JSON-RPC で全操作 | 周辺の模擬は対象外 |
| KEIL-16 | シミュレータ / FVP | 対象外 | — | シミュレータ |
| KEIL-17 | Keil Studio（VS Code、CMSIS-DAP） | △ | CMSIS-DAP v2 準拠 | Keil Studio での動作が未確認。デーモンと同時に使えない（U04） |
| KEIL-18 | CMSIS-Pack によるデバイス対応 | △ | pyOCD のターゲット名で接続（`pyocd_targets.py`）、SVD は同梱・pyOCD から探す（`target.svd.load`） | Pack の導入・選択を API から行えない（U03） |
| ADS-01 | Arm DS デバッガ（Cortex-M/R/A・異種マルチコア） | × | Cortex-M のみ | U31・U32（Armv9・GPU は対象外） |
| ADS-02 | OS 認識（Linux カーネル） | × | — | U32（Pi 2 の Linux 認識は後段） |
| ADS-03 | DSTREAM（パラレルトレース） | × | — | U34 |
| ADS-04 | Streamline（PMU・注釈・Call Paths） | △ | Cortex-M の ITM による PC サンプリング（`profile.*`） | PMU（U12）、注釈（U16）、呼び出し経路（U18） |
| ADS-05 | Arm Energy Probe 連携 | 要ハード | — | U38 |
| ADS-06 | Fast Models / FVP | 対象外 | — | 仮想プラットフォーム |
| ADS-07 | スクリプト（DS・Jython） | ◯ | Python・JSON-RPC | — |

#### Lauterbach TRACE32

| ID | 機能 | 判定 | 根拠（fprobe の API・ファイル） | 足りない点 |
|---|---|---|---|---|
| T32-01 | PowerTrace（オフチップトレース記録器） | × | DDR3 128 MB の記録器はあるが命令トレースを受けない | U34 |
| T32-02 | オンチップトレース（ETB 等） | × | — | U35 |
| T32-03 | CombiProbe（デバッグ＋4 ビットトレース） | × | — | U34 |
| T32-04 | トレースリスト・検索 | △ | 出来事の一覧と検索 `recording.events/find` | 命令トレースが無い（U35）。画面（G07） |
| T32-05 | グラフィカルチャート（関数・タスク・変数の推移） | △（画面なし） | `rtos.timeline.get`、`watch.series`、テストポイントの関数の遷移（ライブラリ） | 画面（G01）。テストポイントの API（U17） |
| T32-06 | 関数の実行時間・統計・関数ツリー | △ | テストポイントの `FunctionStats`・`CallTreeNode`（ライブラリ） | API・MCP に無い（U17）。サンプリングの木（U18） |
| T32-07 | トレースの保存・読み込み | ◯ | `recording.list/open/export` | — |
| T32-08 | CTS（状態の再構成・逆ステップ） | × | — | U36 |
| T32-09 | キャッシュ解析・Flash アクセス解析 | × | — | U36（命令トレースの後段。優先度は低い） |
| T32-10 | トレースによるコードカバレッジ（MC/DC） | △ | テストポイントの関数・C0・C1（ライブラリ、実機確認） | MC/DC なし、API なし（U17）。ツール認定キットは対象外 |
| T32-11 | PowerIntegrator（ロジアナ拡張、プログラムトレースと同期） | △ | ロジアナ 100 MS/s・DDR3 128 MB・64 ビットのタイムベース（10 ns）・多段トリガ、DAP の出来事と同じ時計 | 外部入力は 8 本（U22）。プログラムトレースは U34 |
| T32-12 | ロジアナプローブ（デジタル・アナログ） | △ | デジタルはロジアナ | アナログは 1 ms ごとの電圧だけ（U40） |
| T32-13 | PowerProbe（64 ch・400 MHz） | △ | 100 MS/s・8 本 | チャンネル数・速度（U22） |
| T32-14 | エネルギープロファイリング | 要ハード | — | U38 |
| T32-15 | ソフトウェアのみのトレース（LOGGER / FDX） | ◯ | RTT（`rtt.*`）、テストポイントの RTT 出力の解読（`testpoint/decode.py`） | — |
| T32-16 | SNOOPer（サンプリング） | ◯ | `profile.start` method pcsr（SWO 不要）、`watch.*` | — |
| T32-17 | PRACTICE（スクリプト） | ◯ | Python・JSON-RPC | — |
| T32-18 | Remote API / pyrcl | ◯ | JSON-RPC over WebSocket、Python クライアント（`api/client.py`）、MCP | — |
| T32-19 | 他 IDE との連携（TCF） | 対象外 | — | Wind River の IDE 固有 |
| T32-20 | OS 認識（Linux） | × | — | U32 |
| T32-21 | マルチコア（SMP / AMP） | × | — | U31 |
| T32-22 | 状態の問い合わせ関数 | ◯ | `probe.info`、`session.status`、`recording.status`、`watch.status` 等 | — |
| T32-23 | PowerView（共通の GUI） | △ | 画面（`host/ui`）と同じ API | 画面の厚み（§3 全体） |

#### TASKING / iSYSTEM

| ID | 機能 | 判定 | 根拠（fprobe の API・ファイル） | 足りない点 |
|---|---|---|---|---|
| WIN-01 | Analyzer（プログラム・データ・計装・補助 I/O のトレース） | △ | 計装（テストポイント・ITM）、補助 I/O（ロジアナ）、データ（`watch.*`） | プログラムトレース（U34・U35） |
| WIN-02 | Profiler Timeline / Statistics | △（画面なし） | タスク `rtos.timeline.*`、割り込み `profile.exceptions`、関数はテストポイント（ライブラリ） | 画面（G01・G06）、テストポイントの API（U17） |
| WIN-03 | Profiler Inspectors（派生イベント・制約違反） | × | — | U25 |
| WIN-04 | Coverage（未実行・デッドコード） | △ | `profile.coverage`（近似）、テストポイントの C0/C1 | U17・U18 |
| WIN-05 | RTOS プロファイリング | ◯ | `rtos.timeline.*`（タスクごとの合計・使用率・回数・最長/最短） | 画面は G05 |
| WIN-06 | ADIO・CAN/LIN とトレースの同期 | △ | デジタルはロジアナ（同じ時計） | アナログ（U40）、CAN/LIN の解読（U20） |
| WIN-07 | isystem.connect SDK | ◯ | Python・JSON-RPC | — |
| WIN-08 | testIDEA（ターゲット上の関数単位のテスト） | × | — | 関数の呼び出し（U53）、CI 実行（U07） |
| WIN-09 | 電力計測（未確認） | 要ハード | — | U38 |

#### PEmicro / Green Hills / Wind River

| ID | 機能 | 判定 | 根拠（fprobe の API・ファイル） | 足りない点 |
|---|---|---|---|---|
| PEM-01 | Cyclone（スタンドアロン量産書き込み） | 対象外 | — | 量産 |
| PEM-02 | ProCryption（暗号化・回数制限） | 対象外 | — | 量産・委託生産の保護 |
| PEM-03 | シリアライズ・動的データ | 対象外 | — | 量産 |
| PEM-04 | Multilink FX（多アーキテクチャ・1.6〜5.25 V・セミホスティング・SWO・式表示） | △ | セミホスティング・SWO・`watch.*` | 電圧範囲は要ハード（U39）。NXP 専用アーキテクチャは対象外 |
| GHS-01 | TimeMachine（逆実行・ウォッチポイントで逆に戻る） | × | — | U36 |
| GHS-02 | SuperTrace（大容量トレース） | × | — | U34 |
| GHS-03 | PathAnalyzer（呼び出しの系列を時間軸） | △ | テストポイントの関数の遷移（`render_calls`、ライブラリ） | API（U17）・画面（G01） |
| WR-01 | Simics: チェックポイント | 対象外 | — | 仮想プラットフォームの機能（実機の状態保存は U06） |
| WR-02 | Simics: システム全体の逆実行 | 対象外 | — | 仮想プラットフォーム（実機は U36） |
| WR-03 | Simics: 故障注入 | △ | 実機で、クロストリガの割り込み書き込み（`xtrig.set_inject`）で条件成立時に任意の番地へ書ける | 手順化（何をいつ壊すかの台本）が無い（U25） |
| WR-04 | Workbench と TRACE32 の連携（TCF） | 対象外 | — | IDE 固有 |

#### Percepio Tracealyzer

| ID | 機能 | 判定 | 根拠（fprobe の API・ファイル） | 足りない点 |
|---|---|---|---|---|
| TZ-01 | Trace View（スレッド・割り込み・API 呼び出し・ユーザイベント） | △ | `rtos.timeline.*`、`profile.exceptions` | カーネル API 呼び出しの記録（U10）、ユーザイベント（U16）、画面（G01・G05） |
| TZ-02 | CPU Load Graph | △ | `rtos.timeline.stop` の使用率（全体の合計） | 時間推移（U09）、グラフ（G05） |
| TZ-03 | Event Log・検索 | △（画面なし） | `recording.events/find`（stream・kind・時刻・値・本文の正規表現） | 画面（G07） |
| TZ-04 | Communication Flow | × | — | U10 |
| TZ-05 | Data Plot（ユーザイベントの値） | △ | 変数は `watch.series` | ユーザイベント（U16）、グラフ（G03） |
| TZ-06 | Heap / Stack Memory Usage | △ | タスクのスタック（いま・最大）`rtos.tasks` | 推移（U09）、ヒープ（U27） |
| TZ-07 | ジョブ単位の時間指標（実行・応答・周期・最悪値） | × | 区間の材料は `rtos.timeline.get` にある | U09 |
| TZ-08 | Interval・State Machine・Runnable | △ | 2 イベント間 `timing.measure`（統計・分布・外れ値） | 状態遷移図（U25）、横取りを除いた区間（U09） |
| TZ-09 | 高度な RTOS 解析（ブロック時間・優先度変化・キュー使用率） | × | — | U10・U09 |
| TZ-10 | ストリーミング / スナップショット | △ | ITM でのストリーミング（`rtos.timeline.start` method itm） | RTT でのストリーミング・RAM リングのスナップショット（U14） |
| TZ-11 | Compact Logging（ELF の文字列） | × | — | U15 |
| TZ-12 | マルチコア・巨大トレースのプレビュー | △ | 縮図 `recording.density`、範囲の読み出し `recording.events` | 巨大記録の索引・分割（U23）、マルチコア（U31） |
| TZ-13 | 出力（text / XML / CSV）・SDK | ◯ | `recording.export`（vcd / csv / json）、独自 RTOS は `rtos/base.py` の KernelAwareness を継承 | — |
| TZ-14 | 対応 OS（FreeRTOS・Zephyr・ThreadX・Linux 等） | △ | FreeRTOS・Zephyr | U45（Linux は U32） |
| TZ-15 | DevAlert / Detect（現場の異常をクラウドへ） | 対象外 | — | 現場（フリート）の運用 |

#### Memfault

| ID | 機能 | 判定 | 根拠（fprobe の API・ファイル） | 足りない点 |
|---|---|---|---|---|
| MF-01 | コアダンプ収集（現場→クラウド） | 対象外 | — | 現場の運用。机上のコアダンプは U06 |
| MF-02 | サーバ側のシンボル解決 | 対象外 | — | フリート運用（fprobe は ELF でホスト側で解決している） |
| MF-03 | Issue への自動グルーピングと通知 | 対象外 | — | クラウドサービス |
| MF-04 | リブート理由の追跡 | 対象外 | — | 現場の運用 |
| MF-05 | メトリクス（ハートビート） | 対象外 | — | 現場の運用 |
| MF-06 | トレースイベント | 対象外 | — | 現場の運用 |
| MF-07 | Compact Logs | × | — | ホスト側の解読は U15 |
| MF-08 | Custom Data Recording | 対象外 | — | 現場の運用 |
| MF-09 | 任意の転送路（チャンク） | 対象外 | — | ターゲット側 SDK |
| MF-10 | Linux 版（memfaultd） | 対象外 | — | Linux のプロセス監視 |

### 1.2 半導体メーカーの開発環境・OSS・RTOS 解析・ロジアナソフト・国産ツール（242 件）

#### STMicroelectronics

| ID | 機能 | 判定 | 根拠（fprobe の API・ファイル） | 足りない点 |
|---|---|---|---|---|
| CUBE-01 | 複数の GDB サーバ | ◯ | OpenOCD・pyOCD の GDB サーバで使える（実機試験あり） | デーモンと同時は U04 |
| CUBE-02 | コアレジスタ・メモリ・SFR の表示と書き込み | △（画面なし） | `target.registers.*`、`target.memory.view`、`target.peripheral.read/write`（SVD、読むと変わるものは既定で読まない） | 画面はレジスタだけ。メモリ・周辺（G02） |
| CUBE-03 | Live Expressions | △（画面なし） | `watch.set`（C の式・大域。ポインタはたどらない） | 画面（G03）、ポインタをたどる式（U43） |
| CUBE-04 | SWV の各ビュー（ITM・例外・データトレース・グラフ・統計的プロファイル） | △ | `swo.text`、`swo.decode`、`profile.*`（method itm） | データトレースの設定（U11）、グラフ（G03・G06） |
| CUBE-05 | Fault Analyzer | △（画面なし） | `target.fault` | 画面（G08） |
| CUBE-06 | Build Analyzer（メモリ使用量・シンボルのサイズ） | × | ELF のシンボルは読んでいる（`symbols.py`） | U26 |
| CUBE-07 | Static Stack Analyzer | × | — | U26 |
| CUBE-08 | FreeRTOS / ThreadX のカーネルオブジェクト | △ | FreeRTOS のタスク（`rtos.tasks`）、タスクごとの呼び出し履歴 | キュー・セマフォ・タイマの一覧（U08）、ThreadX（U45） |
| CUBE-09 | マルチコアのデバッグ | × | — | U31 |
| CUBE-10 | VS Code 版 | 対象外 | — | IDE |
| CUBE-11 | 実行中の RAM 変数の読み書き | △ | 読みは `watch.*`。書きは `debug.write_expression` / `target.memory.write` | 走行中の書き込みを実機で確認していない（U43 で確認） |
| CUBE-12 | Direct / Snapshot の 2 方式 | △ | Direct は `watch.*`（FPGA の周期読み） | ターゲット側バッファの吸い出し方式（U43） |
| CUBE-13 | トリガ（条件で取得開始） | △ | 記録の後から `recording.find` で条件の時刻を探せる | 変数の観測そのものを条件で始める・止める（U43） |
| CUBE-14 | Node-RED のフロー | 対象外 | — | ノーコード環境そのもの。API・MCP で同等の自動化は可能 |
| CUBE-15 | ダッシュボード・遠隔表示 | △ | 画面は Web（`--lan` で遠隔から見られる） | ゲージ・ボタン等の部品（G10） |
| CUBE-16 | 記録と再生 | ◯ | `recording.start/stop/list/open/export` | — |
| CUBE-17 | STM32CubeMonitor-Power | 要ハード | — | U38 |
| CUBE-18 | 用途別の派生版（Power・RF・USB-PD） | 対象外 | — | ST の部品専用 |
| CUBE-19 | 多様な接続（DFU・UART・SPI・CAN・I2C のブートローダ） | △ | SWD・JTAG。UART はブリッジ（`uart.*`）で外部のブートローダ書き込みツールが使える見込み | 実機で未確認。DFU・CAN 等はメーカー専用で対象外 |
| CUBE-20 | オプションバイト編集（RDP 等） | × | 周辺レジスタとしては `target.peripheral.*` で読める | 書き換え・保護の解除の手順（U30） |
| CUBE-21 | 外部フラッシュ用ローダ | × | — | U03 |
| CUBE-22 | CLI と API | △ | Python・JSON-RPC・MCP | CLI が `info / la` だけ（U07） |
| CUBE-23 | セキュアな書き込み（SFI/SSP） | 対象外 | — | 量産・ST 専用 |
| CUBE-24 | SWV printf の簡易ビュー | △（画面なし） | `swo.text` | 画面（G04） |

#### NXP

| ID | 機能 | 判定 | 根拠（fprobe の API・ファイル） | 足りない点 |
|---|---|---|---|---|
| MCUX-01 | LinkServer・J-Link・PEmicro の 3 系統 | 対象外 | — | IDE 側のプローブ選択。fprobe は CMSIS-DAP として使われる側（MCUXpresso での動作は未確認） |
| MCUX-02 | SWO Profile（PC サンプリング） | △（画面なし） | `profile.start`（itm）→ `profile.result` | 画面（G06） |
| MCUX-03 | SWO Interrupt Trace | △（画面なし） | `profile.exceptions`、`swo.decode` | 画面（G01・G06） |
| MCUX-04 | SWO Data Watch | △ | DWT データトレースの解読はある（`itm.py`） | 設定の API（U11） |
| MCUX-05 | SWO ITM Console | △（画面なし） | `swo.text` | 画面（G04） |
| MCUX-06 | 実行中のトレース開始・停止 | △ | `swo.start/stop` はプローブ側だけで止めない。`profile.start` は DWT・ITM をメモリ書き込みで設定 | 走行中の設定で止まらないことを実機で未確認（U11 で確認） |
| MCUX-07 | Energy Measurement | 要ハード | — | U38 |
| MCUX-08 | Power Profile（電力とコード） | 要ハード | — | U38 |
| MCUX-09 | アナログ信号モニタ | △ | 補助 Pico の ADC（電圧、1 ms ごと、記録の stream "aux"） | 多チャンネル・高い率（U40） |
| MCUX-10 | プローブのブリッジ機能（VCOM・I2C/SPI・給電） | △ | VCOM（CDC-ACM） | I2C/SPI ブリッジ（U41）、給電・電圧範囲は要ハード（U39） |
| MCUX-11 | LinkServer のスクリプト（接続・回復手順） | × | — | U30 |
| MCUX-12 | 実行中のグローバル変数の表示・グラフ | △（画面なし） | `watch.*` | 画面（G03） |
| MCUX-13 | FreeRTOS 等のカーネル認識（TAD） | △ | `rtos.tasks`（タスク） | キュー・タイマ（U08） |
| MCUX-14 | MCUXpresso for VS Code | 対象外 | — | IDE |

#### Silicon Labs / Renesas / TI / Microchip / Nordic

| ID | 機能 | 判定 | 根拠（fprobe の API・ファイル） | 足りない点 |
|---|---|---|---|---|
| SLS-01 | Energy Profiler（AEM） | 要ハード | — | U38 |
| SLS-02 | 電流と PC の対応づけ | 要ハード | PC サンプリング（`profile.*`）と共通の時計はある | 電流の入力（U38） |
| SLS-03 | 無線の送受信との対応づけ（PTI） | 対象外 | — | Silicon Labs の無線チップ専用 |
| SLS-04 | 複数ノードの同時測定 | 要ハード | — | 電流計が複数要る。複数台の fprobe の時計合わせも無い（U38） |
| SLS-05 | Network Analyzer（PTI） | 対象外 | — | 無線チップ専用 |
| SLS-06 | 複数ノードの統合キャプチャ | 対象外 | — | 無線チップ専用 |
| SLS-07 | Energy Profiler と Network Analyzer の連携 | 対象外 | — | 無線チップ専用 |
| E2S-01 | 実行中のメモリ表示 | △（画面なし） | `target.memory.view`（走行中も AHB-AP で読む） | 画面（G02） |
| E2S-02 | リアルタイムトレース（ルネサスのエミュレータ） | 対象外 | — | ルネサスのエミュレータ専用（Cortex-M の命令トレースは U35） |
| E2S-03 | ハードウェアイベントによるブレーク | 対象外 | — | ルネサス MCU 専用（同等の番地・データ条件は `debug.watchpoint.set`） |
| E2S-04 | Performance Analysis（区間のサイクル） | × | — | U12 |
| E2S-05 | RTOS Resources（タスク・キュー・スタックの実時間表示） | △ | `rtos.tasks`（止めて読む） | オブジェクト一覧と走行中の読み（U08） |
| E2S-06 | シミュレータのカバレッジ | 対象外 | — | シミュレータ |
| E2S-07 | リアルタイムチャート（未確認） | △（画面なし） | `watch.series` | 画面（G03） |
| CCS-01 | Core Trace（MTB・ERAD・ETB） | × | — | U35（ERAD・C66x 等は TI 専用で対象外） |
| CCS-02 | Custom Core Trace（範囲・開始停止） | × | — | U35 |
| CCS-03 | EnergyTrace | 要ハード | — | U38 |
| CCS-04 | EnergyTrace++（電力と CPU 状態） | 対象外 | — | MSP430・CC13xx/26xx 専用 |
| CCS-05 | トレース機能の排他 | ◯ | ロジアナ・SWO・イベント記録・サンプラ・RTT は同時に使える（サンプラは 1 つ: MCP の案内） | — |
| CCS-06 | ROV（RTOS のオブジェクトを走行中に読む） | △ | `rtos.tasks` は止めて読む | 走行中の読み・オブジェクト（U08） |
| CCS-07 | 実行中のメモリ・レジスタアクセス | ◯ | 走行中のメモリ・周辺の読み（`target.memory.*`、`target.peripheral.read`、`watch.*`） | コアレジスタは Cortex-M では停止が必要（どのプローブも同じ） |
| CCS-08 | 配列のグラフ表示（波形・FFT） | × | 配列は `debug.evaluate` で読める | G03 |
| CCS-09 | スクリプト API（JS / Python） | ◯ | Python・JSON-RPC | — |
| CCS-10 | AI コーディング支援 | ◯ | MCP サーバ（`mcp_server.py`）、人と AI の交代（`session.control.*`） | — |
| MPLB-01 | Data Visualizer のデータ取得（COM・DGI） | △（画面なし） | `uart.read`、記録の stream "uart" | 画面（G03・G04） |
| MPLB-02 | DVRT（変数の読み書き・Data Stream の解読） | △ | `watch.*`、`debug.write_expression` | 受信データの書式の解読（U16） |
| MPLB-03 | グラフ（時系列・XY・簡易オシロ・カーソル・保存） | △ | 波形の画面（`WaveformView.svelte`。マウス位置のカーソル）、`recording.export` | 変数のグラフ・XY・カーソル（G03・G01） |
| MPLB-04 | 電力の監視 | 要ハード | — | U38 |
| MPLB-05 | データ取得とデバッグの同時実行 | ◯ | DAP サンプラはデバッガの転送と同時に動く（F4 仕様） | — |
| MPLB-06 | シミュレータと Stimulus | 対象外 | — | シミュレータ |
| MPLB-07 | Code Coverage | △ | 近似 `profile.coverage`、テストポイント | U17・U18 |
| MPLB-08 | DMCI（未確認） | △ | `watch.*`・`debug.write_expression` | 操作パネル（G10） |
| NRF-01 | PPK2 の電流測定 | 要ハード | — | U38 |
| NRF-02 | Source / Ampere meter（SMU） | 要ハード | — | U38・U39 |
| NRF-03 | 8 本のデジタル入力を電流と同じ時間軸 | △ | ロジアナ 8 本（100 MS/s・深いバッファ）と電圧が同じ時計 | 電流は要ハード（U38） |
| NRF-04 | Power Profiler アプリ | 要ハード | — | U38 |
| NRF-05 | Thread Viewer（停止時に更新） | △（画面なし） | `rtos.tasks` | 画面（G05） |
| NRF-06 | RTT 端末（VS Code） | △（画面なし） | `rtt.*` | 画面（G04） |
| NRF-07 | nRF Sniffer | 対象外 | — | 無線のスニッファ |

#### Espressif

| ID | 機能 | 判定 | 根拠（fprobe の API・ファイル） | 足りない点 |
|---|---|---|---|---|
| ESP-01 | apptrace（JTAG / UART でホストへ） | 対象外 | — | ESP32 専用（同じ働きは RTT） |
| ESP-02 | SystemView 形式の出力 | × | — | 取り込み（U14）、書き出し（U13） |
| ESP-03 | gcov の吸い出し | △ | セミホスティングのファイル操作（file_root・writable）で .gcda を書ける見込み | 実機で未確認、手順と書き出しが無い（U18） |
| ESP-04 | 関数の入退出の自動トレース | △ | テストポイントの自動挿入（`testpoint/instrument.py`、関数の入口・出口・分岐） | API・MCP に無い（U17） |
| ESP-05 | トレース形式・転送路の拡張（esp_trace） | 対象外 | — | ESP-IDF の部品 |
| ESP-06 | コアダンプ（フラッシュ / UART） | × | — | プローブからの状態の保存（U06） |
| ESP-07 | コアダンプの内容（タスク・スタック・変数） | × | — | U06 |
| ESP-08 | coredump-info / coredump-debug | × | — | U06 |
| ESP-09 | シリアル出力中のアドレスの自動解読 | × | ELF のシンボルは引ける（`debug.symbol.info`） | UART・RTT の流れの中の番地を変換する処理（U15） |
| ESP-10 | GDBStub（シリアルだけで GDB） | 対象外 | — | ターゲット側の機能 |

#### OpenOCD / pyOCD / probe-rs / Black Magic Probe

| ID | 機能 | 判定 | 根拠（fprobe の API・ファイル） | 足りない点 |
|---|---|---|---|---|
| OOCD-01 | 多数のアダプタ・ターゲット（Tcl 設定） | ◯ | fprobe は CMSIS-DAP v2 のアダプタとして OpenOCD から使える（実機試験あり）。ターゲットは pyOCD の定義 | — |
| OOCD-02 | フラッシュの消去・書き込み・検証・ダンプ | △ | `target.flash`（書き込み） | 消去・検証・ダンプ（U03） |
| OOCD-03 | GDB からのフラッシュ書き込み | ◯ | OpenOCD・pyOCD 経由（実機試験あり） | — |
| OOCD-04 | bp / wp（値とマスクの条件） | △ | `debug.breakpoint.set`（条件はホストで評価）、`debug.watchpoint.set` | DWT の値一致のウォッチポイント（U11） |
| OOCD-05 | 実行中のメモリアクセス | ◯ | `target.memory.*`、`watch.*` | — |
| OOCD-06 | RTT（TCP ポートで公開） | △ | `rtt.*` | TCP 公開（U05） |
| OOCD-07 | TPIU / SWO の設定 | △ | `profile.start`・`rtos.timeline.start` が DWT・ITM・TPIU を設定（`profile/dwt.py`） | 汎用の設定 API（U11） |
| OOCD-08 | profile（PC サンプリング → gmon.out） | △ | `profile.*` | gmon.out 等の書き出し（U18） |
| OOCD-09 | RTOS 認識（多数の RTOS） | △ | FreeRTOS・Zephyr | U45 |
| OOCD-10 | SMP・複数 GDB 接続 | × | — | U31・U04 |
| OOCD-11 | IPDBG（FPGA 内のロジアナ） | △ | JTAG の低レベル（DAP_JTAG_Sequence）は持つ | OpenOCD 経由での動作が未確認（U29） |
| OOCD-12 | バウンダリスキャン（SVF / XSVF） | △ | 同上 | 未確認（U29） |
| OOCD-13 | PLD / FPGA の書き込み | △ | 同上 | 未確認（U29） |
| OOCD-14 | Tcl スクリプト・Telnet・Tcl ポート | ◯ | JSON-RPC over WebSocket、MCP、Python | — |
| OOCD-15 | セミホスティング | ◯ | `debug.semihosting.*` | — |
| PYOCD-01 | CMSIS-Pack によるターゲット対応 | △ | 内部は pyOCD | Pack の導入・選択の API（U03） |
| PYOCD-02 | gdbserver / commander / flash / erase | ◯ | pyOCD から CMSIS-DAP v2 として使える（実機試験あり） | — |
| PYOCD-03 | Python API | ◯ | `host/fprobe`（Session・Debugger・LogicAnalyzer 等） | — |
| PYOCD-04 | ユーザスクリプト（接続・リセットの上書き） | × | — | U30 |
| PYOCD-05 | SWO / SWV | ◯ | `swo.*` | — |
| PYOCD-06 | セミホスティング・マルチコア | △ | セミホスティング | マルチコア（U31） |
| PYOCD-07 | 遠隔プローブ | ◯ | Ethernet のホストリンク、デーモンの `--lan` | — |
| PYOCD-08 | セキュリティ・保護の解除 | × | — | U30 |
| PYOCD-09 | RTOS 認識（未確認） | ◯ | `rtos.*`（FreeRTOS・Zephyr） | — |
| PYOCD-10 | RTT（未確認） | ◯ | `rtt.*` | — |
| PRS-01 | 多種プローブ・多アーキテクチャ（ARM・RISC-V・Xtensa） | △ | probe-rs から CMSIS-DAP として使える（実機試験あり）。ARM のみ | RISC-V（U33）。Xtensa は対象外（手持ちの ESP32 は補助の用途） |
| PRS-02 | CMSIS-Pack のフラッシュアルゴリズム | ◯ | probe-rs 経由（実機試験あり） | — |
| PRS-03 | probe-rs run（書き込み→実行→RTT→パニック時のスタック） | × | 部品（`target.flash`・`rtt.*`・`debug.backtrace`・`target.fault`）はある | 一発のコマンド（U07） |
| PRS-04 | RTT・defmt | △ | RTT | defmt（U15） |
| PRS-05 | ライブラリ | ◯ | Python ライブラリ | — |
| PRS-06 | VS Code デバッガ（DAP） | △ | — | GDB サーバ経由で Cortex-Debug を使う（U04）。VS Code 用 DAP は U04 の後 |
| PRS-07 | GDB サーバ | △ | 外部ツールの GDB サーバ | 内蔵（U04） |
| PRS-08 | コアダンプの作成 | × | — | U06 |
| PRS-09 | 性能プロファイリング | ◯ | `profile.*`（itm / pcsr、信頼区間つき） | — |
| PRS-10 | SWO / ITM | ◯ | `swo.decode`（ITM・DWT のパケット） | — |
| PRS-11 | defmt（遅延フォーマット） | × | — | U15 |
| PRS-12 | defmt-test / embedded-test | × | — | U07 |
| BMP-01 | プローブ内蔵の GDB サーバ | △ | 外部の GDB サーバ（OpenOCD・pyOCD） | デーモン内蔵（U04） |
| BMP-02 | ターゲットの自動検出 | △ | `target.connect` の target 省略時は汎用 Cortex-M | 部品の自動判別（U29） |
| BMP-03 | ウォッチポイント・フラッシュ書き込み・メモリ/レジスタ | ◯ | `debug.watchpoint.*`、`target.flash`、`target.memory.*`、`target.registers.*` | — |
| BMP-04 | SWO | ◯ | `swo.*` | — |
| BMP-05 | セミホスティング | ◯ | `debug.semihosting.*` | — |
| BMP-06 | Hosted 版（他社プローブで動く） | 対象外 | — | fprobe はプローブ本体 |
| BMP-07 | ARM Cortex と RISC-V | △ | Cortex-M | RISC-V（U33） |
| BMP-08 | RTT・ターゲット給電・UART（未確認） | △ | RTT・UART | 給電は要ハード（U39） |

#### VS Code 拡張・GDB

| ID | 機能 | 判定 | 根拠（fprobe の API・ファイル） | 足りない点 |
|---|---|---|---|---|
| VSC-01 | 複数の GDB サーバ（Cortex-Debug） | △ | OpenOCD・pyOCD 経由で使える見込み | Cortex-Debug での動作が未確認。デーモンと同時に使えない（U04） |
| VSC-02 | SWO の解読とグラフ・独自デコーダ | △（画面なし） | `swo.decode`、`swo.text` | グラフ（G03）。数値チャンネルの解釈（U16） |
| VSC-03 | RTT（Cortex-Debug） | △（画面なし） | `rtt.*` | 画面（G04）、TCP 公開（U05） |
| VSC-04 | Live Watch | △（画面なし） | `watch.*` | 画面（G03） |
| VSC-05 | 周辺レジスタ（SVD）・メモリ表示 | △（画面なし） | `target.svd.*`、`target.peripheral.*`、`target.memory.view` | 画面（G02） |
| VSC-06 | 逆アセンブル・命令ステップ・マルチコア・マルチセッション | △ | `debug.disasm.*`、`target.step`、デーモンの複数利用者 | マルチコア（U31）、逆アセンブルの画面（G02） |
| VSC-07 | RTOS スレッド表示（GDB サーバの RTOS 認識） | △ | `rtos.backtrace` | GDB へのスレッドの提示（U04） |
| VSC-08 | probe-rs 拡張 | △ | — | PRS-06 と同じ（U04） |
| VSC-09 | RTOS ビューア（ThreadX・FreeRTOS・Zephyr） | △ | `rtos.tasks` | オブジェクト（U08）、ThreadX（U45）、画面（G05） |
| VSC-10 | レジスタビューア（SVD） | △（画面なし） | `target.peripheral.read` | 画面（G02） |
| VSC-11 | シリアルモニタ（時刻付与・送信） | △（画面なし） | `uart.read/write`、記録で時刻付き | 画面（G04） |
| GDB-01 | 逆実行 | × | — | U36 |
| GDB-02 | 逆実行の条件（record full / btrace） | × | — | U36 |
| GDB-03 | トレースポイント（止めずに式を集める） | △ | ログ・ブレークポイント（`debug.breakpoint.set` の log。数 ms 止まる） | GDB のトレースポイントの口（U48）、止まらない収集（U11） |
| GDB-04 | トレースポイントの条件（スタブの実装） | △ | 同上 | U48 |
| GDB-05 | Python 拡張・MI | 対象外 | — | GDB 自身の拡張機構（U04 で GDB とつながれば使える） |

#### Orbuculum / Trice / Perfetto

| ID | 機能 | 判定 | 根拠（fprobe の API・ファイル） | 足りない点 |
|---|---|---|---|---|
| ORB-01 | 多重化サーバ（TCP で複数へ配信） | △ | デーモンに複数の利用者（JSON-RPC、`swo.data` の通知） | 生のストリームの TCP 配信（U05） |
| ORB-02 | 多様なトレース源 | 対象外 | — | 他社プローブの受け口（fprobe はプローブ本体） |
| ORB-03 | SWO（NRZ / Manchester）とパラレルトレース | △ | NRZ | Manchester（U51）、パラレル（U34） |
| ORB-04 | orbcat（ITM チャネルを出力） | ◯ | `swo.text`（ポートごとの行）、`swo.decode` | — |
| ORB-05 | orbtop（関数・行の時間比率、JSON） | ◯ | `profile.result`（by function / line、JSON） | dot / gnuplot 出力は U18 |
| ORB-06 | orbstat（KCacheGrind 形式） | × | — | U18 |
| ORB-07 | orbmortem（命令トレースの事後解析） | × | — | U35・U36 |
| ORB-08 | orbzmq（ZeroMQ で配信） | × | — | U05 |
| ORB-09 | orblcd（LCD の模擬表示） | 対象外 | — | 用途が限られる表示の玩具（操作パネル G10 で代替） |
| ORB-10 | orbdump / orbtrace（生 SWO をファイルへ） | ◯ | `swo.read`（16 進）、記録の stream "swo" と `recording.export` | — |
| TRC-01 | ID ベースのバイナリログ | × | — | U15 |
| TRC-02 | 累積 ID 表（til.json） | × | — | U15 |
| TRC-03 | 多様な転送（RTT・UART） | × | 生のバイトは `rtt.*`・`uart.*` で受けられる | 解読（U15） |
| TRC-04 | 時刻・暗号化 | △ | 時刻はデバイスのタイムベース（記録） | 暗号化は対象外 |
| PFT-01 | ブラウザ上のタイムライン UI | × | — | G01、Perfetto への書き出し（U13） |
| PFT-02 | 多形式の取り込み | × | — | U13 |
| PFT-03 | SQL による解析 | × | — | U24 |
| PFT-04 | 組込み（CTF）からの変換 | × | — | U13・U14 |

#### RTOS・イベントの解析ツール

| ID | 機能 | 判定 | 根拠（fprobe の API・ファイル） | 足りない点 |
|---|---|---|---|---|
| UCP-01 | 実行中の変数の表示・変更 | △ | `watch.*`、`debug.write_expression` | 走行中の書き込みが未確認（U43）、画面（G10） |
| UCP-02 | 部品を並べた操作パネル | × | — | G10 |
| UCP-03 | オシロスコープ機能（ループに同期・トリガ・プリ/ポスト） | △ | `watch.*`（プローブの時計で等間隔） | トリガ・プリトリガ、ターゲット側の同期取得（U43） |
| UCP-04 | µC/OS・µC/TCP-IP の認識 | × | — | U45 |
| UCP-05 | 多様な通信路（J-Link・RS-232・USB・TCP/IP） | △ | デバッグポート（SWD/JTAG）、ホストへは USB・Ethernet・シリアル | ターゲット常駐コードとの UART 経由の通信路は無い（U43） |
| TRX-01 | イベント記録（ThreadX のリング） | × | — | U14・U45 |
| TRX-02 | イベントの時系列表示 | × | — | U14、G01 |
| TRX-03 | Execution Profile（スレッド・ISR・アイドル） | △ | タスク `rtos.timeline.*`、割り込み `profile.exceptions` | ThreadX（U45）、タスクと割り込みの合成（U09） |
| TRX-04 | Popular Services / Thread Stack Usage | △ | タスクのスタック（`rtos.tasks`） | サービス呼び出しの記録（U10） |
| TRX-05 | Performance Statistics（優先度逆転の検出） | × | — | U09・U10 |
| TRX-06 | マルチコア版 | × | — | U31 |
| ZEP-01 | 複数のトレース形式（CTF・SystemView 等） | × | — | U14 |
| ZEP-02 | 転送路（UART・USB・RTT・RAM・ITM） | △ | バイトは受けられる（`rtt.*`・`uart.*`・`swo.*`） | 形式の解読（U14） |
| ZEP-03 | 名前付きイベント | × | — | U16・U14 |
| ZEP-04 | オブジェクト追跡 | × | Zephyr のスレッドは `rtos.tasks`（最小限） | U08・U45 |
| ZEP-05 | 表示ツール（Trace Compass・Perfetto 等） | × | — | U13 |
| FRT-01 | 30 以上の連動ビュー | △ | 材料は記録・`rtos.timeline.*`・`profile.*` | 画面（G01・G05・G06・G07） |
| FRT-02 | CPU Load Graph | △ | `rtos.timeline.stop` の使用率 | 推移（U09）、グラフ（G05） |
| FRT-03 | Communication Flow | × | — | U10 |
| FRT-04 | ユーザイベントからの状態遷移図 | × | — | U16・U25 |
| FRT-05 | Streaming / Snapshot | △ | ITM のストリーミング | RTT・スナップショット（U14） |
| FRT-06 | デバッガと同じプローブで併用 | ◯ | 時間軸はデバッグと同時に取れる（サンプラ・SWO はデバッガの転送と別） | — |

#### ロジアナのソフト

| ID | 機能 | 判定 | 根拠（fprobe の API・ファイル） | 足りない点 |
|---|---|---|---|---|
| SAL-01 | 標準の解析器（I2C・SPI・UART 等） | △ | `la.decode`（uart・spi・i2c・swd、`decode/`） | 種類が少ない（CAN・1-Wire・I2S 等、U20） |
| SAL-02 | High Level Analyzer（Python で上位層） | △ | 解読はホストの Python（`decode/core.py`） | 利用者の解読器を差し込む口が無い（U20） |
| SAL-03 | Measurement 拡張（周波数・デューティ） | × | — | U19 |
| SAL-04 | グリッチフィルタ | × | — | U21 |
| SAL-05 | Automation API（取得・解析・保存・ヘッドレス） | ◯ | `la.*`、`recording.export`、デーモン（ヘッドレス） | — |
| SAL-06 | 公式 MCP サーバ | ◯ | MCP サーバ。ロジアナに加えてデバッガ・記録まで | — |
| SAL-07 | Analyzer SDK | △ | Python の解読器 | 差し込みの口（U20） |
| SIG-01 | 積み重ね可能なプロトコルデコーダ | × | — | U20 |
| SIG-02 | 多種の測定器 | 対象外 | — | 計測器の統合ソフトの領域 |
| SIG-03 | sigrok-cli・ライブラリ | ◯ | Python ライブラリ、`fprobe la` の CLI | — |
| SIG-04 | 多くのファイル形式 | △ | VCD・CSV・JSON（`recording.export`、`la.capture` の vcd_path） | WAV・バイナリ・sigrok の .sr（U20） |
| SIG-05 | PulseView（GUI） | ◯ | 波形の画面（`WaveformView.svelte`）、VCD を PulseView で開ける | — |
| DSV-01 | Stream / Buffer モード | ◯ | `la.stream`（流し続け）、`la.deep.*`（DDR3 128 MB） | — |
| DSV-02 | 約 100 種のデコーダ | △ | 4 種 | U20 |
| DSV-03 | 単純トリガ・高度なトリガ | ◯ | 値・マスク・エッジ（`la.*`）、4 段の多段トリガ（`xtrig.set_sequence`、回数・制限時間） | パルス幅は U21 |
| WVF-01 | ロジアナ（パラレルバス・SPI/I2C/CAN/UART） | △ | `la.decode` | パラレルバス・CAN（U20） |
| WVF-02 | Protocol 機器（生成と解読） | △ | UART の送受信（`uart.*`） | SPI・I2C・CAN の送信（U41） |
| WVF-03 | オシロ・波形発生・パターン発生・スペアナ | △ | — | パターン発生は U41。アナログの計測器は要ハード |
| WVF-04 | スクリプトと SDK | ◯ | Python・JSON-RPC | — |
| WVF-05 | 計測器間のクロストリガ | ◯ | ロジアナ・DAP の番地一致・ホスト・多段トリガ → ロジアナ・ターゲット停止（`xtrig.*`） | 外部機器とのトリガ入出力は U22 |

#### 日本のツール

| ID | 機能 | 判定 | 根拠（fprobe の API・ファイル） | 足りない点 |
|---|---|---|---|---|
| DT-01 | テストポイント方式（関数・2 点間の時間） | △ | `testpoint/`（自動挿入・解読・解析。2 線の GPIO で実機確認） | API・MCP に無い（U17） |
| DT-02 | 出力経路の選択 | △ | GPIO の直列・並列、ITM、RTT の解読（`testpoint/decode.py`） | 並列・ITM は実機で未確認（U17 で確認） |
| DT-03 | 専用記録機 DBOX+Trace | ◯ | fprobe 自身がロジアナで記録（深いバッファ・流し続け） | — |
| DT-04 | 長時間記録（最大 30 日） | △ | `la.stream`、記録 | 日単位の運用（U23） |
| DT-05 | アナログ同時記録（10 MSPS・12 bit） | △ | 電圧（1 ms ごと） | U40（Pico の ADC の範囲まで。10 MSPS は要ハード） |
| DT-06 | タスク切替・割り込み・プロセス（Linux 含む） | △ | `rtos.timeline.*`、`profile.exceptions` | Linux（U32） |
| DT-07 | 変数の監視 | ◯ | `watch.*`、テストポイントの変数（`WatchVar`） | — |
| DT-08 | 実行ログに基づくカバレッジ | △ | テストポイントの C0/C1（ライブラリ） | API（U17） |
| DT-09 | 処理時間・周期・応答時間のばらつき | ◯ | `timing.measure`（最小・最大・平均・σ・中央値・p90・p99・分布・外れ値）、テストポイントの関数ごとの統計 | — |
| DT-10 | 期待値との比較・証跡の自動生成 | × | — | U25 |
| DT-11 | 機器間通信との同時確認 | ◯ | `la.decode` が記録の共通の時間軸に注釈を入れる、`uart.*` も同じ記録 | — |
| DT-12 | AUTOmeal（テスト自動化）との連携 | 対象外 | — | 別製品の試験台。fprobe は API で連携できる |
| DT-13 | 出荷後の不具合ログ収集 | 対象外 | — | 現場の運用 |
| DT-14 | オーバーヘッド低減（内容未確認） | △ | 変わった関数だけに入れ直す（`--only-changed`） | 比較対象の中身が不明 |
| KMC-01 | 大容量のリアルタイムトレース（4 GB） | × | — | U34 |
| KMC-02 | 多コア（16 CPU 以上） | × | — | U31 |
| KMC-03 | Linux 対応 ICE | × | — | U32 |
| KMC-04 | 多アーキテクチャ（Arm・RISC-V・SuperH） | △ | Arm Cortex-M | U32・U33（SuperH は対象外） |
| KMC-05 | ファームウェア更新で機能追加 | ◯ | FPGA のビットストリームの差し替えで回路ごと機能を足せる。ケイパビリティで申告（`caps.py`） | — |
| KMC-06 | VS Code プラグイン | △ | — | U04（GDB 経由） |
| CTX-01 | リアルタイムモニタ（10 µs 周期・実行中の変更・条件で記録・CSV） | △ | `watch.*`（10 kHz を実機確認）、`recording.export` の csv | 10 µs（100 kHz）の確認、条件での開始・停止、走行中の書き込みの確認（U43） |
| CTX-02 | 内蔵トレース（ETB/MTB）と外部トレース（ETM 4 GB） | × | — | U34・U35 |
| CTX-03 | Undo トレース | × | — | U36 |
| CTX-04 | Core Benchmark（CPU 使用率の推移・関数のグループ化） | △ | `profile.result`（関数の割合） | 推移・グループ化（U18・U09） |
| CTX-05 | OS 対応（µITRON・TOPPERS・FreeRTOS・Zephyr・Linux） | △ | FreeRTOS・Zephyr | U45・U32 |
| CTX-06 | 異種マルチコア | × | — | U31 |
| CTX-07 | バグ検出（配列・ヒープのオーバーフロー、ベクタキャッチ） | △ | タスクのスタックのあふれの印（`rtos.tasks`） | ベクタキャッチ（U02）、ヒープ（U27） |
| CTX-08 | ロジアナモード（4 ch・200 MHz） | △ | 8 本・100 MS/s・128 MB・多段トリガ・デバッガと同じ時計 | 200 MS/s に届かない（U22） |
| CTX-09 | ホットプラグ・フラッシュ・シリアルフラッシュ書き込み | △ | connect_mode attach、`target.flash` | SPI フラッシュ（U42） |
| SOP-01 | EJ-Extreme Long Time Trace（100 GB 超） | × | — | U34・U23 |
| SOP-02 | JTAG-ICE・HyperSTAC（機能未確認） | 対象外 | — | 機能が未確認で比較できない |
| YDC-01 | adviceLUNA（関数・スレッドの履歴とタスク遷移のチャート） | △ | テストポイント（ライブラリ）、`rtos.timeline.*` | API（U17）、画面（G01） |
| YDC-02 | TRQerAM（動きと通信データの同時可視化） | △（画面なし） | 記録に解読と RTOS・テストポイントが同じ時間軸で入る | 画面（G01） |
| YDC-03 | microVIEW-PLUS（未確認） | 対象外 | — | 機能が未確認で比較できない |

---

## 2. 実装の単位（バックログ）

× と △ を、独立に作れる単位（目安 0.5〜3 日）にまとめた。「満たす ID」は §1 の「足りない点」でその単位を挙げた ID（その単位だけで ◯ になるとは限らない。画面 `G..` も要るものがある）。

優先度: **A** = 日常のデバッグや解析の力に直結 / **B** = 差別化・上位互換の幅を広げる / **C** = 需要が限られる・大きな前提が要る。

### 2.1 並行で作るときの共通の衝突点

- **API を足す単位はすべて** `host/api/fprobe_api.json`（正）と生成物 `docs/api.md`、MCP の `host/fprobe/mcp_server.py`（`Probe` の継承の並びと INSTRUCTIONS）に触る。衝突を小さくするため、各単位は新しい部品 `host/fprobe/probe_<名前>.py`（既存の `probe_rtos.py` 等と同じ混ぜ込みの形）に実装し、JSON は自分のメソッドの塊を足すだけにする。JSON と INSTRUCTIONS の合流は最後にまとめて行う。
- **ゲートウェアの単位**（U21・U22・U34・U38・U42・U51・U52）は `gateware/rtl/core/fprobe_core.vhd`、`gateware/fprobe_gw/csr/fprobe_map.py`、`caps.py`、ボード定義に触り、しかも**論理の使用率 87%** を取り合う。並行で設計はできるが、合流と配置配線は 1 つずつ行う。足りなければケイパビリティで機能を外したデザインの変種（`designs.py`）を作る。
- `host/fprobe/debug.py` に触る単位（U01・U02・U28・U53）は同じファイルの別の関数として作れるが、合流の順を決めておく。

### 2.2 一覧

| 単位 | 名前 | 満たす ID | 内容 | 層 | 依存 | 要ハード | 優先度 | 目安 | 触るファイル（並行の可否） |
|---|---|---|---|---|---|---|---|---|---|
| U01 | ソース行単位のステップ | SEG-17, SEG-51, IAR-25 | `debug.step`（into / over / out / until 行）。DWARF の行表と CFI で次の行・戻り先を決め、ステップ中の割り込みマスク（DHCSR.C_MASKINTS）を選べる。C++ の DWARF の確認も行う | ホスト・API | なし | — | A | 1.5 日 | `debug.py`、`probe_inspect.py`（U02・U28 と合流順を決める） |
| U02 | ベクタキャッチとリセットの種類 | SEG-16, SEG-17, IAR-24, IAR-25, CTX-07 | `target.vector_catch`（DEMCR の VC_HARDERR・VC_CORERESET 等）、`target.reset` の kind（SYSRESETREQ / VECTRESET / nRESET 線）。故障で止まったら `target.fault` の要約を返す | ホスト・API | なし | — | A | 0.5〜1 日 | `debug.py`、`probe_inspect.py` |
| U03 | Flash 操作の拡充 | SEG-03, SEG-04, SEG-05, SEG-06, SEG-31, KEIL-18, CUBE-21, OOCD-02, PYOCD-01 | 消去（全体・範囲）・検証・読み出して保存・ファイルとの比較・進捗の通知、CMSIS-Pack の導入と選択（pyOCD の pack 機能）、外部 Flash のアルゴリズム（FLM）の指定。書き込み・読み出しの速度を実機で計測して公表する | ホスト・API（画面は G11） | なし | — | A | 2 日 | 新規 `flash.py`・`probe_flash.py`（並行可） |
| U04 | デーモン内蔵の GDB サーバ | SEG-11, SEG-32, SEG-36, KEIL-17, CUBE-01, OOCD-10, PRS-06, PRS-07, BMP-01, VSC-01, VSC-07, VSC-08, GDB-05, KMC-06 | デーモンが握る 1 つのセッションの上で GDB リモートプロトコルを提供し、IDE（Cortex-Debug・CubeIDE 等）とロジアナ・記録・AI を同時に使えるようにする。RTOS のタスクを GDB のスレッドとして見せ、`monitor` で fprobe の API を呼べる。GDB は操作権の調停（`api/arbiter.py`）に 1 利用者として加わる | ホスト（デーモン） | なし（マルチコアは U31 の後） | — | A | 3 日 | 新規 `gdb_server.py`、`api/daemon.py`（並行可） |
| U05 | 生ストリームの TCP 公開 | SEG-23, OOCD-06, VSC-03, ORB-01, ORB-08 | RTT の各チャンネル・SWO・UART を TCP（Telnet 互換）で公開し、複数のツールが同時に読む。書き込み（下り）も。ファイルへの記録（Logger） | ホスト（デーモン） | なし | — | B | 1.5 日 | 新規 `api/streams.py`（並行可） |
| U06 | スナップショット（状態の保存と復元） | SEG-59, WR-01, MF-01, ESP-06, ESP-07, ESP-08, PRS-08 | 止めた状態（コアレジスタ・RAM・副作用の無い周辺レジスタ・RTOS の状態・記録の参照・ELF の指紋）を 1 ファイルに保存。プローブ無しで開き、`debug.backtrace`・`debug.evaluate`・`rtos.*` をスナップショットに対して使える（メモリの読みの裏を差し替える）。RAM を書き戻して再開。ELF のコアファイルとして書き出し GDB でも読める | ホスト・API | なし | — | A | 3 日 | 新規 `snapshot.py`・`probe_snapshot.py`、`dwarf/access.py` の読みの口（並行可） |
| U07 | CI 実行コマンド | SEG-31, IAR-31, WIN-08, CUBE-22, PRS-03, PRS-12 | `fprobe run firmware.elf`: 書き込み→実行→RTT・セミホスティング・SWO の出力→SYS_EXIT の終了コード・時間切れ・故障で判定。故障時は自動で `target.fault`・`debug.backtrace`・スナップショット（U06）を残す。JUnit XML。defmt-test 等の試験の結果の行も読む | ホスト（CLI） | U03（検証）、U06 は任意 | — | A | 1.5 日 | `__main__.py`、新規 `run.py`（並行可） |
| U08 | RTOS のカーネルオブジェクトの一覧 | SEG-53, IAR-33, CUBE-08, MCUX-13, E2S-05, CCS-06, VSC-09, ZEP-04 | `rtos.objects`: キュー・セマフォ・ミューテックス（持ち主）・タイマ・イベントグループ・ストリームバッファ、ヒープの残量・最小残量。名前の無いものも番地で出す。走行中の読み（整合性の注意付き）も選べる | ホスト・API | なし | — | A | 2 日 | `rtos/freertos.py`・`rtos/zephyr.py`・`rtos/model.py`・`probe_rtos.py`（U09・U10 と同じ部品。合流順を決める） |
| U09 | RTOS の時間軸の解析の拡張 | SEG-66, TZ-02, TZ-06, TZ-07, TZ-08, TZ-09, TRX-03, TRX-05, FRT-02, CTX-04 | 窓ごとの CPU 負荷の推移、タスクのジョブ単位の指標（実行時間・周期・応答時間・最悪値）、横取りを除いた区間、タスクのスタックの推移、割り込みとタスクの合成。優先度逆転の検出（ミューテックスの出来事は U10 から） | ホスト・API | 応答時間・優先度逆転は U10 | — | A | 2 日 | `rtos/timeline.py`、`profile/timing.py`、`probe_rtos.py` |
| U10 | トレースフックの拡張 | SEG-69, TZ-01, TZ-04, TZ-09, TRX-04, TRX-05, FRT-03 | `fprobe_trace.h` に、カーネル API（キュー送受信・セマフォ・ミューテックス）・タスクの準備完了・優先度の変化・ISR の出入りの出来事を足す（ITM / RTT）。ホストで Communication Flow（スレッドとオブジェクトの有向グラフ）・ブロック時間・キュー使用率を出す。フックの負荷を実機で計測 | ターゲット側ヘッダ・ホスト・API | なし | — | B | 3 日 | `host/firmware/freertos_demo/fprobe_trace.h`、`rtos/timeline.py`、新規 `rtos/flow.py` |
| U11 | SWO の DWT・ITM 設定の API | IAR-09, IAR-21, KEIL-06, CUBE-04, MCUX-04, MCUX-06, OOCD-04, OOCD-07, GDB-03 | `swo.configure`: ITM のポート・TPIU・例外トレース・イベントカウンタ・DWT のコンパレータによるデータトレース（変数へのアクセスを値・PC・時刻付きで、止めずに）・値一致のウォッチポイント。走行中に設定しても止まらないことを実機で確認。結果は記録の stream "swo" に | ホスト・API | なし | — | A | 2 日 | `profile/dwt.py`、新規 `probe_swo.py`（並行可） |
| U12 | サイクルの計測 | SEG-21, IAR-15, KEIL-06, ADS-04, E2S-04 | CYCCNT による区間のサイクル（2 つのブレークポイント間）、DWT のイベントカウンタ（CPI・LSU・SLEEP・EXC・FOLD）、サンプラで CYCCNT を時刻付きで読んで CPU クロックを実測 | ホスト・API | U11（設定の部品を共有） | — | B | 1 日 | `profile/dwt.py`、`probe_profile.py` |
| U13 | 記録を Perfetto 等へ書き出す | ESP-02, PFT-01, PFT-02, PFT-04, ZEP-05 | `recording.export` に format perfetto（Chrome JSON のトレースイベント: タスク・割り込み・テストポイントの関数はスライス、変数・電圧はカウンタ、DAP・UART・注釈は点）。SystemView 形式の書き出しも | ホスト・API | なし | — | A | 1.5 日 | `recording.py`（export）、新規 `export/perfetto.py`（並行可） |
| U14 | 既存のトレース形式の取り込み | SEG-64, KEIL-01, KEIL-03, TZ-10, ESP-02, PFT-04, TRX-01, TRX-02, ZEP-01, ZEP-02, ZEP-03, FRT-05 | RTT・ITM・UART で届く SystemView・CTF（Zephyr）・Tracealyzer・Event Recorder を解読して記録の stream に入れる。ターゲット RAM のリングバッファの吸い出し（スナップショット方式・ポストモーテム）。SCVD の解釈（Component Viewer） | ホスト・API | なし | — | B | 3 日 | 新規 `tracefmt/`（並行可） |
| U15 | ID ベースのログの解読 | SEG-67, TZ-11, MF-07, ESP-09, PRS-04, PRS-11, TRC-01, TRC-02, TRC-03 | defmt（ELF の表）・Trice（til.json）・Compact logging の解読。UART・RTT の流れの中のコード番地を関数:行に変換。結果は記録の stream "log" | ホスト・API | なし | — | B | 2 日 | 新規 `logfmt/`（並行可） |
| U16 | ターゲットが出す値とイベント | SEG-67, SEG-70, IAR-10, KEIL-02, ADS-04, TZ-01, TZ-05, MPLB-02, VSC-02, ZEP-03, FRT-04 | ITM のポート・RTT のチャンネルを型（i32・float・Q16.16・構造体）で解釈して記録の stream "user" に。名前付きイベント・区間（EventStart/Stop 相当）の集計を `timing.measure` へ | ホスト・API | なし | — | B | 1.5 日 | `probe_observe.py`、`recording.py`（stream の追加） |
| U17 | テストポイントの API・MCP | SEG-56, SEG-57, IAR-16, KEIL-10, KEIL-11, T32-05, T32-06, T32-10, WIN-02, WIN-04, GHS-03, MPLB-07, ESP-04, DT-01, DT-02, DT-08, YDC-01 | 既存の `testpoint/`（挿入・解読・解析）を API にする: `testpoint.instrument` / `start`（ロジアナ・ITM・RTT の出力を記録に）/ `report`（関数の時間・呼び出しの木・遷移・C0/C1・異常）。並列・ITM の出力を実機で確認 | API・MCP | なし | — | A | 2 日 | 新規 `probe_testpoint.py`（並行可） |
| U18 | プロファイル・カバレッジの書き出しと集計 | IAR-14, KEIL-10, ADS-04, T32-06, WIN-04, MPLB-07, ESP-03, OOCD-08, ORB-05, ORB-06, CTX-04 | lcov / gcov、KCacheGrind（callgrind）、gmon.out、dot。時間軸の区間に限る集計（t0・t1）、関数のグループ集計（名前の型・ファイル）、セミホスティングで .gcda を書く手順の確認 | ホスト・API | なし | — | B | 1.5 日 | `profile/`、新規 `export/profile_formats.py`（並行可） |
| U19 | ロジアナの測定 | SAL-03 | `la.measure`: 周波数・周期・デューティ・パルス幅の統計、チャンネル間のエッジの時間差（セットアップ・ホールド）、範囲（start・end）指定 | ホスト・API | なし | — | A | 1 日 | `la.py`、新規 `la_measure.py`（並行可） |
| U20 | 解読器の追加と積み重ね | WIN-06, SAL-01, SAL-02, SAL-07, SIG-01, SIG-04, DSV-02, WVF-01 | CAN・1-Wire・JTAG・I2S・パラレルバス・Manchester の解読、解読結果を入力にする上位の解読器（利用者の Python を登録）、sigrok の .sr の書き出し | ホスト・API | なし | — | B | 3 日 | `decode/`（新規ファイル中心、並行可） |
| U21 | ロジアナのグリッチフィルタとパルス幅トリガ | SAL-04, DSV-03 | チャンネルごとのグリッチフィルタ（N 周期未満の変化を捨てる）、「一定時間変化しない」「幅が範囲外」のトリガ（多段トリガの事象に追加） | ゲートウェア・ホスト | なし | — | B | 2 日 | `rtl/la/la_front.vhd`、`rtl/core/trig_seq.vhd`、`fprobe_map.py`（ゲートウェアの合流は 1 つずつ） |
| U22 | ロジアナの専用チャンネルと外部トリガ | T32-11, T32-13, WVF-05, CTX-08 | 空いている PMOD にロジアナの外部入力 8〜16 本を足す（いまはデバッグコネクタの 8 本）、外部トリガの入出力、入力の DDR 取り込みで 200 MS/s（ベンダーのプリミティブはラッパー越し） | ゲートウェア・ボード | なし | 配線のみ（ジャンパ線、購入なし） | B | 3 日 | `boards/primer20k.py`、`rtl/la/`、`fprobe_core.vhd` |
| U23 | 長時間の記録の運用 | TZ-12, DT-04, SOP-01 | 記録のファイル分割・索引・縮図の事前計算、日単位の連続記録（ロジアナの流し続け＋出来事）、巨大な記録の部分読み込み | ホスト | なし | — | B | 3 日 | `recording.py`、`collector.py` |
| U24 | 記録の SQL 問い合わせ | PFT-03 | 記録を SQLite に書き出し、`recording.query`（読み取り専用の SQL）。AI が集計に使う | ホスト・API | なし | — | B | 1 日 | `recording.py`（export）、新規 `probe_query.py`（並行可） |
| U25 | 規則の検査・期待値の比較・故障注入の台本 | WIN-03, WR-03, TZ-08, FRT-04, DT-10 | 区間の上限・順序・周期の規則を書き、記録から違反を探して印を付ける（派生イベント）。期待値と実測の比較と証跡の報告。ユーザイベントからの状態遷移図。クロストリガの割り込み書き込みを使う故障注入の台本 | ホスト・API | U16 があると状態遷移図が使える | — | B | 2 日 | 新規 `rules.py`・`probe_rules.py`（並行可） |
| U26 | メモリ・スタックの使用量の解析 | SEG-72, IAR-27, IAR-28, CUBE-06, CUBE-07 | ELF のセクション・領域・シンボルの大きさ（Build Analyzer）、コンパイラの .su と逆アセンブルの呼び出しグラフからの静的な最大スタック、ベアメタル（MSP）のスタックの塗り込みと使用量 | ホスト・API | なし | — | B | 2 日 | 新規 `elfsize.py`・`stackusage.py`（並行可） |
| U27 | ヒープの解析 | SEG-68, TZ-06, CTX-07 | newlib（dlmalloc）・FreeRTOS heap_4/5 の走査（使用量・断片化・壊れの検出）、malloc / free の記録（ログ・ブレークポイントかフック）とリークの候補 | ホスト・API | U08（FreeRTOS のヒープの部品を共有） | — | B | 2 日 | 新規 `heap.py` |
| U28 | Flash 上の無制限ブレークポイント | SEG-15, SEG-50, IAR-23 | HW の比較器が尽きたら Flash の命令を BKPT に書き換える。ページ単位のまとめ書き・キャッシュ、ステップ中は HW を優先、切断・異常終了時に必ず元に戻す（書き換えの記録をファイルにも残す） | ホスト | U03（Flash の部品） | — | A | 3 日 | `debug.py`、新規 `flash_bp.py` |
| U29 | 接続の診断と低レベル操作 | SEG-01, SEG-18, SEG-19, SEG-20, SEG-21, IAR-35, OOCD-11, OOCD-12, OOCD-13, BMP-02 | DP / AP の直接の読み書き、JTAG のチェーンの走査（IDCODE・IR 長の推定）、IR/DR、ピンの状態、R/W の速度試験、接続の診断（電圧・SWD の ACK・DPIDR・ROM テーブル→部品の自動判別）。OpenOCD 経由の SVF・IPDBG を実機で確認。cJTAG は調査のみ | ホスト・API | なし | — | B | 2 日 | 新規 `probe_lowlevel.py`（並行可） |
| U30 | 接続の手順の上書きとロック解除 | SEG-12, CUBE-20, MCUX-11, PYOCD-04, PYOCD-08 | 利用者の Python で接続・リセットの手順を上書き（pyOCD のデリゲート）、STM32 のオプションバイトの読み書きと RDP の解除（全消去の確認付き） | ホスト・API | なし | — | B | 2 日 | 新規 `connect_scripts.py`（並行可） |
| U31 | マルチコア（RP2040） | SEG-01, SEG-11, SEG-63, IAR-34, ADS-01, T32-21, TZ-12, CUBE-09, OOCD-10, PYOCD-06, VSC-06, TRX-06, KMC-02, CTX-06 | SWD のマルチドロップ（DP TARGETSEL。ゲートウェアは DAP_SWD_Sequence を持つ）、API にコアの指定、コアごとの停止・再開・ブレークポイント、時間軸のコア別の段、GDB のスレッド（U04） | ホスト・API（ゲートウェアは確認） | なし | 配線のみ | B | 3 日 | `session.py`、`debug.py`、全 `probe_*.py` の core 引数（広く触るので単独で） |
| U32 | Cortex-A7 / ARM11 の JTAG デバッグ | SEG-18, ADS-01, ADS-02, T32-20, TZ-14, DT-06, KMC-03, KMC-04, CTX-05 | Raspberry Pi 2（Cortex-A7）・Pi Zero（ARM1176）を OpenOCD 経由で動かし、fprobe の API から停止・レジスタ・メモリ（pyOCD は Cortex-A を持たないので裏の実装を抽象化）。Linux カーネルの認識は後段 | ホスト・API | なし | 配線のみ | B | 3 日 | 新規 `backends/openocd.py`（並行可） |
| U33 | RISC-V（RP2350 の Hazard3） | SEG-01, PRS-01, BMP-07, KMC-04 | RP2350 の RISC-V コアのデバッグ（probe-rs / OpenOCD 経由の確認から） | ホスト | U32 の裏の抽象 | — | C | 3 日 | U32 と同じ |
| U34 | ETM のパラレルトレースの受信 | SEG-47, SEG-48, SEG-50, IAR-13, KEIL-08, ADS-03, T32-01, T32-03, T32-11, WIN-01, GHS-02, ORB-03, KMC-01, CTX-02, SOP-01 | TPIU の 4 ビットのトレース端子（TRACECLK は両エッジ）を取り込み、DDR3 のリング・ストリーミングに記録（ロジアナの深いバッファの部品を流用）。STM32F401 の TRACED0〜3 をジャンパ線でつなぐ（要確認）。全体設計 §1.3 の非目標を改める判断が要る | ゲートウェア・ボード | なし | 配線のみ | B | 3 日 | 新規 `rtl/trace/`、`fprobe_core.vhd`、`fprobe_map.py`、`caps.py` |
| U35 | ETM の解読と応用 | SEG-28, SEG-29, SEG-47, SEG-48, SEG-49, SEG-55, SEG-57, IAR-13, IAR-19, KEIL-08, KEIL-09, KEIL-10, KEIL-11, T32-02, T32-04, WIN-01, E2S-02, CCS-01, CCS-02, ORB-07, CTX-02 | TPIU のフォーマッタの解除、ETMv3.5 の解読、ELF からの命令列の再構成、命令トレースの一覧と検索、命令単位のカバレッジ（分岐の成立・不成立）、呼び出し回数付きのプロファイル、トレースの開始・停止の条件。ETB / MTB のあるチップでは読み出しも | ホスト・API | U34 | — | B | 3 日 | 新規 `trace/`（並行可） |
| U36 | 事後の命令履歴と逆方向の調査 | T32-08, T32-09, GHS-01, WR-02, GDB-01, GDB-02, ORB-07, CTX-03 | 止まる直前の命令列、トレースとスナップショット（U06）からの逆ステップ・レジスタの再構成（データトレースが無い分の限界を明示） | ホスト・API | U35、U06 | — | C | 3 日 | `trace/` |
| U37 | Monitor Mode Debugging | SEG-27 | DebugMonitor 例外のハンドラ（ターゲットに組み込む）と API。高優先度の割り込みを止めずに調べる | ターゲット側・ホスト | なし | — | C | 2 日 | 新規 `monitor_mode/` |
| U38 | 電流の計測 | SEG-30, SEG-58, IAR-03, IAR-04, IAR-05, IAR-06, IAR-07, KEIL-02, KEIL-04, KEIL-12, KEIL-13, ADS-05, T32-14, WIN-09, CUBE-17, MCUX-07, MCUX-08, SLS-01, SLS-02, SLS-04, CCS-03, MPLB-04, NRF-01, NRF-02, NRF-03, NRF-04 | INA226 を FPGA の I2C に直結（CLAUDE.md の案）。記録の stream "power"、電流と PC サンプルの対応（関数ごとのエネルギー）、区間のエネルギー、電流のしきい値を多段トリガの事象にして停止（Power ブレーク） | ゲートウェア・ホスト・API | なし | **要ハード**（INA226・シャント） | B | 3 日 | 新規 `rtl/i2c/`、`fprobe_map.py`、新規 `power.py` |
| U39 | ターゲット給電と電圧範囲 | SEG-08, SEG-09, IAR-01, IAR-02, PEM-04, MCUX-10, NRF-02, BMP-08 | 給電の入切（負荷スイッチ）、1.2〜5 V のターゲット（レベル変換） | ボード・ゲートウェア | なし | **要ハード** | C | 2 日 | ボード定義 |
| U40 | アナログ入力の拡充 | IAR-04, KEIL-14, T32-12, WIN-06, MCUX-09, DT-05 | 補助 Pico の ADC 3 本・高い率（Pico の ADC の上限と UART の帯域の範囲）、記録の stream "aux" に全チャンネル、電圧のしきい値を多段トリガの事象に。配線の注意（Pico の USB を先に、電圧範囲、逆流）を手順に書く | 補助ファーム・ゲートウェア・ホスト | なし | 配線のみ | B | 2 日 | `host/firmware/aux_pico`、`rtl/core/aux_link.vhd`、`aux_mcu.py` |
| U41 | 試験用 I/O の API | KEIL-14, MCUX-10, WVF-02, WVF-03 | `gpio.*`（拡張ヘッダの 8 本の駆動・読み・パターンの出力）、SPI / I2C のマスタの送受信（まずは CSR のビットバング、速度が要れば回路） | ホスト・API（ゲートウェアは既存の gpio） | なし | — | B | 1.5 日 | 新規 `probe_gpio.py`（並行可） |
| U42 | SPI Flash の直接書き込み | SEG-07, SEG-40, CTX-09 | プローブの SPI マスタで外付け SPI NOR を JEDEC の手順で読み書き | ゲートウェア・ホスト | U41（ビットバングで先に） | 配線のみ | C | 2 日 | 新規 `rtl/spi/`、`spiflash.py` |
| U43 | 変数の観測の拡充 | CUBE-03, CUBE-11, CUBE-12, CUBE-13, UCP-01, UCP-03, UCP-05, CTX-01 | 条件（クロストリガ・多段トリガ・変数の値）で観測を始める・止める・プリトリガ、ポインタをたどる式（周期ごとに番地を引き直す）、ターゲット側バッファの吸い出し方式、走行中の書き込みの実機確認、100 kHz の確認 | ホスト・API（ゲートウェアは確認） | なし | — | B | 1.5 日 | `probe_watch.py`、`sampler.py` |
| U44 | デバッグの出来事のフック | SEG-60, IAR-32 | 停止・リセット・書き込みの前後に、式の評価・記録の印・任意の API 呼び出しを登録（デーモン側で実行。AI と人が同じフックを見る） | ホスト・API | なし | — | B | 1 日 | `api/daemon.py`、新規 `hooks.py` |
| U45 | RTOS の対応の拡大 | SEG-65, TZ-14, CUBE-08, OOCD-09, VSC-09, UCP-04, TRX-01, TRX-03, ZEP-04, CTX-05 | ThreadX・RTX5・µC/OS-III・NuttX と、Zephyr の完全化（オブジェクト・スタック）。試験用ファームを足す | ホスト | U08 のモデル | — | C | 3 日 | `rtos/` に新規ファイル（並行可） |
| U46 | ELF の自動再読込 | SEG-52 | 読み込んだ ELF の更新を検出し、再読込と通知（画面と AI に） | ホスト | なし | — | B | 0.5 日 | `probe_inspect.py` |
| U47 | NAT 越しの中継 | SEG-33 | 中継サーバ（WebSocket の中継）か SSH のトンネルの手順 | ホスト | なし | — | C | 2 日 | 新規 |
| U48 | GDB のトレースポイント | GDB-03, GDB-04 | GDB のトレースポイントのパケットを、ログ・ブレークポイントと DWT のデータトレース（U11）に対応付ける | ホスト | U04、U11 | — | C | 2 日 | `gdb_server.py` |
| U49 | プローブの個体名と版の申告 | SEG-14, SEG-37 | ケイパビリティにビルドの版・日時・git の版を足す、ホストの設定でニックネーム | ゲートウェア（caps）・ホスト | なし | — | C | 0.5 日 | `caps.py`、`caps_rom` の生成 |
| U50 | RTT の高速化 | SEG-22 | RTT の速度の実測、FPGA のサンプラで書き込み位置を監視して変化したときだけ読む（ホストのポーリングを減らす）、まとめ読み | ホスト（ゲートウェアは既存のサンプラ） | なし | — | B | 2 日 | `rtt.py`、`probe_observe.py` |
| U51 | SWO の Manchester と高速化 | SEG-24, IAR-01, ORB-03 | Manchester の受信回路、NRZ の最高速度の実測 | ゲートウェア | なし | — | C | 2 日 | `rtl/debug/swo_receiver.vhd` |
| U52 | UART の拡充 | SEG-26 | HW フロー制御、2 本目の CDC-ACM | ゲートウェア・USB ディスクリプタ | なし | 配線のみ | C | 2 日 | `rtl/serial/`、`fprobe_gw/usb/fprobe_device.py` |
| U53 | ターゲットの関数の呼び出し | WIN-08 | `debug.call`: 停止中に関数を引数付きで実行し戻り値を得る（戻り先に BKPT の踏み台、時間切れで止めて元に戻す）。単体テスト・AI の試行に使う | ホスト・API | なし | — | B | 1.5 日 | `debug.py`、`probe_inspect.py` |

---

## 3. 画面（UI）の要件の一覧

API にあるのに画面が無いため △ になっているもの（判定「△（画面なし）」36 件と、△ の理由に画面を挙げたもの）を、画面の要件としてまとめる。後の UI 設計の入力にする。UI 調査（`2026-10-17-debug-ui-ux-survey.md`）の結論どおり、**G01 の共通タイムラインを骨格にして、他のビューを時刻でつなぐ**ことを前提にする。今の画面は `host/ui/`（タブ: 作業・デバッガ・波形）。

| 画面 | 名前 | 関係する ID | 使う API（既存） | 要件 | 優先度 |
|---|---|---|---|---|---|
| G01 | 共通タイムライン | SEG-56, SEG-63, IAR-07, KEIL-04, T32-05, WIN-02, GHS-03, TZ-01, MCUX-03, MPLB-03, PFT-01, TRX-02, FRT-01, YDC-01, YDC-02 | `recording.density/waveform/events/find/mark`、`la.view`、`rtos.timeline.get`、`watch.series`、`session.activity.list` | 段を縦に積む（ロジアナの波形・DAP の転送・UART / SWO / RTT の行・RTOS のタスク・割り込み・変数・電圧・印・AI と人の操作）。カーソル 3 種（選択・マウス・基準点）と差分、縮図（ミニマップ）と異常の印、キーボードで次/前の出来事へ、時刻から他のビュー（ソース・メモリ・解読表）へ飛ぶ | A |
| G02 | デバッガの画面の拡充 | KEIL-07, CUBE-02, E2S-01, VSC-05, VSC-06, VSC-10 | `debug.disasm.*`、`target.memory.view`（compare）、`target.svd.*`・`target.peripheral.*`、`debug.evaluate`・`debug.frame_variables`・`debug.write_expression`、`debug.breakpoint.*`（condition・ignore_count・log） | ソースと逆アセンブルの混在表示、メモリの 16 進表示（変化の印・シンボル名・走行中の更新）、周辺レジスタの木（フィールドの名前で編集、読むと変わるものの警告）、式のウォッチとローカル変数（構造体の展開・書き込み）、ブレークポイントの条件・ログの編集、行単位のステップ（U01）、ベクタキャッチ（U02） | A |
| G03 | ライブウォッチとグラフ | SEG-54, SEG-70, IAR-12, KEIL-05, KEIL-07, TZ-05, CUBE-03, CUBE-04, MCUX-12, E2S-07, CCS-08, MPLB-01, MPLB-03, VSC-02, VSC-04 | `watch.set/values/series/status`、記録の stream "sample"、`debug.evaluate`（配列） | 変数の時系列グラフ（複数・軸の自動・XY）、最新値の表、取得の周期と取りこぼしの表示、配列の波形表示、条件での取得開始（U43）、CSV 書き出し | A |
| G04 | 端末 | SEG-61, SEG-71, IAR-11, CUBE-24, MCUX-05, MPLB-01, NRF-06, VSC-03, VSC-11 | `rtt.read`（since で画面用）・`rtt.write`・`rtt.status`、`swo.text/decode`、`uart.read/write`、通知 `uart.data`・`swo.data` | RTT のチャンネル・SWO の ITM ポート・セミホスティング・UART を端末として表示、送信欄、制御コード（色・消去）、仮想端末、時刻の表示（デバイスの時刻）、ログの保存、行から G01 の時刻へ飛ぶ | A |
| G05 | RTOS のビュー | SEG-53, SEG-63, SEG-66, WIN-05, TZ-01, TZ-02, NRF-05, VSC-09, FRT-01, FRT-02 | `rtos.info/tasks/backtrace/read`、`rtos.timeline.start/stop/get` | タスクの一覧（状態・優先度・待ち・スタックの使用量とあふれ）、タスクごとの呼び出し履歴、タスクの時間軸（G01 の段）、CPU 負荷の推移（U09）、オブジェクト（U08） | B |
| G06 | 性能解析・カバレッジのビュー | SEG-49, SEG-57, IAR-08, IAR-16, WIN-02, CUBE-04, MCUX-02, MCUX-03, FRT-01 | `profile.result/coverage/exceptions`、`timing.measure` | 関数・行の割合の表（信頼区間付き）、ソースの余白に当たりの数と色、カバレッジの色分け、割り込みの統計、区間の分布図（ヒストグラム）と外れ値から時刻へ飛ぶ | B |
| G07 | 記録の検索・統計 | T32-04, TZ-03, FRT-01 | `recording.find/intervals/events/mark/list/open/export`、`timing.measure` | 構造化された検索（stream・kind・値・本文）の結果を時間軸に印、区間の統計と生データの往復、調査の印とメモ（AI と人）、記録の一覧・開く・書き出し | B |
| G08 | 故障のビュー | IAR-26, CUBE-05 | `target.fault`、`debug.run_until_halt` の要約、`debug.backtrace` | 故障で止まったら自動で開き、原因（CFSR・HFSR のビットの意味）・故障の番地・故障した行・その時のレジスタ・呼び出し履歴を示し、ソースへ飛ぶ | A |
| G09 | ロジアナの解読・測定・トリガの画面 | （△ の理由ではないが、SAL-01・DSV-03・WVF-05 の機能を人が使う入口） | `la.decode`、`xtrig.route/set_match/set_sequence`、`la.capture` 系、U19 の `la.measure` | 波形の上に解読の注釈、解読の表（誤りだけ・件数）、多段トリガとクロストリガの設定（段の図）、カーソル間の測定 | A |
| G10 | 操作パネル | CUBE-15, MPLB-08, ORB-09, UCP-01, UCP-02 | `watch.*`、`debug.write_expression` | ゲージ・数値・ボタン・スライダを並べて変数を読む・書く。配置の保存。遠隔（`--lan`）で見る | C |
| G11 | 書き込みの画面 | SEG-39 | `target.flash`、U03 の消去・検証・進捗 | ファイルの選択、消去・検証の選択、進捗と速度、Pack の選択 | B |

---

## 4. まとめ

### 4.1 件数

| 判定 | 商用（202） | メーカー・OSS 等（242） | 合計（444） |
|---|---|---|---|
| ◯（満たす・上回る） | 20 | 42 | 62 |
| △（一部） | 75 | 84 | 159 |
| △（画面なし） | 16 | 20 | 36 |
| ×（無い） | 45 | 57 | 102 |
| 対象外 | 32 | 28 | 60 |
| 要ハード | 14 | 11 | 25 |

- 対象外を除いた 384 件のうち、◯ は 62 件（16%）、△ と △（画面なし）は 195 件（51%）、× は 102 件（27%）、要ハードは 25 件（7%）。
- △（画面なし）36 件は画面（§3）だけで ◯ に上がる。△ のうち多くは「API はあるが周辺の一部が無い」もので、単位 1 つで ◯ になる。
- × の大きな塊は、命令トレース（U34〜U36、34 件に関係）、マルチコアと他のコア（U31〜U33）、トレース形式・ログ形式の互換（U13〜U16）、量産・電力以外の実行制御の細部（U01・U02・U28）。
- 要ハード 25 件のほとんどは電流（U38）。INA226 を FPGA の I2C に付ければ、共通の時計で「電流 × コード × ロジアナ」を持つ、他に無い形になる。
- fprobe が既に上回っている所: ロジアナとデバッガが同じ時計（DAP の出来事・ロジアナ・SWO・UART・電圧・RTOS を 1 本の記録に）、クロストリガと多段トリガ、FPGA の変数の周期読み、MCP と人・AI の協働、FPGA の差し替えでの機能追加。

### 4.2 優先度 A の単位

| 単位 | 名前 | 目安 | 満たす ID の数 |
|---|---|---|---|
| U01 | ソース行単位のステップ | 1.5 日 | 3 |
| U02 | ベクタキャッチとリセットの種類 | 0.5〜1 日 | 5 |
| U03 | Flash 操作の拡充（消去・検証・保存・速度の計測・Pack） | 2 日 | 9 |
| U04 | デーモン内蔵の GDB サーバ（IDE と解析の同時利用） | 3 日 | 14 |
| U06 | スナップショット（保存・オフライン解析・コアファイル） | 3 日 | 7 |
| U07 | CI 実行コマンド（書き込み→実行→終了コード） | 1.5 日 | 6 |
| U08 | RTOS のカーネルオブジェクトの一覧 | 2 日 | 8 |
| U09 | RTOS の時間軸の解析の拡張（負荷の推移・ジョブ・優先度逆転） | 2 日 | 10 |
| U11 | SWO の DWT・ITM 設定の API（データトレース） | 2 日 | 9 |
| U13 | 記録を Perfetto 等へ書き出す | 1.5 日 | 5 |
| U17 | テストポイントの API・MCP | 2 日 | 17 |
| U19 | ロジアナの測定 | 1 日 | 1 |
| U28 | Flash 上の無制限ブレークポイント | 3 日 | 3 |

画面の優先度 A: G01（共通タイムライン）、G02（デバッガの画面の拡充）、G03（ライブウォッチとグラフ）、G04（端末）、G08（故障のビュー）、G09（ロジアナの解読・トリガの画面）。

並行の目安: A のうち U03・U04・U06・U07・U11・U13・U17・U19 は新しいファイルが中心で並行に作れる。U01・U02・U28 は `debug.py`、U08・U09 は `rtos/` と `probe_rtos.py` を共有するので合流の順を決める。API の JSON と MCP の INSTRUCTIONS は最後にまとめて合流する（§2.1）。
