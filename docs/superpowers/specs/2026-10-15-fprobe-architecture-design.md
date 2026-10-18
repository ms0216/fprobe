# fprobe — FPGA マルチファンクション・デバッグプローブ 全体設計

- 日付: 2026-10-15
- 状態: 承認済み方針（案1）に基づく全体設計。各マイルストーンは本書を親として個別の実装計画を持つ。
- コードネーム: **fprobe**（FPGA probe）。Python パッケージ名・USB 製品名に使用。

## 1. 目的と成功基準

### 1.1 目的
SWD/JTAG デバッガ・ロジックアナライザ・SWO/UART・クロストリガを 1 台に統合し、
PC（特に生成 AI エージェント）からターゲットを多角的に制御・観測できるデバッグプローブを作る。
優先順位: (1) 解析力 (2) 日常デバッガとしての実用性 (3) 人間にとっての使いやすさ。

### 1.2 成功基準（測定可能なもの）

| 項目 | 目標 | 比較対象 |
|---|---|---|
| 標準ツール互換 | pyOCD / OpenOCD / probe-rs が CMSIS-DAP v2 として**ドライバ追加なし**（Windows）で認識し、Nucleo F401RE に Flash 書き込み・GDB デバッグできる | ST-LINK/V2-1 |
| JTAG | OpenOCD で JTAG 経由の Cortex-M デバッグ（Nucleo）。Cortex-A / ARM11（Pi 2 / Zero）は実機入手時に検証 | — |
| SWD クロック | 1 kHz〜25 MHz 可変（上限はターゲット依存） | — |
| メモリ読み出しスループット | DAP_TransferBlock で **≥ 1.5 MB/s**（SWCLK 25 MHz 時） | ST-LINK/V2-1 |
| コマンド往復遅延 | DAP_Info 往復 **≤ 250 µs**（中央値） | — |
| ロジアナ（バッファ） | **16 ch @ 100 MS/s**、深さ **≥ 32 M サンプル**（DDR3）、プリトリガ任意比率 | — |
| ロジアナ（ストリーミング） | 16 ch @ **≥ 10 MS/s 連続**（無圧縮）、変化点圧縮時はそれ以上 | — |
| トリガ→ターゲット停止 | ロジアナトリガ成立から SWD 停止要求開始まで **≤ 1 µs** | — |
| 時刻相関 | 全イベント（波形・DAP 操作・UART・SWO・トリガ）が**同一タイムベース**（10 ns 分解能）で記録される | — |
| AI 操作 | MCP サーバ経由で、停止/再開/ステップ/レジスタ・メモリ R/W/Flash/リセット/波形取得/UART 送受信 が行える | — |
| 移植性 | コア RTL 無変更で、ボード層とホストリンク層の差し替えのみで別ボードにビルドできる構造 | — |

### 1.3 非目標（当面やらない）
- 1.8V など 3.3V 以外のターゲット（レベル変換ハードが無いため）。I/F は電圧非依存に設計し、将来の追加を妨げない。
- ETM 並列トレース、Cortex-A の CoreSight トレース。
- sigrok 用ネイティブドライバ（VCD 出力で代替。必要になったら追加）。
- Ethernet ホストリンク（ホストリンク抽象の 2 つ目の実装として将来追加）。

## 2. ハードウェア前提（Tang Primer 20K Dock, rev 3713/3714）

| 資源 | 詳細 | 用途 |
|---|---|---|
| FPGA | GW2A-LV18PG256C8/I7（20,736 LUT4, BSRAM 828 Kbit） | 全機能 |
| USB-OTG（J8） | USB3317 ULPI HS PHY, 60 MHz CLKOUT → `T15`。VBUS は基板 +5V に直結（単独給電可） | **主ホストリンク** |
| USB-JTAG（J1） | BL702（FT2232 互換, FS）。FPGA JTAG + UART（FPGA `T13`=RX / `M11`=TX） | 開発用（ビットストリーム書き込み・デバッグコンソール） |
| DDR3 | 128 MB, x16 | ロジアナ深バッファ |
| Ethernet | RTL8201F 10/100 RMII | 将来の第 2 ホストリンク |
| 発振器 | 27 MHz → `H11` | システムクロック源 |
| PMOD ×4 | 各 8 I/O, 3.3V | デバッグポート・ロジアナ入力 |
| LED ×6 / ボタン ×5 | | 状態表示 |

注意:
- `F10` は USB3317 RESET と RTL8201F RESET で**共用**。ボード層で単一のリセット資源として扱う。
- rev 3714 で SW0 と KEY4 のピンが入れ替わっている。ボード層でリビジョンをパラメータ化する。
- ULPI 60 MHz は Sipeed 実績例に倣い**反転クロック**で受ける（ボード層の責務）。

## 3. 全体アーキテクチャ

### 3.1 層構造

```
┌──────────────────────────── Host PC ─────────────────────────────┐
│ pyOCD / OpenOCD / probe-rs  │  fprobe (Python lib)  │ fprobe-mcp │
│   (CMSIS-DAP v2)            │  terminal (CDC-ACM)   │  (AI)      │
└──────────────┬──────────────────────────┬────────────────────────┘
               │ USB HS (composite)        │
┌──────────────┴──────────────────────────┴──── FPGA ─────────────┐
│ Host link layer   : UsbHsLink (VHDL/ULPI) | SerialLink (将来/小型)│
│                     ── 論理チャンネル (stream) に正規化 ──        │
│ Core layer        : ControlPlane(CSR) | DapProcessor | DebugPort │
│                     LogicAnalyzer | UartBridge | SwoReceiver     │
│                     Timebase | EventFabric | CrossTrigger        │
│ Memory layer      : SampleStore (BSRAM | DDR3 | PSRAM)           │
│ Board layer       : ピン・PLL・ベンダープリミティブ・リセット      │
└──────────────────────────────────────────────────────────────────┘
```

**依存方向は上から下のみ。** Core はホストリンク・メモリ・ボードの具体実装を知らない（インターフェースにのみ依存）。

### 3.2 論理チャンネル（ホストリンク非依存の契約）

コアとホストリンクの境界は「番号付きの双方向/単方向バイトストリーム」に正規化する。

| ch | 名前 | 方向 | 単位 | USB HS での実体 |
|---|---|---|---|---|
| 0 | CONTROL | 双方向 | 要求/応答パケット | fprobe 拡張 IF の Bulk OUT/IN（EP1） |
| 1 | DAP | 双方向 | CMSIS-DAP コマンド/応答パケット | CMSIS-DAP v2 IF の Bulk OUT/IN（EP2） |
| 2 | SWO | 片方向 ↑ | バイト列 | CMSIS-DAP v2 IF の SWO Bulk IN（EP3） |
| 3 | UART0 | 双方向 | バイト列 | CDC-ACM IF（EP4 + 通知 EP5） |
| 4 | CAPTURE | 片方向 ↑ | サンプルブロック | fprobe 拡張 IF の Bulk IN（EP6） |
| 5 | EVENT | 片方向 ↑ | タイムスタンプ付きレコード | fprobe 拡張 IF の Bulk IN（EP7） |

- パケット境界を持つチャンネル（0, 1）は `first/last` 付きストリームで境界を伝える。
- 小型ボード用 SerialLink は同じチャンネルを「COBS フレーム + ch 番号 + CRC16」で多重化する（外付け MCU が USB FS ↔ シリアルを中継）。PC 側はケイパビリティでリンク種別を知る。

### 3.3 クロックドメイン

| ドメイン | 周波数 | 内容 |
|---|---|---|
| `usb` | 60 MHz（ULPI 由来） | USB デバイススタック |
| `sync` | 100 MHz（27 MHz → PLL） | コア全般・タイムベース（10 ns） |
| `sample` | 100〜200 MHz（PLL） | ロジアナ入力段 |
| `mem` | DDR3 コントローラ指定 | SampleStore（DDR3 実装時） |

ドメイン間は**非同期 FIFO のみ**で接続する（ストリーム境界 = CDC 境界）。単一ビット信号は 2 段同期化器ユーティリティを必ず通す。

## 4. コア層の構成要素

### 4.1 共通基盤（`core/`）
- **Stream**: `<prefix>_valid/ready/data(/last)` の個別ポートで統一（VHDL-2008 では向きの混在したレコードをポートにできないため。M3 で決定）。全ブロック間 I/F はこれか CSR。
- **ControlPlane / CSR**: 32 bit レジスタ空間。レジスタ定義は**Python の単一定義**から (a) ゲートウェアのレジスタ実体 (b) ホスト側 Python 定数モジュール (c) Markdown レジスタ表 を生成する（単一の真実の源）。CONTROL ch のパケット: `READ(addr, n)` / `WRITE(addr, n, data…)` → 応答 `data…, tag, status, done`（詳細は M3 設計 `2026-10-15-m3-control-plane-design.md` §3）。
- **Capabilities ROM**: プロトコル版、ボード ID、各機能の有無・ch 数・バッファ深さ・最大周波数などを TLV で申告。PC 側は必ずこれを読んで動作を合わせる。
- **Timebase**: `sync` ドメインの 64 bit フリーラン（10 ns）。他ドメインへはグレイコード経由で配布。

### 4.2 デバッグポート（`debug/`）
ポートは N 個（パラメータ, Primer は 2）。各ポートは独立した PHY を持ち、`DapProcessor` が選択中のポートを使う。

- **SwdPhy**: 1 トランザクション（要求 8 bit → turnaround → ACK 3 bit → データ 32 bit + パリティ）をハードウェアで実行。生シーケンス（line reset, JTAG→SWD 切替等）用の `sequence` モードを持つ。クロックは `sync` からの分周（イネーブル方式, 25 MHz 上限）。
- **JtagPhy**: TMS/TDI シーケンス、IR/DR スキャン。JTAG-DP 経由の DAP 転送（DPACC/APACC スキャン）をサポート。
- **DapTransferEngine**: CMSIS-DAP の転送意味論（WAIT リトライ、値一致待ち、AP 読み出しのポスティングと RDBUFF 回収、ブロック転送、`TransferAbort`）を PHY 非依存に実装。
- **DapProcessor**: CMSIS-DAP v2 コマンドパケットを解釈して Engine/PHY を駆動し応答を生成する**ハードウェア FSM**。
  - 対応コマンド: `Info, HostStatus, Connect, Disconnect, WriteABORT, Delay, ResetTarget, SWJ_Pins, SWJ_Clock, SWJ_Sequence, SWD_Configure, SWD_Sequence, JTAG_Sequence, JTAG_Configure, JTAG_IDCODE, TransferConfigure, Transfer, TransferBlock, TransferAbort, SWO_Transport, SWO_Mode, SWO_Baudrate, SWO_Control, SWO_Status, SWO_ExtendedStatus, SWO_Data, ExecuteCommands, QueueCommands`。
  - 未対応コマンドは仕様どおり `0xFF` 応答。
  - **設計判断（会話時点の案からの変更）**: 当初は「ソフト CPU でコマンド解釈」としていたが、60 MHz 級のソフト CPU では HS USB とブロック転送の速度目標（≥1.5 MB/s, ≤250 µs）を満たす余裕がないため、**ハードウェア FSM に変更**する。プロトコル拡張性は CSR 生成系と CONTROL ch で確保する。
- **DebugActionInjector**: クロストリガからの「停止（DHCSR 書き込み）」「リセット」要求を、ホスト由来トランザクションの**トランザクション境界で割り込み**実行するアービタ。
- **TransactionMonitor**: 実行された全 DAP トランザクション（ポート, AP/DP, アドレス, 値, ACK）を EventFabric に流す（有効/無効は CSR）。アドレス/値の一致比較器を持ち、クロストリガ源になる（「この番地に書いたら波形トリガ」）。
- **TargetControl**: nRESET（オープンドレイン）、nTRST。

### 4.3 ロジックアナライザ（`la/`）
- **SampleFrontend**: 入力同期化、`sample` ドメインでサンプリング、分周（サンプルレート可変）。入力源は PMOD ピン＋**内部プローブ**（デバッグポート信号・UART 線を内部で直接観測でき、外部配線不要）。
- **TriggerUnit**: 多段シーケンストリガ（段数パラメータ, Primer は 4）。各段: 値/マスク、エッジ、カウント、次段遷移。外部（クロストリガ）からのトリガ入力も受ける。
- **Compressor**: 無圧縮 or 変化点符号化（値 + 経過サンプル数）。ストリーミング時の実効帯域を上げる。
- **CaptureController**: プリトリガ・リングバッファ制御、トリガ位置記録（タイムベース時刻とサンプル番号の対応を保存）、読み出し。
- モード: **Buffered**（SampleStore へ記録 → 後で読み出し）／**Streaming**（CAPTURE ch へ直接）。

### 4.4 SWO / UART（`serial/`）
- **SwoReceiver**: NRZ（UART）および Manchester。ボーレート自動/指定。CMSIS-DAP の SWO コマンド群と SWO ch にマップ。受信バイトはタイムスタンプ付きで EventFabric にも出せる。
- **UartBridge**: ターゲット UART ↔ CDC-ACM。ボーレートは CDC の `SET_LINE_CODING` に追従。送受信バイトを EventFabric に出せる（AI が「このログが出た時刻に何が起きたか」を相関できる）。

### 4.5 イベントとクロストリガ（`trigger/`）
- **EventFabric**: 各源からの `EventRecord{timestamp(64), source(8), kind(8), payload(可変)}` を優先度付きで合流し EVENT ch へ。溢れた場合は欠落数レコードを挿入（**黙って捨てない**）。
- **CrossTrigger**: 源（LA トリガ, DAP 一致, UART パターン, SWO パターン, 外部トリガ入力ピン, ホスト指令）× 動作（LA トリガ, ターゲット停止, リセットパルス, イベント記録, 外部トリガ出力ピン）のマトリクス。CSR で設定。全経路が `sync` ドメインで 1〜数クロック。

## 5. ホストリンク層（`hostlink/`）

### 5.1 UsbHsLink（Primer 20K）
- **自作の VHDL USB 2.0 デバイススタック**（ULPI PHY インターフェース → パケット層（PID/CRC5/CRC16）→ トランザクション層（エンドポイント別のデータトグル・ACK/NAK）→ 制御転送（標準・クラス・ベンダー要求）→ ディスクリプタ ROM）。
  - 設計判断: 当初は LUNA（Amaranth）を予定したが、RTL を **VHDL に統一**する方針（2026-10-15 ユーザー要望）により自作する。ベンダー暗号化 IP は使わない。
  - Primer 20K の USB3317 で HS が動く実績は Sipeed 公式例（LUNA 生成物）で確認済み。ULPI クロックの反転受けなど PHY 周りの知見はそこから得る。
  - 検証は cocotb 上の **USB ホスト／ULPI PHY 振る舞いモデル**（Python）で行い、列挙・各転送種別・エラー（CRC 不一致、タイムアウト、トグル不一致）を網羅する。
- 複合デバイス構成:
  - IF0: **CMSIS-DAP v2**（Vendor class, EP2 OUT/IN = DAP, EP3 IN = SWO）。インターフェース文字列に `CMSIS-DAP` を含める。
  - IF1: **fprobe 拡張**（Vendor class, EP1 OUT/IN = CONTROL, EP6 IN = CAPTURE, EP7 IN = EVENT）
  - IF2–3: **CDC-ACM**（UART0）
- **MS OS 2.0 ディスクリプタ**（BOS）で IF0/IF1 に WinUSB を自動割当て → Windows でドライバ不要。
- VID/PID: 開発中は pid.codes のテスト用 `1209:0001`。公開時に正式 PID を取得する。
- シリアル番号: FPGA の固有 ID（取得可能なら）またはビルド時指定。

### 5.2 SerialLink（将来・小型ボード）
外付け MCU（Pico / BL702 / RP2040）が USB FS を終端し、論理チャンネルを COBS フレームで FPGA と多重化。本書では契約のみ定義し、実装は移植マイルストーンで行う。

## 6. メモリ層（`memory/`）

`SampleStore` インターフェース: 書き込みストリーム（ワード列）、読み出し要求（開始アドレス, 長さ）→ 読み出しストリーム、容量申告。

| 実装 | 対象 | 容量 |
|---|---|---|
| `BramSampleStore` | 全ボード（フォールバック） | 数十 KB |
| `Ddr3SampleStore` | Primer 20K | 128 MB |
| `PsramSampleStore` | Nano 9K / 4K（将来） | 8 MB |

DDR3 コントローラの実装は M8 で **LiteDRAM（GW2A PHY, LiteX の Primer 20K 対応実績）** と **Gowin DDR3 IP** を比較し、`SampleStore` の裏に隠す。シミュレーションは常に振る舞いモデルで行う。

## 7. ボード層（`platform/`）

ボードごとに 1 ディレクトリ（`rtl/boards/<board>/`）: トップ entity、ピン制約（.cst）、ボード定数パッケージ、クロック/PLL、ULPI 反転クロック等のプリミティブ、リセット、リビジョン差分、そのボードで使う HostLink/SampleStore 実装の選択、パラメータ（ポート数・LA ch 数・トリガ段数）。

| ボード | HostLink | SampleStore | 想定ティア |
|---|---|---|---|
| Tang Primer 20K Dock | UsbHsLink | Ddr3 | フル |
| Tang Nano 9K | SerialLink（BL702 or 外付け Pico） | Psram | 中位（将来） |
| Tang Nano 4K | SerialLink | Psram（HyperRAM） | 中位（将来） |
| Shrike-Lite | RP2040 側で CMSIS-DAP、FPGA は LA のみ | Bram | 最小（将来・要評価: FPGA 規模が極小） |

## 8. ピン割り当て（Primer 20K, 初版）

| PMOD | 用途 | 信号（8 本） |
|---|---|---|
| PMOD0 | デバッグポート A | SWCLK/TCK, SWDIO/TMS, SWO/TDO, TDI, nRESET, nTRST, UART_TX, UART_RX |
| PMOD1 | デバッグポート B | 同上 |
| PMOD2 | LA ch0–7 | |
| PMOD3 | LA ch8–15 | |

具体的な FPGA ピン番号は M1 で回路図 p7/p8 とシルクから確定し、ボード層に記載する。外部トリガ入出力は余り I/O（GPIO ヘッダ）に割り当てる。

## 9. ホストソフトウェア（`host/`）

- **`fprobe`（Python パッケージ, ゲートウェア開発ツールに非依存）**
  - `transport`: pyusb + libusb（`libusb-package` で DLL 同梱）。デバイスはシリアル番号で特定。
  - `regs`: ゲートウェアから**生成された**レジスタ定義。
  - `device`: ケイパビリティ取得、CSR R/W、チャンネル I/O。
  - `debug`: **pyOCD をライブラリとして利用**（CMSIS-DAP v2 互換なので pyOCD がそのまま本機を扱える）。ポート選択や拡張機能は fprobe 側で補う。JTAG/Cortex-A は OpenOCD プロセス管理で対応。
  - `la`: キャプチャ設定・トリガ・取得 → NumPy 配列 / VCD 出力。
  - `events`: EVENT ch の受信とデコード、時刻相関クエリ。
  - `uart`: CDC-ACM の COM を pyserial で扱う薄いラッパ（イベント相関付き）。
  - CLI: `fprobe info | la capture | uart | events ...`
- **`fprobe-mcp`**: MCP サーバ。AI 向けツール（connect, halt, resume, step, read_registers, read_memory, write_memory, flash, reset, la_capture, la_set_trigger, uart_read, uart_write, events_query, cross_trigger_set など）。出力は AI が扱いやすい構造化テキスト/JSON。

## 10. 開発・検証方式

- **言語/ツール**: RTL は **VHDL-2008 に統一**（2026-10-15 ユーザー要望。当初案の Amaranth から変更）。**Gowin EDA（gw_sh, Education 1.9.11.03, `-vhdl_std vhd2008`）** で合成・配置配線。ビルド・書き込み・PLL 計算・CSR 生成などの**開発ツールは Python**（RTL ではないため統一の対象外）。
- **単体テスト**: 全 VHDL モジュールを **GHDL 6 + cocotb 2（pytest から起動）** で検証。プロトコル系は**振る舞いモデル**（Python）を相手にテスト（SWD ターゲットモデル: DP/AP/メモリ、JTAG TAP モデル、UART モデル、USB ホスト／ULPI PHY モデル、SampleStore モデル）。
- **生成物の一元管理**: CSR レジスタ定義・ケイパビリティ・USB ディスクリプタは Python の単一定義から VHDL パッケージ・ホスト側 Python 定数・Markdown 表を生成する。生成 VHDL はコミットし、「定義と生成物が一致する」ことを pytest で検査する（ビルドに Python 生成段を挟まない）。
- **結合テスト（シミュレーション）**: DapProcessor + SwdPhy + SWD ターゲットモデルに実 CMSIS-DAP パケット列を流し、応答を照合。
- **実機テスト（HIL）**: Nucleo F401RE を基準ターゲットとし、同じ操作を **ST-LINK/V2-1 と fprobe の両方**で行い結果を照合（メモリダンプ一致、Flash 書き込み検証）。速度目標は自動計測スクリプトで測る。
- **安全装置**: FPGA 書き込みは安全ラッパ経由でのみ行う。ボード定義の USB シリアル番号（Primer: `FACTORYAIOT_PRO`）の機器が接続されている USB ポート経路を OS から引き、D2XX の位置 ID がそのポート経路で説明文（`USB Debugger A`）も一致するチャンネルが**ちょうど 1 つ**のときだけ対象とし、さらに書き込み前に **IDCODE**（Primer: `0x0000081B`）を照合する（PYNQ-Z2・同名の Tang Nano 9K 等への誤書き込み防止。D2XX からは Primer のシリアルが `A`/`B` に見えるため、シリアル番号は OS の PnP 情報から引く）。
- **リポジトリ構成**:

```
gateware/rtl/        VHDL-2008（common/ core/ debug/ la/ serial/ trigger/ usb/ memory/ boards/<board>/ designs/）
gateware/fprobe_gw/  Python 開発ツール（toolchain/ = ビルド・書き込み・PLL・シミュレーション起動、boards/ = ボード定義、designs = デザイン登録）
gateware/tests/      pytest + cocotb（モデルは tests/models/）
host/                Python パッケージ fprobe（+ fprobe_mcp）
host/tests/
tools/               build・program（安全ラッパ）・計測スクリプト
docs/                仕様・計画・生成レジスタ表
```

## 11. マイルストーン

各マイルストーンは「実機で検証可能な成果」で終わる。実装計画はマイルストーンごとに `docs/superpowers/plans/` に作成する。

| M | 内容 | 完了条件 |
|---|---|---|
| M0 | リポジトリ・ツールチェーン・安全な書き込みラッパ・テスト基盤 | pytest が走る。誤ったシリアルへの書き込みを拒否する |
| M1 | ボード層と VHDL→Gowin ビルドフロー・cocotb シミュレーション基盤 | LED 点滅・BL702 UART コンソールが実機で動く。PMOD ピン確定 |
| M2 | UsbHsLink: HS 列挙・複合デバイス・WinUSB 自動割当て | Windows で HS として列挙、ループバックで ≥30 MB/s |
| M3 | Stream 基盤・ControlPlane・CSR 生成・Capabilities・Timebase | ホストから CSR R/W、ケイパビリティ取得 |
| M4 | SwdPhy・DapTransferEngine・DapProcessor | pyOCD/OpenOCD で Nucleo に Flash 書き込み・GDB デバッグ、速度目標達成 |
| M5 | JtagPhy（JTAG-DP） | OpenOCD JTAG で Nucleo をデバッグ |
| M6 | SwoReceiver・UartBridge（CDC-ACM） | SWO printf と UART が PC で見える |
| M7 | LogicAnalyzer（BRAM バッファ）・TriggerUnit・VCD 出力 | Nucleo の GPIO 波形を取得・PulseView で表示 |
| M8 | Ddr3SampleStore・Streaming・Compressor | 深さ・ストリーミング目標達成 |
| M9 | EventFabric・TransactionMonitor・CrossTrigger・Injector | 「LA トリガで停止」「番地書き込みで LA トリガ」が実機で動く |
| M10 | ホストライブラリ完成・CLI・MCP サーバ | AI が MCP 経由で一連のデバッグ作業を完遂できる |
| M11 | （将来）SerialLink + 小型ボード移植、Pico 2 補助、Ethernet、1.8V 対応 | — |

## 12. 未確定事項と決め方

| 事項 | 決める時点 | 方法 |
|---|---|---|
| PMOD の FPGA ピン番号 | M1 | 回路図 p7/p8・シルク照合 |
| 基板リビジョン（3713/3714） | M1 | ボタン動作で判定 |
| DDR3 コントローラ（LiteDRAM / Gowin IP） | M8 | 両方を `SampleStore` 裏で評価 |
| Pico 2 補助の役割 | M11 | 手持ちだけでは電源スイッチ・電流計測ハードが無いため、当面は電圧監視・補助 GPIO 程度。追加部品の要否をその時点で判断 |
