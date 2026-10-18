# 商用デバッグツールの機能調査（競合調査）

- 作成日: 2026-10-17（ファイル名は 2026-10-18 の作業用）
- 目的: fprobe が「J-Link とその周辺ツールの上位互換」を目指すにあたり、商用ツールが利用者に提供している機能を、宣伝文句ではなく**具体的に使える機能の単位**で洗い出す。
- 方法: 各社の製品ページ・公式マニュアル（Web 版）・公式 KB・プレスリリースを Web で確認した。出典欄の URL は確認に使ったページ。
- 表記の約束:
  - 「未確認」= 公式資料で確認できなかった（存在しないという意味ではない）。推測では埋めていない。
  - 数値は出典ページの記載のまま（モデル・世代で変わる）。販売店の記載に頼ったものはその旨を書いた。
  - 分類は次のいずれか: 接続・書き込み / 止めて調べる / ブレークポイント / 止めずに見る / トレース / 時間軸の解析 / RTOS / 性能・カバレッジ / 電力 / 故障解析 / 自動化・連携 / UI・UX / その他

---

## 1. SEGGER（J-Link / J-Trace / Flasher / Ozone / SystemView / J-Scope / RTT Viewer / Embedded Studio）

### 1.1 J-Link プローブ本体とソフトウェアパック

| ID | 分類 | 機能 | 具体的な内容（何ができるか、条件・制約・必要なハード） | 出典 URL |
|---|---|---|---|---|
| SEG-01 | 接続・書き込み | 対応ターゲット I/F | JTAG / cJTAG、SWD / SWO、Microchip ICSP、Renesas FINE、VCOM。Arm・RISC-V・Renesas・Microchip・Silicon Labs・Cadence などのコア、数万デバイスに対応 | https://www.segger.com/products/debug-probes/j-link/ |
| SEG-02 | 接続・書き込み | ターゲット I/F 速度 | 最大 50 MHz（Pro PoE / Pro / Ultra）、15 MHz（WiFi / PLUS / BASE） | https://www.segger.com/products/debug-probes/j-link/models/model-overview/ |
| SEG-03 | 接続・書き込み | RAM へのダウンロード速度 | 最大 4.0 MB/s（Pro PoE / Pro / Ultra）、1.0 MB/s（WiFi / PLUS / BASE）。ピーク値で、I/F・クロック・ホストで変わる | https://www.segger.com/products/debug-probes/j-link/models/model-overview/ |
| SEG-04 | 接続・書き込み | 速度最適化された Flash ローダ | SEGGER が各デバイス用に Flash ローダを作成・最適化。例: STM32F417 512KB を J-Link Commander で 2.1 s（244 KB/s）、同条件で ULINKpro 60 KB/s、I-jet 27 KB/s、ST-LINK 15 KB/s（SEGGER 自身の比較）。内蔵 Flash で 364〜988 KB/s、QSPI NOR で最大 1086 KB/s の実測表あり | https://www.segger.com/products/debug-probes/j-link/technology/flash-download/ |
| SEG-05 | 接続・書き込み | 外部 Flash（メモリマップ QSPI NOR）へのダウンロード | メモリマップ QSPI NOR はカスタムアルゴリズム無しで IDE から書き込み・RAM のような書き込み・Flash ブレークポイントが可能。パラレル CFI NOR も可。シリアル NOR / NAND / DataFlash / EEPROM は J-Flash / Flasher でカスタム RAMCode が必要 | https://www.segger.com/products/debug-probes/j-link/technology/flash-download/ |
| SEG-06 | 接続・書き込み | Device Support Kit（DSK）/ Open Flash Loader | 利用者が非メモリマップ Flash 用の書き込みアルゴリズムを自作して追加できる | https://www.segger.com/products/debug-probes/j-link/technology/flash-download/ |
| SEG-07 | 接続・書き込み | SPI Flash 直接書き込み | CPU を介さず、J-Link から SPI Flash を直接書く（J-Flash SPI）。SPI 速度は最大 50 MHz（上位機）/ 12 MHz | https://www.segger.com/products/debug-probes/j-link/ |
| SEG-08 | 接続・書き込み | 対応電圧 | ターゲット電圧 1.2〜5 V（全モデル）。VTref の自動検出 / 固定値設定（Commander の `VTREF`） | https://www.segger.com/products/debug-probes/j-link/models/model-overview/ / https://kb.segger.com/J-Link_Commander |
| SEG-09 | 接続・書き込み | ターゲットへの給電 | 20 ピンの 5V（pin 19）でターゲットへ給電。Commander の `Power` で入切・既定値化。Pro PoE は PoE 受電し、デバッグ I/F か USB-A でターゲット給電 | https://kb.segger.com/J-Link_Commander / https://www.segger.com/products/debug-probes/j-link/ |
| SEG-10 | 接続・書き込み | ホスト接続 | USB（USB-C）、Ethernet（Pro / Pro PoE）、WLAN（WiFi モデル） | https://www.segger.com/products/debug-probes/j-link/ |
| SEG-11 | 接続・書き込み | 複数デバッガの同時接続・マルチコア | 1 台の J-Link を複数のデバッガ（例: IDE + SystemView）で共有。スキャンチェーン上のコアを選んでマルチコアデバッグ | https://www.segger.com/products/debug-probes/j-link/ |
| SEG-12 | 接続・書き込み | J-Link Script Files（接続スクリプト） | 自動検出で扱えない特殊なターゲットの接続手順（リセット・AP 選択等）をスクリプトで記述 | https://www.segger.com/products/debug-probes/j-link/ / https://kb.segger.com/J-Link_Commander |
| SEG-13 | 接続・書き込み | プローブのモード切替 | `BootMode` で J-Link / CMSIS-DAP v1 / v2 のファームウェア動作を選択 | https://kb.segger.com/J-Link_Commander |
| SEG-14 | 接続・書き込み | シリアル番号・ニックネームで個体指定 | `-USB <SN/Nickname>`、`-IP <addr/SN/nickname>`、`SelectProbe`、`SetNickname` で複数台を確実に区別 | https://kb.segger.com/J-Link_Commander |
| SEG-15 | ブレークポイント | Unlimited Flash Breakpoints | HW ブレークポイントを使い切ると Flash のセクタを書き換えてソフトブレークポイントを置く。RAM 常駐ヘルパで高速化、同一セクタのまとめ書き・キャッシュ・ステップ中は HW BP 優先・命令セットシミュレータで書き換え回数を削減。外部 QSPI など HW BP が置けない領域でも有効。PLUS 以上（BASE は要アップグレード）、ライセンスはプローブ単位 | https://www.segger.com/products/debug-probes/j-link/technology/flash-breakpoints/ |
| SEG-16 | ブレークポイント | ウォッチポイント・ベクタキャッチ | `SetWP`/`ClearWP`、`VCatch`（ベクタキャッチ）、`SetBP`/`ClearBP` | https://kb.segger.com/J-Link_Commander |
| SEG-17 | 止めて調べる | 低レベルの実行制御・レジスタ・メモリ | Halt / Go / Step / IsHalted / WaitHalt、リセット種別の選択（`RSetType`）、レジスタ R/W、8/16/32/64 bit メモリ R/W、停止理由（`MoE`）表示 | https://kb.segger.com/J-Link_Commander |
| SEG-18 | 止めて調べる | CoreSight DP / AP の直接アクセス | `ReadDP`/`WriteDP`/`ReadAP`/`WriteAP`、CP15 R/W、EmbeddedICE レジスタ R/W | https://kb.segger.com/J-Link_Commander |
| SEG-19 | その他 | JTAG 低レベル操作 | IR/DR/生ビット列の書き込み、スキャンチェーン長の測定（`MS`）、TAP リセット、nTRST、`JTAGConf` で前後の IR/DR ビット指定 | https://kb.segger.com/J-Link_Commander |
| SEG-20 | その他 | ピン単位の制御と状態読み出し | TCK / TDI / TMS / TRST / RESET を個別に High/Low、TCK にクロック出力、RTCK 応答時間測定、全ピン状態（電源電圧含む）の読み出しでハード故障を検出 | https://kb.segger.com/J-Link_Commander / https://www.segger.com/products/debug-probes/j-link/ |
| SEG-21 | その他 | 速度測定コマンド | ターゲット R/W 速度（`TestWSpeed`/`TestRSpeed`）、CPU クロック測定（DWT 使用、`TestCSpeed`）、ホスト〜プローブ間ネットワーク速度 | https://kb.segger.com/J-Link_Commander |
| SEG-22 | 止めずに見る | RTT（Real Time Transfer） | デバッグ I/F（2 線 SWD でも可）経由で、走行中のターゲットと双方向通信。上り/下りチャネル複数、ブロッキング / 非ブロッキング（既定は非ブロッキングでデバッガ無しでも止まらない）。1 行 1 µs 以下、512 B バッファで約 2 MB/s（高速機）/ 約 0.5 MB/s（通常機）。ROM 約 500 B、RAM 24 B + チャネル毎 24 B + バッファ。制御ブロックは IDE からアドレス通知、または J-Link がバックグラウンドで RAM を自動探索（`SetRTTSearchRanges` / `SetRTTAddr`）。Cortex-M/R/A・RISC-V・RX 等、バックグラウンドメモリアクセスできるターゲット | https://www.segger.com/products/debug-probes/j-link/technology/about-real-time-transfer/ |
| SEG-23 | 自動化・連携 | RTT の Telnet サーバ・RTT Client・RTT Logger | 接続中は localhost:19021 の Telnet で RTT に接続（ポート変更可）。RTT Client は自動再接続、RTT Logger は上りチャネル 1 をファイルへ（ソースは SDK に同梱） | https://www.segger.com/products/debug-probes/j-link/technology/about-real-time-transfer/ |
| SEG-24 | 止めずに見る | SWO 受信 | SWO 最大 100 MHz（上位機）/ 30 MHz。Commander の `SWOStart`/`SWORead`/`SWOView` 等で取得・解析。GDB Server 経由で端末出力・ITM・PC サンプルを転送 | https://www.segger.com/products/debug-probes/j-link/models/model-overview/ / https://kb.segger.com/J-Link_Commander / https://www.segger.com/products/debug-probes/j-link/tools/j-link-gdb-server/about-j-link-gdb-server/ |
| SEG-25 | 止めずに見る | HSS（High Speed Sampling） | 変数のアドレスを一定周期でバックグラウンド読み出し（ターゲットのコード変更不要）。上位機は I/F 帯域だけが上限（1 変数で通常 10 kHz 超）、下位機は 1 変数 1 kHz 保証（I/F ≥ 1 MHz） | https://www.segger.com/products/debug-probes/j-link/models/model-overview/ |
| SEG-26 | 止めずに見る | VCOM（仮想 COM、CDC-ACM） | プローブ内蔵の USB-UART。上位機 10 MBd、下位機 115200 Bd。出荷時無効、Configurator で有効化。4 線 JTAG 時は pin 5 を TDI に使うため不可。HW フロー制御設定（`SetHWFC`） | https://www.segger.com/products/debug-probes/j-link/ / https://kb.segger.com/J-Link_Commander |
| SEG-27 | 止めずに見る | Monitor Mode Debugging | Cortex-M3/M4/M7 で、コアを止めずに DebugMonitor 例外のハンドラ内で停止状態を作る。DebugMonitor より優先度の高い割り込み（モータ制御・BLE 等）は動き続ける。C_DEBUGEN をクリアする必要、ハンドラ（JLINK_MONITOR.c）をターゲットに組み込む。Cortex-M0 は不可 | https://kb.segger.com/Monitor_Mode_Debugging / https://www.segger.com/products/debug-probes/j-link/ |
| SEG-28 | トレース | オンチップトレース（ETB / MTB） | J-Link で ETB（Cortex-M3/M4/M7/M8・Cortex-A/R、実装していれば）と MTB を読み出し。ETM（トレースポート）は J-Trace が必要 | https://www.segger.com/products/debug-probes/j-link/ |
| SEG-29 | トレース | STRACE | Commander の `STraceStart`/`STraceStop`/`STraceRead` で簡易トレースを制御・取得。詳細仕様は未確認 | https://kb.segger.com/J-Link_Commander |
| SEG-30 | 電力 | 電流計測（Power Profiling） | J-Link Ultra V4 / Pro V4 / J-Trace PRO V2 で、pin 19 の 5V 給電経路のターゲット電流を最大 100 kHz（200 kSa/s）、分解能 50 µA で計測。Ozone の Power Graph に表示（SysTick の 1 ms 割り込みが見える例）。範囲は未確認。Commander の `Calibrate` で校正、`PowerTrace` で取得 | https://www.segger.com/products/debug-probes/j-link/technology/power-profiling/ / https://kb.segger.com/J-Link_Commander |
| SEG-31 | 自動化・連携 | J-Link Commander（CLI） | 接続確認・実行制御・メモリ・Flash 書き込み（`LoadFile` は .bin/.elf/.hex/.mot/.srec 等、既定でベリファイ）、`Erase`、`SaveBin`、`VerifyBin`。`-CommandFile` でバッチ実行、`-ExitOnError`・`-NoGui`、終了コードで成否判定 | https://kb.segger.com/J-Link_Commander |
| SEG-32 | 自動化・連携 | J-Link GDB Server | GDB リモートプロトコルで任意の IDE/ツールチェーン（GDB / LLDB）に対応。monitor コマンドで Flash 書き込み・Unlimited Flash BP・セミホスティング・デバイス固有機能。SWO（端末出力・ITM・PC サンプル）転送。GUI 版と CL 版（Linux / macOS / Windows、x86-64 / Arm64） | https://www.segger.com/products/debug-probes/j-link/tools/j-link-gdb-server/about-j-link-gdb-server/ |
| SEG-33 | 自動化・連携 | J-Link Remote Server | LAN モード（IP→USB 変換、テストファーム向け）とトンネルモード（SEGGER 運営の無料トンネルサーバ経由で NAT/ファイアウォール越し。大企業はサーバのソースを購入して自前運用）。CHAP 認証、任意の暗号化。GUI / CLI 版（Raspberry Pi 等でヘッドレス）。Commander・Embedded Studio・IAR・GDB/Eclipse から利用、Flasher のリモート書き込みにも | https://www.segger.com/products/debug-probes/j-link/tools/j-link-remote-server/ |
| SEG-34 | 自動化・連携 | J-Link SDK | J-Link DLL の API を使って独自アプリ（RTT・HSS・Flash 書き込み等）を作れる。RTT Logger のソースを同梱 | https://www.segger.com/products/debug-probes/j-link/ / https://www.segger.com/products/debug-probes/j-link/technology/about-real-time-transfer/ |
| SEG-35 | その他 | セミホスティング | GDB Server の monitor で有効化。詳細は未確認 | https://www.segger.com/products/debug-probes/j-link/tools/j-link-gdb-server/about-j-link-gdb-server/ |
| SEG-36 | その他 | OpenOCD 等サードパーティとの関係 | OpenOCD 経由では高速 Flash ローダ・Unlimited Flash BP 等の J-Link 固有機能が使えず、サポート対象外 | https://www.segger.com/products/debug-probes/j-link/ |
| SEG-37 | UI・UX | ファームウェア・ライセンス管理 | `ShowFWInfo`、`License`（独自ライセンスの追加・削除）、`Reboot`、`Uptime`、`ShowHWStatus`、ネットワーク設定（IP/GW/DNS） | https://kb.segger.com/J-Link_Commander |

### 1.2 J-Flash / J-Flash SPI / Flasher（量産書き込み）

| ID | 分類 | 機能 | 具体的な内容 | 出典 URL |
|---|---|---|---|---|
| SEG-38 | 接続・書き込み | J-Flash（量産向け GUI/バッチ） | 1 プロジェクトに複数 Flash バンク（内蔵＋外付け）を保存し 1 セッションで書き込み。CFI/非 CFI NOR（1x8〜2x16）、SPI NOR、NAND、QSPI。コマンドライン制御で量産利用。Windows / Linux / macOS | https://www.segger.com/products/debug-probes/j-link/tools/j-flash/about-j-flash/ |
| SEG-39 | 接続・書き込み | J-Flash Lite | 開発用の無料の簡易 GUI 書き込みツール（パック同梱） | https://www.segger.com/products/debug-probes/j-link/technology/flash-download/ |
| SEG-40 | 接続・書き込み | J-Flash SPI | CPU を介さずに SPI Flash を直接書く | https://www.segger.com/products/debug-probes/j-link/technology/flash-download/ |
| SEG-41 | 接続・書き込み | J-Flash のシリアル番号書き込み・パッチ | J-Flash 単体ページでは未確認（Flasher 側で「個体ごとに異なるデータ（シリアル番号・パッチ）の書き込み」を記載） | https://c.a.segger.com/fileadmin/documents/Product_briefs/Product-Brochure_Production-Tools_02.pdf |
| SEG-42 | 接続・書き込み | Flasher（スタンドアロン書き込み器） | PC 無しで書き込み。Flasher Pro XL 2 GB / Pro 128 MB / Compact / Portable PLUS（電池駆動）/ ATE2（8×128 MB、ATE 向け）。JTAG / SWD。Arm・RISC-V・RX/RL78・PowerPC・FPGA・外付け SPI Flash | https://www.segger.com/products/production/flasher/ |
| SEG-43 | 接続・書き込み | ギャング書き込み | Flasher Hub-4 / Hub-12 で 12 台以上を並列書き込み | https://www.segger.com/products/production/flasher/ |
| SEG-44 | 接続・書き込み | Authorized Flashing（書き込み回数制限） | IP 所有者が書き込み回数の上限を設定し、上限到達後は所有者が再設定するまで書けない（委託生産の過剰生産対策） | https://www.segger.com/products/production/flasher/technology/authorized-flashing/ |
| SEG-45 | 接続・書き込み | Flasher Secure（委託先での安全な量産） | サーバが 1 回ごとの書き込みを認可し、台数・シリアル番号を一元管理。シリアル番号の生成規則（日付・製造者・使用 Flasher 等）をカスタマイズ | https://www.segger.com/fileadmin/documents/Product_briefs/Product-Brief_Flasher-Secure_01.pdf / https://www.segger.com/products/production/flasher/tools/flasher-secure-server/ |
| SEG-46 | 自動化・連携 | Flasher のファイル操作・リモート制御 | Commander から Flasher 内のファイルを読み書き・一覧・削除（`FWrite`/`FRead`/`FList`）、`SecureArea` 作成。Remote Server 経由で遠隔書き込み。ATE からの制御 | https://kb.segger.com/J-Link_Commander / https://www.segger.com/products/debug-probes/j-link/tools/j-link-remote-server/ |

### 1.3 J-Trace PRO

| ID | 分類 | 機能 | 具体的な内容 | 出典 URL |
|---|---|---|---|---|
| SEG-47 | トレース | ストリーミングトレース（無制限） | トレースポートの命令トレースを実時間で PC へ送り、時間制限なく完全な命令トレースを記録 | https://www.segger.com/products/debug-probes/j-trace/ |
| SEG-48 | トレース | トレースの速度・対応エンコーダ | トレースクロック最大 150 MHz、TPIU 最大 300 MHz、SWO 100 MHz、I/F 50 MHz。Cortex-M/A/R ETM、Cortex-A PTM、RISC-V N-Trace BTM。ポート幅・バッファ容量は未確認 | https://www.segger.com/products/debug-probes/j-trace/ |
| SEG-49 | 性能・カバレッジ | ライブのコードカバレッジ・プロファイル | ストリーミングトレースから関数単位のカバレッジ・プロファイルを実行中に更新表示（Ozone） | https://www.segger.com/products/debug-probes/j-trace/ |
| SEG-50 | 接続・書き込み | J-Link 機能の包含 | J-Trace PRO は Flash Download、GDB Server、J-Flash、Ozone、RTT、Unlimited Flash BP を含む。デバッグ用とトレース用のコネクタは同時に使えない | https://www.segger.com/products/debug-probes/j-trace/ |

### 1.4 Ozone（デバッガ）

| ID | 分類 | 機能 | 具体的な内容 | 出典 URL |
|---|---|---|---|---|
| SEG-51 | 止めて調べる | ソースレベルデバッグ | C / C++ / Rust のソース窓、シンボル・データ窓、レジスタ・メモリ、内蔵エディタ。IDE 不要で ELF を読み込んで使う（Embedded Studio / GCC / Clang / IAR / Arm Compiler の出力） | https://www.segger.com/products/development-tools/ozone-j-link-debugger/ |
| SEG-52 | UI・UX | 再ビルド検出 | ファームウェアの再ビルドを自動検出し、新しいイメージの読み込みを促す | https://www.segger.com/products/development-tools/ozone-j-link-debugger/ |
| SEG-53 | RTOS | RTOS 認識 | RTOS プラグインでタスク状態・メールボックス・キュー・メモリプール等を表示（embOS の例） | https://www.segger.com/products/development-tools/ozone-j-link-debugger/ |
| SEG-54 | 止めずに見る | データサンプリング・グラフ | 走行中に変数をサンプリングしグラフ化（HSS ベース） | https://www.segger.com/products/development-tools/ozone-j-link-debugger/ |
| SEG-55 | トレース | 命令トレース窓 | J-Trace PRO のストリーミングトレースを命令単位で表示・ライブ解析。トレースレポートを外部解析用に出力 | https://www.segger.com/products/development-tools/ozone-j-link-debugger/ |
| SEG-56 | 時間軸の解析 | Timeline | タスク切り替え・関数実行・タスク遅延を時間軸で表示 | https://www.segger.com/products/development-tools/ozone-j-link-debugger/ |
| SEG-57 | 性能・カバレッジ | Code Profile / Code Coverage | 関数・行単位のプロファイルとカバレッジレポート（トレースまたはサンプリング） | https://www.segger.com/products/development-tools/ozone-j-link-debugger/ |
| SEG-58 | 電力 | Power Graph | J-Link の電流計測を時間軸で表示し、コードの動作と見比べる | https://www.segger.com/products/debug-probes/j-link/technology/power-profiling/ |
| SEG-59 | 故障解析 | Debug Snapshots | RAM・Flash・CPU レジスタ・選択した周辺・Timeline・プロファイル・グラフ・端末ログを保存し、後で復元。クロック・IRQ・周辺設定も復元して実行を再開できる。プローブ無し（オフライン）でも読み込み可。`.jdebug` に OnSnapshotSave / OnSnapshotLoad フック。元の ELF が必要 | https://kb.segger.com/Using_snapshots |
| SEG-60 | 自動化・連携 | スクリプト・自動化 I/F・イベントハンドラ | プロジェクトスクリプト（`.jdebug`）、コンソールからのコマンド、自動化 I/F。ダウンロード・リセット・停止・実行・ブレークポイントの前/後にハンドラを実行 | https://www.segger.com/products/development-tools/ozone-j-link-debugger/ |
| SEG-61 | 止めずに見る | 端末 / コンソール（RTT・SWO・セミホスティング） | ターゲット出力を端末窓に表示 | https://www.segger.com/products/development-tools/ozone-j-link-debugger/ |
| SEG-62 | その他 | Ozone-Sim | 命令セットシミュレータ版。機能詳細は未確認 | https://www.segger.com/products/development-tools/ozone-j-link-debugger/ |

### 1.5 SystemView / J-Scope / RTT Viewer / Embedded Studio

| ID | 分類 | 機能 | 具体的な内容 | 出典 URL |
|---|---|---|---|---|
| SEG-63 | 時間軸の解析 | SystemView: イベントの時間軸 | タスク切り替え・割り込み・スケジューライベントを時刻順に表示。タイムスタンプは CPU 1 サイクル分解能（200 MHz で 5 ns）。マルチコアの同期タイムライン | https://www.segger.com/products/development-tools/systemview/ |
| SEG-64 | 時間軸の解析 | SystemView: 記録モード | 連続（RTT でストリーミング、J-Link 必須、Cortex-A/R は AHB-AP 必要）、シングルショット、ポストモーテム（後 2 者は任意のシステム） | https://www.segger.com/products/development-tools/systemview/ |
| SEG-65 | RTOS | SystemView: RTOS 対応 | embOS、ThreadX、FreeRTOS（V11 からネイティブ）、uC/OS-II/III、Micrium OS、Zephyr、NuttX。ベアメタルは割り込みとユーザイベントのみ | https://www.segger.com/products/development-tools/systemview/ |
| SEG-66 | 性能・カバレッジ | SystemView: CPU 負荷 | コアごとの負荷解析 | https://www.segger.com/products/development-tools/systemview/ |
| SEG-67 | 止めずに見る | SystemView: DataPlot・ログ | 変数・センサ値をイベントと同じ時間軸でグラフ化。printf 形式のログはホスト側で整形（ターゲットは書式文字列を送らない、ELF から名前を解決） | https://www.segger.com/products/development-tools/systemview/ |
| SEG-68 | 故障解析 | SystemView: ヒープ監視 | C/C++ ヒープ・独自ヒープ・RTOS メモリプールの使用量・ピーク・確保元を追跡 | https://www.segger.com/products/development-tools/systemview/ |
| SEG-69 | その他 | SystemView: オーバーヘッド | ROM 2 KB 未満・RAM 約 600 B、200 MHz Cortex-M4 で 10,000 イベント/s 時に CPU 1% 未満。バッファ 1〜4 KB、イベント発生コンテキスト毎に約 200 B のスタック。I/F 4 MHz 以上推奨。WFI 等の低電力モードは非推奨 | https://www.segger.com/products/development-tools/systemview/ |
| SEG-70 | 止めずに見る | J-Scope: 変数のオシロスコープ表示 | HSS（コード変更不要）または RTT（計装が必要、最大 2 MB/s）で変数を取得しグラフ表示。ELF から変数を選択。変数数は上位機 100 / 他 10。データのファイル保存、プロジェクト保存。トリガ機能は未確認。HSS は実行と非同期 | https://www.segger.com/products/debug-probes/j-link/tools/j-scope/ |
| SEG-71 | 止めずに見る | RTT Viewer | チャネル 0 上に最大 16 の仮想端末（色・画面消去の制御コード対応）、チャネル 1 のバイナリをファイル記録、下り入力（行単位・逐次）、既存デバッグセッションへのアタッチ（Telnet ポートで選択）、コマンドライン起動 | https://kb.segger.com/J-Link_RTT_Viewer |
| SEG-72 | 止めて調べる | Embedded Studio のデバッガ | J-Link 統合、ブレークポイント・ステップ・変数・式評価、RTOS プラグイン（タスク・スタック使用量）、静的スタック解析、メモリ使用量解析、実行トレース・プロファイル、命令セットシミュレータ emSim。ライブウォッチ・カバレッジは製品ページでは未確認 | https://www.segger.com/products/development-tools/embedded-studio/ |

---

## 2. IAR Embedded Workbench for Arm（C-SPY / C-RUN / I-jet / I-scope）

| ID | 分類 | 機能 | 具体的な内容（条件・制約・必要なハード） | 出典 URL |
|---|---|---|---|---|
| IAR-01 | 接続・書き込み | I-jet の仕様 | JTAG/SWD 最大 48 MHz、USB 2.0 HS、SWO 最大 60 Mbps（Manchester / UART）、ターゲット電圧 1.65〜5.5 V 自動検出 | https://docs.iar.com/probes/en/i-jet/technical-specifications/model-specifications.html |
| IAR-02 | 接続・書き込み | I-jet からのターゲット給電 | 4.4〜5 V で最大 420 mA、約 520 mA で過電流保護 | https://docs.iar.com/probes/en/i-jet/technical-specifications/model-specifications.html |
| IAR-03 | 電力 | I-jet の電流計測 | 分解能 約 160 µA、最大 200 ksps。C-SPY の Power Log / Timeline に表示 | https://docs.iar.com/probes/en/i-jet/technical-specifications/model-specifications.html |
| IAR-04 | 電力 | I-scope（I-jet 用アドオン） | 基板上の任意の点の電流（外付けシャント両端を差動で測定）と電圧を 12 bit・最大 200 kHz で測定（2013 年の発表） | https://news.cision.com/iar-systems-group-ab/r/iar-systems-now-shipping-i-scope-probe-for-real-time-current-and-voltage-measurements,c9404902 |
| IAR-05 | 電力 | Power Log / Power Log Setup | チャネル毎にしきい値を設定し「全記録 / しきい値超のみ / 未満のみ」、プローブによっては「しきい値超（未満）で CPU を停止」。サンプリング周波数を指定（最寄りの可能値に丸め） | https://docs.iar.com/ewarm/9.7x/en/c-spy-debugging/power-debugging/reference-information-on-power-debugging/power-log-setup-window.html |
| IAR-06 | 電力 | 電力とソースの対応付け | Timeline の Power グラフで電流サンプルを共通時間軸に表示し、割り込み・関数の実行と対応づける。保存ログには上限があり古いものから捨てる | https://docs.iar.com/ewarm/10.1x/en/c-spy-debugging/power-debugging/reference-information-on-power-debugging/timeline-window-power-graph.html |
| IAR-07 | 時間軸の解析 | Timeline 窓 | 割り込みログ・データログ・イベントログ・電力・（TI EnergyTrace の場合）State Log を共通時間軸で表示。秒 / サイクル切替。SWD/SWO が必要 | https://docs.iar.com/ewarm/9.7x/en/c-spy-debugging/additional-information-on-c-spy-drivers/reference-information-on-the-c-spy-hardware-debugger-drivers/i-jet-menu.html / https://docs.iar.com/ewarm/9.7/en/c-spy-debugging/power-debugging/reference-information-on-power-debugging/timeline-window-state-log-graph.html |
| IAR-08 | 止めずに見る | Interrupt Log / Summary | 割り込みの入退出を SWO で記録し、回数・時間を集計（SWD/SWO 限定） | https://docs.iar.com/ewarm/9.7x/en/c-spy-debugging/additional-information-on-c-spy-drivers/reference-information-on-the-c-spy-hardware-debugger-drivers/i-jet-menu.html |
| IAR-09 | 止めずに見る | Data Log / Summary | データログブレークポイントで指定変数へのアクセスを SWO で記録（1 命令 4 バイト以下の制約） | https://docs.iar.com/ewarm/9.7x/en/c-spy-debugging/breakpoints/introduction-to-setting-and-using-breakpoints/breakpoint-types.html |
| IAR-10 | 止めずに見る | Event Log / Summary | アプリから出したイベントを記録・集計 | https://docs.iar.com/ewarm/9.7x/en/c-spy-debugging/additional-information-on-c-spy-drivers/reference-information-on-the-c-spy-hardware-debugger-drivers/i-jet-menu.html |
| IAR-11 | 止めずに見る | SWO Trace 窓 | SWO の設定・表示（ITM 等） | https://docs.iar.com/ewarm/9.7x/en/c-spy-debugging/additional-information-on-c-spy-drivers/reference-information-on-the-c-spy-hardware-debugger-drivers/i-jet-menu.html |
| IAR-12 | 止めずに見る | Live Watch | 実行中に変数を周期更新表示（C-SPY 実行中のみ） | https://docs.iar.com/ewrx/5.2/en/ide-project-management-and-building/menu-reference/menus/view-menu.html |
| IAR-13 | トレース | ETM トレース・Function Trace | ETM 設定・保存・トレース窓・関数トレース窓（I-jet Trace 等トレース対応プローブ）。I-jet Trace の容量・速度は未確認 | https://docs.iar.com/ewarm/9.7x/en/c-spy-debugging/additional-information-on-c-spy-drivers/reference-information-on-the-c-spy-hardware-debugger-drivers/i-jet-menu.html |
| IAR-14 | 性能・カバレッジ | Function Profiler | 関数ごとの時間。Timeline で選んだ区間に限定した集計（PC サンプリング対応ドライバのとき） | https://docs.iar.com/ewarm/9.7x/en/c-spy-debugging/additional-information-on-c-spy-drivers/reference-information-on-the-c-spy-hardware-debugger-drivers/i-jet-menu.html |
| IAR-15 | 性能・カバレッジ | Performance Monitoring | PMU のイベントカウンタ・サイクル数を表示 | https://docs.iar.com/ewarm/9.7x/en/c-spy-debugging/additional-information-on-c-spy-drivers/reference-information-on-the-c-spy-hardware-debugger-drivers/i-jet-menu.html |
| IAR-16 | 性能・カバレッジ | Code Coverage 窓 | プログラム・モジュール・関数ごとの実行率（C/C++）。アセンブラは逆アセンブル窓で確認。SWO トレース利用時はサンプルに基づく近似で、100% に達するには複数回の実行が必要 | https://docs.iar.com/ewrx/5.2x/en/c-spy-debugging/code-coverage/introduction-to-code-coverage.html |
| IAR-17 | ブレークポイント | コードブレークポイント | 命令フェッチで停止（実行前）。条件・スキップ回数・マクロ実行などの付加設定は本調査では未確認 | https://docs.iar.com/ewarm/9.7x/en/c-spy-debugging/breakpoints/introduction-to-setting-and-using-breakpoints/breakpoint-types.html |
| IAR-18 | ブレークポイント | ログブレークポイント | ソースを変えずにメッセージを Debug Log に出して実行継続（Arm では SWO 設定が必要） | https://docs.iar.com/ewarm/9.7x/en/c-spy-debugging/breakpoints/introduction-to-setting-and-using-breakpoints/breakpoint-types.html |
| IAR-19 | ブレークポイント | Trace Start / Stop トリガ | 2 点間の命令だけをトレース収集 | https://docs.iar.com/ewarm/9.7x/en/c-spy-debugging/breakpoints/introduction-to-setting-and-using-breakpoints/breakpoint-types.html |
| IAR-20 | ブレークポイント | データブレークポイント | アクセスで停止（通常アクセス命令の直後）。Cortex-M55 等では数命令遅れることがある | https://docs.iar.com/ewarm/9.7x/en/c-spy-debugging/breakpoints/introduction-to-setting-and-using-breakpoints/breakpoint-types.html |
| IAR-21 | ブレークポイント | データログブレークポイント | 止めずにアクセスを Data Log / Timeline に記録 | https://docs.iar.com/ewarm/9.7x/en/c-spy-debugging/breakpoints/introduction-to-setting-and-using-breakpoints/breakpoint-types.html |
| IAR-22 | ブレークポイント | イミディエイトブレークポイント（シミュレータ） | 読み出し前/書き込み後に一瞬止めて C-SPY マクロを実行（メモリマップ周辺の模擬） | https://docs.iar.com/ewarm/9.7x/en/c-spy-debugging/breakpoints/introduction-to-setting-and-using-breakpoints/breakpoint-types.html |
| IAR-23 | ブレークポイント | Flash ブレークポイント | I-jet ドライバで Flash 等の不揮発メモリにソフトブレークポイント | https://docs.iar.com/ewarm/9.7x/en/c-spy-debugging/breakpoints/introduction-to-setting-and-using-breakpoints/breakpoint-types.html |
| IAR-24 | ブレークポイント | ベクタキャッチ・Breakpoint Usage | 例外ベクタで停止。使用中のブレークポイント資源を一覧 | https://docs.iar.com/ewarm/9.7x/en/c-spy-debugging/additional-information-on-c-spy-drivers/reference-information-on-the-c-spy-hardware-debugger-drivers/i-jet-menu.html |
| IAR-25 | 止めて調べる | 実行制御の細かな設定 | 終了時にターゲットを走らせたまま、ステップ中の割り込み禁止、走行中に BP を置けない HW 向けに一瞬止めて設定、メモリ構成の強制（未定義領域へのアクセス禁止）、デバッガキャッシュ無効化 | https://docs.iar.com/ewarm/9.7x/en/c-spy-debugging/additional-information-on-c-spy-drivers/reference-information-on-the-c-spy-hardware-debugger-drivers/i-jet-menu.html |
| IAR-26 | 故障解析 | Fault exception viewer | Cortex-M で故障例外が起きて停止すると自動で開き、原因を表示。例外時の PC / LR をコピー | https://docs.iar.com/ewarm/9.7/en/c-spy-debugging/executing-your-application/reference-information-on-application-execution/fault-exception-viewer-window.html |
| IAR-27 | 故障解析 | スタック使用量の監視 | スタックを 0xCD で埋め、停止時にどこまで上書きされたかで使用量を表示。しきい値超で警告 | https://docs.iar.com/ewrx/5.2/en/c-spy-debugging/memory-and-registers/monitoring-memory-and-registers/monitoring-stack-usage.html |
| IAR-28 | 故障解析 | 静的スタック使用量解析 | リンカがコールグラフの根ごとの最大スタック深さをマップファイルに出力 | https://www.iar.com/news/press-releases/iar-systems-adds-stack-usage-analysis-to-its-popular-development-tools-for-renesas-rx-c73b9206 |
| IAR-29 | 故障解析 | C-RUN（実行時エラー検査） | 算術（オーバーフロー・不正シフト・ゼロ除算・値の変わる変換・switch の未処理）、境界（ポインタが対象オブジェクト内か。コード量・速度のコスト大）、ヒープ（不正書き込み・二重 free・確保/解放の不一致・リーク）。プロジェクト/ファイル単位で有効化 | https://docs.iar.com/ewrx/5.2/en/c-spy-debugging/c-run-runtime-error-checking/introduction-to-runtime-error-checking.html / https://www.iar.com/knowledge/learn/the-basics-of-c-run/ |
| IAR-30 | 故障解析 | C-RUN の現地（フィールド）利用 | 検査入りの試験ファームの出力をシリアルで記録し、後で cspybat でテキストに解読 | https://www.iar.com/knowledge/learn/the-basics-of-c-run/ |
| IAR-31 | 自動化・連携 | cspybat（バッチ実行） | コマンドラインで C-SPY を実行し、終了状態を OS に返す。launch.json による起動も可。マクロ側で `__isBatchMode()` 判定 | https://docs.iar.com/ewarm/10.1x/en/c-spy-debugging/the-c-spy-command-line-utility-cspybat/using-c-spy-in-batch-mode.html / https://docs.iar.com/ewrx/5.2x/en/c-spy-debugging/c-spy-macros/reference-information-on-c-spy-system-macros/__isbatchmode.html |
| IAR-32 | 自動化・連携 | C-SPY マクロ | C 風のマクロ言語で、セットアップ・ブレークポイント時の処理・周辺模擬などを記述（システムマクロ群あり） | https://docs.iar.com/ewrx/5.2x/en/c-spy-debugging/c-spy-macros/reference-information-on-c-spy-system-macros/__isbatchmode.html |
| IAR-33 | RTOS | RTOS 認識プラグイン | タスク一覧・キュー・セマフォ・メールボックス・RTOS 変数を専用メニュー・窓で表示 | https://docs.iar.com/ewrx/5.2/en/c-spy-debugging/the-iar-c-spy-debugger/introduction-to-c-spy.html |
| IAR-34 | 止めて調べる | マルチコアデバッグ | 対称マルチコアでは窓がフォーカス中のコアの状態を示す（RH850 版の記述。Arm 版の詳細は未確認） | https://docs.iar.com/ewrh850/3.2/en/c-spy-debugging/multicore-debugging/introduction-to-multicore-debugging.html |
| IAR-35 | UI・UX | Session Overview / EmuDiag | セッション設定・状態の一覧と保存。ホスト〜プローブ〜基板の接続診断ツール | https://docs.iar.com/ewarm/9.7x/en/c-spy-debugging/additional-information-on-c-spy-drivers/reference-information-on-the-c-spy-hardware-debugger-drivers/i-jet-menu.html |
| IAR-36 | その他 | 命令セットシミュレータ | ハード無しで実行。イミディエイト BP とマクロで周辺を模擬 | https://docs.iar.com/ewarm/9.7x/en/c-spy-debugging/breakpoints/introduction-to-setting-and-using-breakpoints/breakpoint-types.html |
| IAR-37 | 自動化・連携 | J-Link・ST-LINK 等の他社プローブ対応 | C-SPY は J-Link/J-Trace ドライバを持つ（JTAG ウォッチポイント等）。他のプローブの一覧は未確認 | https://docs.iar.com/ewarm/9.7x/en/c-spy-debugging/breakpoints/introduction-to-setting-and-using-breakpoints/breakpoint-types.html |

---

## 3. Arm Keil MDK（µVision / Event Recorder / ULINK）と Arm Development Studio（Streamline）

| ID | 分類 | 機能 | 具体的な内容（条件・制約・必要なハード） | 出典 URL |
|---|---|---|---|---|
| KEIL-01 | 止めずに見る | Event Recorder | API で注釈したイベントを RAM バッファに記録し、デバッガが走行中に読む。SWO・ETM 不要で全 Cortex-M（シミュレータでも可） | https://www.keil.com/pack/doc/compiler/EventRecorder/html/er_overview.html |
| KEIL-02 | 性能・カバレッジ | Event Statistics | `EventStartX`/`EventStopX` で囲んだ区間（グループ文字＋スロット番号）ごとに、実行時間・回数・（ULINKplus 使用時）消費電流を集計。最小/最大のソース位置を表示 | https://www.keil.com/pack/doc/compiler/EventRecorder/html/es_use.html |
| KEIL-03 | RTOS | Component Viewer（SCVD） | ソフト部品の静的情報（RTOS オブジェクト・ミドルウェアの状態等）を、SCVD 記述ファイルに従い表示。RTX5 のイベントは Event Recorder 経由 | https://www.keil.com/pack/doc/compiler/EventRecorder/html/er_overview.html / https://www.keil.com/support/docs/4165.htm |
| KEIL-04 | 時間軸の解析 | System Analyzer | Event Recorder のイベント、Logic Analyzer の変数、電力（ULINKplus）を共通時間軸で表示。電力データの表示はリングバッファで約 8.5 秒（2019 年時点） | https://www.keil.com/support/docs/4165.htm |
| KEIL-05 | 止めずに見る | Logic Analyzer（SWV） | SWO のデータトレースで最大 4 変数を実時間グラフ化（Cortex-M の DWT コンパレータ数の制約）。ウォッチポイントと排他 | https://www.keil.com/support/docs/4165.htm / https://learn.arm.com/learning-paths/embedded-and-microcontrollers/uv_debug/4_swv/ |
| KEIL-06 | 止めずに見る | Debug (printf) Viewer・Trace Exceptions・Event Counters | ITM の printf、例外の入退出、DWT のイベントカウンタを SWV で表示。詳細仕様は未確認 | https://learn.arm.com/learning-paths/embedded-and-microcontrollers/uv_debug/4_swv/ |
| KEIL-07 | 止めずに見る | ウォッチ・メモリの実時間更新 | 走行中にウォッチ・メモリ窓を周期更新（Periodic Window Update）。詳細は未確認 | https://learn.arm.com/learning-paths/embedded-and-microcontrollers/uv_debug/4_swv/ |
| KEIL-08 | トレース | ETM 命令トレース（ULINKpro） | ULINKpro で Cortex-M3/M4 の ETM を最大 800 Mbit/s でホストへストリーミング（無制限バッファ）。Trace Data 窓（停止後に表示、ソース・逆アセンブルと連動）、記録の検索・表示フィルタ | https://keil.com/arm/ulinkpro / https://learn.arm.com/learning-paths/embedded-and-microcontrollers/uv_debug/5_etm_trace |
| KEIL-09 | ブレークポイント | トレースポイント（TraceRun / TraceSuspend / TraceHalt） | トレース収集の開始・中断・停止の条件をコード位置で指定（スキッドあり） | https://learn.arm.com/learning-paths/embedded-and-microcontrollers/uv_debug/5_etm_trace |
| KEIL-10 | 性能・カバレッジ | Code Coverage | ETM による命令単位カバレッジ（実行 / 未実行 / 分岐の常に成立・不成立を余白の色で表示）。統計窓、`.gcov` や ASCII ログに出力。FVP でもファイル経由で可 | https://learn.arm.com/learning-paths/embedded-and-microcontrollers/uv_debug/5_etm_trace |
| KEIL-11 | 性能・カバレッジ | Performance Analyzer / Execution Profiler | 関数ごとの時間・呼び出し回数（実時間更新）、ソース行ごとの実行時間・回数（ETM + ULINKpro が必要） | https://learn.arm.com/learning-paths/embedded-and-microcontrollers/uv_debug/5_etm_trace |
| KEIL-12 | 電力 | ULINKplus の電力計測 | 16 bit Σ-Δ ADC ×2。内蔵シャントで ±2.5 mA（分解能 200 nA、±2%）、外付けシャント（5〜250 mA 用 6 種同梱）でレンジ拡張。電圧 ±6 V（0.6 mV）。サンプルレートの公式値は未確認（販売店・コミュニティ情報） | https://static.mercateo.com/32/beac69b5b2c04358ba6d58f435c83bf1/pdf/2549001.pdf |
| KEIL-13 | 電力 | イベント単位のエネルギー集計 | Event Statistics の区間ごとに消費電流・エネルギーを集計（ULINKplus 必須） | https://www.keil.com/pack/doc/compiler/EventRecorder/html/es_use.html |
| KEIL-14 | 自動化・連携 | ULINKplus の試験用 I/O・絶縁 | デジタル I/O 最大 9、アナログ入力最大 4、アナログ出力 1、3.3 V 10 mA の切替電源を、デバッグ画面やデバッグスクリプトから操作し試験台を制御。1 kV 絶縁（販売店資料） | https://static.mercateo.com/32/beac69b5b2c04358ba6d58f435c83bf1/pdf/2549001.pdf |
| KEIL-15 | 自動化・連携 | デバッグスクリプト（.ini・デバッグ関数） | µVision のデバッグ関数でセットアップ・周辺模擬・I/O 操作。詳細は未確認 | https://www.keil.com/support/docs/4165.htm |
| KEIL-16 | その他 | シミュレータ / FVP | µVision の命令セットシミュレータ、Arm FVP（Corstone 等）でハード無しにデバッグ・カバレッジ | https://learn.arm.com/learning-paths/embedded-and-microcontrollers/uv_debug/5_etm_trace |
| KEIL-17 | 自動化・連携 | Keil Studio（VS Code 拡張） | Keil Studio Pack: CMSIS csolution、Arm Device Manager、Arm Embedded Debugger（DAP 準拠、CMSIS-DAP 等で書き込み・デバッグ、プローブ経由のシリアルモニタ）、リモートビルド。マルチコア・Ethos-U 対応。J-Link は CMSIS-DAP モードで利用 | https://kb.segger.com/J-Link_Arm_Keil_Studio_Pack / https://developer.arm.com/documentation/108029/0000/ |
| KEIL-18 | 接続・書き込み | CMSIS-Pack によるデバイス対応 | デバイス・Flash アルゴリズム・SVD（周辺レジスタ表示）を Pack で配布し、デバッガ設定を自動化 | https://arm.com/products/development-tools/embedded-and-software/arm-development-studio |
| ADS-01 | 止めて調べる | Arm Development Studio デバッガ | Cortex-M/R/A/X・Neoverse、GPU を含む異種マルチコア、Armv9 64 bit SoC。CMSIS-Pack と既成ターゲット設定 | https://arm.com/products/development-tools/embedded-and-software/arm-development-studio |
| ADS-02 | RTOS | OS 認識（Linux カーネル・RTOS） | カーネルスレッド・プロセス・ローダブルモジュール、ThreadX 等の RTOS オブジェクトを表示（DS-5 時代の資料。現行版の詳細は未確認） | https://www.automation.com/article/express-logic-adapts-threadx-to-arm-development-st |
| ADS-03 | トレース | DSTREAM-ST / DSTREAM-PT / DSTREAM-HT | ST: 4 ピンで 2.4 Gbps のパラレルトレースをホストへストリーミング。PT: 最大 32 bit 幅・8 GB バッファ。HT: 高速シリアルトレース（詳細未確認） | https://developer.arm.com/community/arm-community-blogs/b/tools-software-ides-blog/posts/enhanced-debug-and-analysis-dstream-pt-parallel-trace-probe / https://www.arm.com/products/development-tools/debug-probes/dstream-ht |
| ADS-04 | 性能・カバレッジ | Streamline | PMU カウンタ・プロセス/スレッド・注釈（アノテーション）を同期した時間軸、Call Paths、関数ごとのコスト。Linux / Android / ベアメタル / RTOS（Cortex-M は ITM 捕捉の取り込み） | https://community.arm.com/tools/b/blog/posts/analyzing-rtos-based-systems-performance-using-streamline |
| ADS-05 | 電力 | Arm Energy Probe 連携 | 最大 3 点の電圧・電流・電力をサンプリングし、Streamline の性能データ・実行トレースと同期表示 | https://arm.com/company/news/2012/02/arm-enables-energy-aware-coding-with-the-arm-energy-probe-and-ds-5-toolchain |
| ADS-06 | その他 | Fast Models / FVP | 実チップ前に仮想プロトタイプ上でデバッグ | https://arm.com/products/development-tools/embedded-and-software/arm-development-studio |
| ADS-07 | 自動化・連携 | スクリプト（DS スクリプト・Jython） | DS コマンドスクリプトと Jython。コマンドラインからは DS スクリプトのみとの指摘あり（フォーラム）。現行の仕様は未確認 | https://community.arm.com/support-forums/f/armds-forum/8485/run-jython-scripts-from-command-line/28828 |

---

## 4. Lauterbach TRACE32

| ID | 分類 | 機能 | 具体的な内容（条件・制約・必要なハード） | 出典 URL |
|---|---|---|---|---|
| T32-01 | トレース | PowerTrace（オフチップトレース記録器） | トレースメモリ 256 MB〜8 GB。ホストのファイルへストリーミングで最大 1 T フレーム。PowerTrace Serial V2 は 4/8 GB（高速シリアルトレース） | https://repo.lauterbach.com/tracesinks.html / https://jp.lauterbach.com/products/LA-3122 |
| T32-02 | トレース | オンチップトレースの利用 | ETB 等のオンチップバッファ（数エントリ〜数 KB）も同じ解析画面で扱う | https://repo.lauterbach.com/tracesinks.html |
| T32-03 | トレース | CombiProbe 2 | デバッグ＋最大 4 bit 幅の小型トレースポートを 1 本のプローブで。トレースピンあたり 200〜400 Mbit/s、トレースメモリ 512 MB（構成により異なる）。PowerDebug モジュールが必要 | https://lauterbach.com/cob_riscv.html / https://jp.lauterbach.com/products/LA-3060 |
| T32-04 | 時間軸の解析 | トレースリスト・検索 | 記録したトレースを一覧表示し、条件で検索（ソース・逆アセンブル・データアクセスを表示） | https://www2.lauterbach.com/pdf/trace_tutorial.pdf |
| T32-05 | 時間軸の解析 | グラフィカルチャート | 関数・タスク・変数の推移を時間軸チャートで表示 | https://www2.lauterbach.com/pdf/trace_tutorial.pdf |
| T32-06 | 性能・カバレッジ | 関数実行時間・統計・関数ツリー | 関数ごとの実行時間（最小/最大/平均、呼び出し回数）、呼び出しツリー、距離（Distance）・継続時間（Duration）解析 | https://www2.lauterbach.com/pdf/trace_tutorial.pdf |
| T32-07 | トレース | トレースの保存・読み込み | 記録を保存し、後でオフラインで解析 | https://www2.lauterbach.com/pdf/trace_tutorial.pdf |
| T32-08 | 故障解析 | CTS（Context Tracking System） | トレースからレジスタ・メモリの過去の状態を再構成し、記録区間を前後にステップ実行（逆方向デバッグ）。フルデータトレースがあれば過去のローカル変数・スタックも表示。SMP/AMP 対応、実時間性を壊さない。1999 年から提供 | https://repo.lauterbach.com/news_560.html |
| T32-09 | 性能・カバレッジ | キャッシュ解析・Flash アクセス解析 | CTS を基にキャッシュの動作や Flash アクセスを模擬・解析 | https://repo.lauterbach.com/news_560.html / https://www.eetindia.co.in/cache-analysis-through-lauterbach-trace32/ |
| T32-10 | 性能・カバレッジ | トレースによるコードカバレッジ（機能安全） | 文・分岐・MC/DC・関数・呼び出しカバレッジ。文/関数/呼び出しは計装不要。MC/DC・判定は目的限定の少量計装（ソースの 10% 以下）とマルチモード。判定構造は t32cast で静的解析。ISO 26262 のツール認定キット | https://lauterbach.com/coverage.html / https://support.lauterbach.com/news/posts/new-multi-mode-mcdc-coverage |
| T32-11 | 時間軸の解析 | PowerIntegrator（ロジアナ拡張） | 204（102）ch、ステート 200（400）MHz・タイミング 250（500）MHz、8 MB サンプルメモリ・512K フレーム、48 bit タイムスタンプ、トリガフィルタ・遅延、トランジェント記録。PowerDebug に追加してプログラムトレースと同期 | https://www.lauterbach.com/products/LA-7940 |
| T32-12 | 時間軸の解析 | ロジアナプローブ（PowerTrace 内蔵用） | PowerTrace II 用の 512K エントリのデジタル/アナログ信号メモリ（プログラムトレースと同時記録） | https://repo.lauterbach.com/publications/trace32_logic_analyzer_probe_for_powertrace_ii.pdf |
| T32-13 | 時間軸の解析 | PowerProbe（旧製品） | 最大 64 ch、タイミング 400 MHz・ステート 100 MHz。新規非推奨 | https://www2.lauterbach.com/doc/pp.pdf / https://cn.lauterbach.com/products/LA-7931 |
| T32-14 | 電力 | エネルギープロファイリング | Analog Probe: 電圧 4 ch（0〜5 V、12 bit）、電流 3 ch（外付けシャント、精度 5%）、625 kS/s。Mixed-Signal Probe もあり。PowerTrace / CombiProbe / MicroTrace のプログラムトレースと相関し、関数ごとの電力・エネルギーを表示 | https://repo.lauterbach.com/energyprofiling.html / https://lauterbach.com/products/LA-7949 |
| T32-15 | 止めずに見る | ソフトウェアのみのトレース（LOGGER / FDX） | トレースハードが無い場合、ターゲットに計装して RAM バッファ（LOGGER）や FDX（高速データ交換）で記録 | https://www.lauterbach.com/Features/Bug-Hunting/Software-Only-Trace/ |
| T32-16 | 止めずに見る | SNOOPer（サンプリング） | コードを変更できないとき、PC やデータ値を周期的にサンプリングして統計的にプロファイル（ピーク値は不正確） | https://www.lauterbach.com/Features/Bug-Hunting/Software-Only-Trace/ |
| T32-17 | 自動化・連携 | PRACTICE（CMM スクリプト） | TRACE32 の全操作を記述するスクリプト言語。Python から `dbg.cmm()` で実行 | https://www2.lauterbach.com/pdf/app_python.pdf |
| T32-18 | 自動化・連携 | Remote API / pyrcl | TCP（既定ポート 20000）/ UDP で外部から TRACE32 を制御。Python パッケージ `lauterbach.trace32.rcl`（2020.09〜）。設定ファイル無しで起動する `lauterbach-trace32-pystart` | https://pyrcl.readthedocs.io/ / https://support.lauterbach.com/news/posts/python-library-in-place-of-trace32-config-files |
| T32-19 | 自動化・連携 | 他 IDE との連携（TCF） | TRACE32 を TCF エージェントとして Wind River Workbench 4.x から操作 | https://repo.lauterbach.com/intwindriver.html |
| T32-20 | RTOS | OS 認識（Linux 等） | Linux の MMU 変換を扱いカーネル・プロセスをデバッグ、Linux 用メニュー。RTOS 対応一覧は本調査では未確認 | https://wiki.dave.eu/index.php/MITO8M-AN-001:_Advanced_multicore_debugging,_tracing,_and_energy_profiling_with_Lauterbach_TRACE32 |
| T32-21 | 止めて調べる | マルチコア（SMP/AMP）デバッグ | SMP・AMP のデバッグとトレース（CTS も対応） | https://repo.lauterbach.com/news_560.html |
| T32-22 | 自動化・連携 | 関数リファレンス（状態の問い合わせ） | `Analyzer.SIZE()` 等、スクリプトから状態を取得する関数群 | https://www2.lauterbach.com/pdf/general_func.pdf |
| T32-23 | UI・UX | PowerView | 全機能共通の GUI（コマンドライン＋窓）。個別画面の網羅的な一覧は本調査では未確認 | https://www2.lauterbach.com/pdf/trace_tutorial.pdf |

---

## 5. TASKING / iSYSTEM winIDEA（BlueBox）

| ID | 分類 | 機能 | 具体的な内容 | 出典 URL |
|---|---|---|---|---|
| WIN-01 | トレース | Analyzer（トレース記録） | プログラム・データ・計装・補助 I/O のトレースを記録し、PC の動き・ソース・変数の読み書きを再構成 | https://www.isystem.com/downloads/winIDEA/help/analyzer-overview.html |
| WIN-02 | 時間軸の解析 | Profiler Timeline / Statistics | 関数・タスク・割り込みの状態（Active / Inactive / Suspended）とイベント（Entry / Resume / Suspend / Exit / Write）を時間軸と統計（区間・領域・セッション）で表示 | https://www.isystem.com/downloads/winIDEA/help/analyzer-overview.html |
| WIN-03 | 時間軸の解析 | Profiler Inspectors | 記録に独自の後処理を加え、イベントチェーンやタイミング制約違反などの派生イベントを生成 | https://www.isystem.com/downloads/winIDEA/help/analyzer-overview.html |
| WIN-04 | 性能・カバレッジ | Coverage | 未実行コード・デッドコード・冗長なテストケースを検出 | https://www.isystem.com/downloads/winIDEA/help/analyzer-overview.html / https://www.tasking.com/products/analyzer/ |
| WIN-05 | RTOS | RTOS プロファイリング | プログラム・RTOS のプロファイル（iC5700） | https://www.tasking.com/products/network-trace/ic5700-hub/ |
| WIN-06 | 時間軸の解析 | ADIO・CAN/LIN アドオン | アナログ/デジタル I/O と CAN/LIN バスを記録し、プログラムトレースと同時に解析 | https://www.isystem.com/downloads/winIDEA/help/analyzer-overview.html |
| WIN-07 | 自動化・連携 | isystem.connect SDK（Python 等） | 外部プログラムから BlueBox を制御し、メモリ・レジスタ・トレース・プロファイラ・カバレッジを取得 | https://www.isystem.com/downloads/winIDEA/help/isystem-connect-sdks.html |
| WIN-08 | 自動化・連携 | testIDEA（単体テスト） | ターゲット上で関数単位のテストを実行（スタブ・カバレッジ 100% を目標にした例）。SDK から実行・フィルタ | https://isystem.com/downloads/winIDEA/SDK/iSYSTEM.Python.SDK/documentation/isystem-connect-api/py_examples_by_group.html |
| WIN-09 | 電力 | 電力計測 | 公式資料では未確認 | — |

---

## 6. PEmicro / Green Hills / Wind River（主な差別化機能のみ）

| ID | 分類 | 機能 | 具体的な内容 | 出典 URL |
|---|---|---|---|---|
| PEM-01 | 接続・書き込み | Cyclone（スタンドアロン量産書き込み） | PC 無しでボタン 1 つで書き込み、PC からは自動制御 SDK で起動。FX は内蔵 1 GB＋SDHC | https://pemicro.com/cyclone / https://NXP.com/docs/en/fact-sheet/U-CYCLONE-FX-FS.pdf |
| PEM-02 | 接続・書き込み | ProCryption（イメージ暗号化・回数/期限制限） | RSA と AES でイメージを暗号化し、同じ ImageKey を設定した Cyclone だけが書ける。書き込み回数・日付範囲の制限、耐タンパ | https://www.pemicro.com/manuals/CycloneFX/programming_image_encryption/ |
| PEM-03 | 接続・書き込み | シリアライズ・動的データ・ターゲット試験 | 量産でのシリアル番号付与、動的データの埋め込み、ターゲット試験（販売店の機能表。手順は未確認） | https://uk.farnell.com/nxp/u-cyclone-fx/cyclone-programmer-arm-cortex/dp/2820399 |
| PEM-04 | 接続・書き込み | Multilink FX | NXP の多数のアーキテクチャを 1 台で。1.6〜5.25 V、3.3/5 V 給電。セミホスティング、SWO のデータ/イベント捕捉、リアルタイム式表示 | https://www.nxp.jp/docs/en/fact-sheet/U-MULTILINK-FXFS.pdf |
| GHS-01 | 故障解析 | TimeMachine（逆実行デバッグ） | トレースから実行を再構成し、障害発生後にプログラムを逆方向に実行・ステップ。ウォッチポイントを置いて逆実行すると、メモリ破壊の書き込み元に戻れる。マルチコア（MIPS・RH850/V850） | https://www.ghs.com/news/20030902_time_machine.html / https://www.ghs.com/news/20130416_timemachine_multicore.html |
| GHS-02 | トレース | SuperTrace / Probe V4 | 最大 8 GB（SuperTrace）/ 4 GB（Probe V4）のトレースを非侵襲で記録、300 MHz 超のトレースポート | https://ghs.com/products/probe.html |
| GHS-03 | 時間軸の解析 | PathAnalyzer | 関数呼び出しの系列をコールスタック順に時間軸で可視化し、性能のボトルネックを探す | https://www.ghs.com/news/20030902_time_machine.html |
| WR-01 | 故障解析 | Simics: チェックポイント | システム全体の状態を保存し、他の開発者がそのまま読み込んで調べられる | https://blogs.windriver.com/themes/Windriver/pdf/Wind-River-Simics_Product-Overview.pdf |
| WR-02 | 故障解析 | Simics: システム全体の逆実行 | 全体を前後にステップ（チェックポイントへ戻って再実行する方式） | https://learning.windriver.com/working-with-simics-48 / https://www.sourceware.org/ml/gdb/2010-09/msg00001.html |
| WR-03 | 故障解析 | Simics: 故障注入 | メモリのビット固着などの故障を模擬環境で注入 | https://blogs.windriver.com/themes/Windriver/pdf/Wind-River-Simics_Product-Overview.pdf |
| WR-04 | 自動化・連携 | Workbench と TRACE32 の連携 | TCF 経由で Workbench から TRACE32 を使う（T32-19 と同じ） | https://repo.lauterbach.com/intwindriver.html |

---

## 7. Percepio Tracealyzer

| ID | 分類 | 機能 | 具体的な内容 | 出典 URL |
|---|---|---|---|---|
| TZ-01 | 時間軸の解析 | Trace View | スレッド実行・割り込み・カーネル API 呼び出し・ユーザイベントの時間軸（縦/横） | https://percepio.com/tracealyzer/features-capabilities/ |
| TZ-02 | 性能・カバレッジ | CPU Load Graph | タスク・割り込みごとの CPU 使用率と合計 | https://percepio.com/tracealyzer/features-capabilities/ |
| TZ-03 | 時間軸の解析 | Event Log・検索 | 絞り込める全イベント一覧、Ctrl-F、SQL 風の Finder | https://percepio.com/tracealyzer/features-capabilities/ |
| TZ-04 | RTOS | Communication Flow | キュー・セマフォ・ミューテックスを介したスレッド間の依存を有向グラフで要約 | https://percepio.com/tracealyzer/features-capabilities/ |
| TZ-05 | 止めずに見る | Data Plot | ユーザイベントの値を時間軸でグラフ化 | https://percepio.com/tracealyzer/features-capabilities/ |
| TZ-06 | 故障解析 | Heap / Stack Memory Usage | malloc/free の推移（リーク確認）、スレッドごとの未使用スタックの推移 | https://percepio.com/tracealyzer/features-capabilities/ |
| TZ-07 | 性能・カバレッジ | ジョブ単位の時間指標 | ジョブ（インスタンス）ごとの実行時間・応答時間・周期のグラフと最悪値 | https://percepio.com/tracealyzer/features-capabilities/ |
| TZ-08 | 時間軸の解析 | Interval・State Machine・Runnable | 2 イベント間の時間（スレッド・割り込みをまたぐ）の統計とプロット（ソフトウェアロジアナ）、状態遷移の図示、スレッド内のコード区間の実行時間（横取りを除く） | https://percepio.com/tracealyzer/features-capabilities/ / https://help.intervalzero.com/product_help/RTX64_4/Content/Topics/Tracealyzer/RTX64%203.6%20and%20Later/TracealyzerViews.html |
| TZ-09 | RTOS | 高度な RTOS 解析 | IPC 遅延・ブロック時間・動的優先度変化・キュー使用率・サービス呼び出し頻度 | https://percepio.com/tracealyzer/features-capabilities/ |
| TZ-10 | 時間軸の解析 | ストリーミング / スナップショット | ストリーミング（J-Link RTT・ITM・TCP/IP・UDP、他はカスタマイズ）で長時間記録と実時間アニメーション表示。スナップショットは RAM のリングバッファを必要時に取得（専用ハード不要） | https://percepio.com/tracealyzer/features-capabilities/ / https://FreeRTOS.org/trace |
| TZ-11 | その他 | Compact Logging（ELF） | 文字列定数をアドレスで記録し、ホストが ELF で復元 | https://percepio.com/tracealyzer/features-capabilities/ |
| TZ-12 | 時間軸の解析 | マルチコア・巨大トレースのプレビュー | SMP の統合トレース、ストリーミング時の索引で巨大トレースの概観と部分読み込み | https://percepio.com/tracealyzer/features-capabilities/ |
| TZ-13 | 自動化・連携 | 出力・SDK | イベントログ・プロット・統計を text / XML / CSV で出力。SDK で独自 RTOS やライブラリに対応 | https://percepio.com/tracealyzer/features-capabilities/ |
| TZ-14 | RTOS | 対応 OS | FreeRTOS、Zephyr、ThreadX、PX5、SafeRTOS、LynxOS-178、VxWorks、Linux、ベアメタル | https://percepio.com/tracealyzer/ |
| TZ-15 | 故障解析 | DevAlert / Detect（関連製品） | 現場の機器で異常（ハードフォルト・性能異常）時にトレースを保存しクラウドへ。遠隔でソースレベル解析 | https://percepio.com/tracealyzer/ |

---

## 8. Memfault（現場の故障収集・解析）

| ID | 分類 | 機能 | 具体的な内容 | 出典 URL |
|---|---|---|---|---|
| MF-01 | 故障解析 | コアダンプ収集 | ハング・クラッシュ・assert 時のバックトレース・故障レジスタを保存しクラウドへ。Cortex-M/R、ESP32 | https://docs.memfault.com/docs/mcu/introduction |
| MF-02 | 故障解析 | サーバ側のシンボル解決 | ソフトのバージョンごとに 1 つの ELF（シンボル）を登録し、クラッシュデータをサーバで解決（デバイスに名前を持たせない） | https://docs.memfault.com/docs/platform/memfault-terminology |
| MF-03 | 故障解析 | 問題（Issue）への自動グルーピングと通知 | トレースを Issue にまとめ、問題検出時に通知。グルーピング規則の詳細は未確認 | https://docs.memfault.com/docs/mcu/introduction |
| MF-04 | 故障解析 | リブート理由の追跡 | クラッシュ・ブラウンアウト・更新・タスクウォッチドッグ等の理由を記録し集計 | https://docs.memfault.com/docs/mcu/introduction / https://components.espressif.com/components/memfault/memfault-firmware-sdk/versions/1.18.0/changelog |
| MF-05 | 止めずに見る | メトリクス（ハートビート） | 電池・接続・性能の値を周期送信し、機器ごとの推移とフリート全体のダッシュボード | https://docs.memfault.com/docs/mcu/introduction |
| MF-06 | 故障解析 | トレースイベント | 40 バイト未満の軽量イベント（Flash 書き込み失敗・I2C タイムアウト等）で頻度を把握 | https://docs.memfault.com/docs/mcu/introduction |
| MF-07 | その他 | Compact Logs | ログの書式をリンカセクションに置いて圧縮記録し、コアダンプから復元 | https://docs.memfault.com/docs/mcu/compact-logs |
| MF-08 | その他 | Custom Data Recording | 任意のバイナリを送る（1 台 24 時間に 1 件、16 MB まで） | https://docs.memfault.com/docs/mcu/custom-data-recording |
| MF-09 | 自動化・連携 | 任意の転送路（チャンク） | 最小 9 バイトのチャンクに分け、BLE / LTE / Wi-Fi / LoRa / 独自プロトコルで送る。SDK は Flash 約 5 KB・RAM 約 1.5 KB | https://docs.memfault.com/docs/mcu/introduction |
| MF-10 | 故障解析 | Linux 版（memfaultd） | プロセスのクラッシュでコアダンプ、取得範囲の方針（スタックだけ / スレッド / カーネル選択）、直前のログ行の添付、OTA | https://docs.memfault.com/docs/linux/coredumps |

---

## 9. 横断のまとめ

### 9.1 複数のツールに共通する機能（「当たり前」の水準）

| 機能 | 持っているツール | fprobe への含意 |
|---|---|---|
| 高速・安定した Flash 書き込み（デバイス別ローダ、外部 QSPI） | J-Link、I-jet、ULINK、TRACE32、PEmicro | 速度の比較表は SEGGER が自ら公表している（SEG-04）。同条件で測って示す必要がある |
| ソフトウェア / Flash ブレークポイント（HW 資源の不足を補う） | J-Link（Unlimited）、I-jet（Flash BP） | 必須級 |
| データ（ウォッチ）ブレークポイント・ベクタキャッチ | 全デバッガ | 必須 |
| 走行中の変数表示（ライブウォッチ / HSS / データサンプリング） | J-Link（HSS・J-Scope・Ozone）、C-SPY Live Watch、µVision、TRACE32 SNOOPer | 「止めずに見る」の基本 |
| SWO（ITM printf・PC サンプル・例外・データトレース）の受信と表示 | J-Link、I-jet、ULINK、PEmicro、TRACE32 | 必須 |
| RAM バッファ経由のログ・イベント（RTT、Event Recorder、LOGGER/FDX、Tracealyzer スナップショット） | SEGGER、Keil、Lauterbach、Percepio | RTT 互換は事実上の標準 |
| ETM 命令トレースとそれによるカバレッジ・プロファイル | J-Trace、I-jet Trace、ULINKpro、TRACE32、BlueBox、GHS、DSTREAM | 大きな差のある領域（fprobe はトレースポート対応が課題） |
| 関数単位のプロファイル（サンプリングまたはトレース） | Ozone、C-SPY、µVision、TRACE32、winIDEA、Streamline | 必須級 |
| RTOS 認識（タスク・キュー・スタック） | Ozone、C-SPY、µVision、TRACE32、Arm DS、Embedded Studio | 必須級 |
| RTOS の時間軸可視化（タスク切替・CPU 負荷） | SystemView、Tracealyzer、winIDEA、µVision System Analyzer | 共通タイムスタンプと相性が良い |
| 電流計測とコードの対応付け | J-Link（50 µA）、I-jet（160 µA）、I-scope、ULINKplus（200 nA）、TRACE32 Analog Probe、Arm Energy Probe | 分解能の差が大きい。µA 級は ULINKplus だけ |
| スクリプト・CLI・外部 API（CI 用） | J-Link Commander / SDK、cspybat・マクロ、µVision スクリプト、PRACTICE / pyrcl、isystem.connect | 終了コード・バッチ実行は必須 |
| GDB サーバ | J-Link GDB Server、（Arm DS / PEmicro 等） | 必須 |
| ネットワーク越しのプローブ利用 | J-Link Remote Server（トンネル）、J-Link Pro（Ethernet） | 遠隔・AI 用途に直結 |
| 量産書き込み（スタンドアロン・ギャング・回数制限・暗号化・シリアル番号） | SEGGER Flasher、PEmicro Cyclone | 本業とは別市場 |
| 命令セットシミュレータ / 仮想プラットフォーム | C-SPY、Embedded Studio emSim、Ozone-Sim、µVision / FVP、Simics | ハード無しの試験 |

### 9.2 特定ツールにしかない（または際立つ）差別化機能

| 機能 | ツール | 要点 |
|---|---|---|
| Unlimited Flash Breakpoints（書き換え最小化の工夫つき） | SEGGER J-Link | セクタまとめ・キャッシュ・ISS で Flash 書き換えを減らす（SEG-15） |
| RTT の制御ブロック自動探索＋Telnet 公開 | SEGGER J-Link | 設定なしで走行中のターゲットとつながる（SEG-22, 23） |
| Remote Server のトンネルモード | SEGGER J-Link | NAT 越しに無料の中継で遠隔デバッグ（SEG-33） |
| Monitor Mode Debugging | SEGGER J-Link | 止めている間も高優先度割り込みが動く（SEG-27） |
| Debug Snapshots（状態の保存と復元・オフライン解析） | SEGGER Ozone | RAM・Flash・周辺・時間軸を保存して後で再開（SEG-59） |
| 無制限ストリーミングトレース＋ライブカバレッジ | SEGGER J-Trace PRO（ULINKpro も同種） | 実行中にカバレッジ・プロファイルが更新（SEG-47, 49） |
| 電力しきい値で CPU を停止する Power ブレークポイント | IAR C-SPY（プローブ依存） | 電流の異常で止める（IAR-05） |
| C-RUN（境界・ヒープ・算術の実行時検査） | IAR | デバッガと統合された実行時エラー検査（IAR-29, 30） |
| Fault exception viewer | IAR | Cortex-M の故障原因を自動表示（IAR-26） |
| Event Statistics（区間ごとの時間とエネルギー） | Keil（ULINKplus） | コード区間単位の消費エネルギー（KEIL-02, 13） |
| nA 級の電流計測と試験用 I/O・絶縁 | Keil ULINKplus | 200 nA 分解能、GPIO / アナログ I/O を試験台の制御に（KEIL-12, 14） |
| CTS（トレースからの状態再構成・逆ステップ） | Lauterbach TRACE32 | 過去の変数まで見える逆方向デバッグ（T32-08） |
| ロジアナ（PowerIntegrator）とプログラムトレースの同期 | Lauterbach TRACE32 | 204 ch・500 MHz・48 bit タイムスタンプ（T32-11） |
| 機能安全向けトレースカバレッジ（MC/DC、ツール認定キット） | Lauterbach TRACE32 | 計装を最小化した MC/DC（T32-10） |
| Profiler Inspectors（タイミング制約違反の自動検出） | winIDEA | 記録から派生イベントを生成（WIN-03） |
| CAN/LIN・ADIO とトレースの同期 | winIDEA BlueBox | バス・アナログとプログラムの相関（WIN-06） |
| TimeMachine（逆実行）＋大容量トレース | Green Hills | 逆実行＋ウォッチポイントで破壊元へ（GHS-01, 02） |
| システム全体のチェックポイント・逆実行・故障注入 | Wind River Simics | 仮想プラットフォームならでは（WR-01〜03） |
| 量産イメージの暗号化・回数/期限制限 | PEmicro Cyclone、SEGGER Flasher Secure | 委託生産の保護（PEM-02、SEG-44, 45） |
| Communication Flow・Interval・State Machine 解析 | Percepio Tracealyzer | RTOS の依存関係・区間時間の統計（TZ-04, 08） |
| フリートからのコアダンプ収集と Issue 化・サーバ側シンボル解決 | Memfault | 開発後（現場）の故障解析（MF-01〜03） |

### 9.3 調査で確認できなかった主な事項（未確認）

- J-Link の電流計測レンジ、J-Trace PRO のトレースポート幅とバッファ容量、STRACE の仕様。
- J-Flash 単体のシリアル番号・パッチ・CRC 比較の扱い（UM08003 未読）。
- I-jet Trace の仕様、C-SPY の条件付きブレークポイントの細目、Arm 版マルチコアの詳細。
- ULINKplus のサンプルレートの公式値（販売店・コミュニティ情報のみ）。
- Arm Development Studio の現行スクリプト API・スナップショットビューア。
- TRACE32 の個々のコマンド名（Trace.STATistic 等）と RTOS 対応一覧（チュートリアルの章題でのみ確認）。
- winIDEA の電力計測、トリガの仕様。
- Memfault の Issue グルーピング規則。
