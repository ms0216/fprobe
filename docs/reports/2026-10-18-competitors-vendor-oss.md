# 競合調査: 半導体メーカーの開発環境・OSS・RTOS 解析・ロジアナソフト・国産ツールの機能一覧

調査日: 2026-10-17〜18。目的: fprobe（FPGA 製の J-Link 上位互換プローブ。ロジアナとデバッガを一体化）が「どの機能を持てば上位互換になるか」を判断するために、
各ツールで利用者が実際に使える機能を、機能単位で洗い出す。

## 凡例と調べ方

- 分類: 接続・書き込み / 止めて調べる / ブレークポイント / 止めずに見る / トレース / 時間軸の解析 / RTOS / 性能・カバレッジ / 電力 / 故障解析 / ロジアナ・解読 / 自動化・連携 / UI・UX / その他。
- 出典は、今回 Web 上で本文または検索結果の抜粋を確認できたページを示す。
- **※** が付いた行は、今回の調査では本文まで確かめられず、公式文書の名前や章の場所だけを確認できたもの。正確な条件は再確認が必要。
- 「未確認」は、今回の調査では有無を確かめられなかったこと。推測では埋めていない。
- J-Link・SEGGER の各ツール（Ozone・SystemView・J-Scope 等）は別の報告（`2026-10-17-jlink-comparison.md`）の対象なので、ここでは他のツールとの関係で触れるだけにする。

---

## 1. STMicroelectronics: STM32CubeIDE / STM32CubeMonitor / STM32CubeProgrammer

### 1.1 STM32CubeIDE（デバッガ部分）

| ID | 分類 | 機能 | 具体的な内容（何ができるか、条件・制約・必要なハード） | 出典 URL |
|---|---|---|---|---|
| CUBE-01 | 接続・書き込み | 複数の GDB サーバ | ST-LINK GDB server・OpenOCD・SEGGER J-Link を選んで接続する。※ | https://dca.fee.unicamp.br/ftp/docs/ea701/cubeide/um2609-stm32cubeide-user-guide-stmicroelectronics.pdf |
| CUBE-02 | 止めて調べる | コアレジスタ・メモリ・周辺レジスタ（SFR）の表示 | CPU コアレジスタ、メモリ、周辺レジスタ（SVD に基づく）をビューで見る・書く。 | 同上（UM2609） |
| CUBE-03 | 止めずに見る | Live Expressions（実行中の変数監視） | ターゲットを止めずに式・変数の値を周期的に読んで表示する。デバッグポートのバックグラウンドメモリアクセスを使う。 | 同上（UM2609） |
| CUBE-04 | トレース | SWV（Serial Wire Viewer）の各ビュー | SWO 経由で ITM/DWT のデータを受ける。ITM Data Console（printf）、Trace Log、Exception Trace、Data Trace、Data Trace Timeline Graph（変数の時系列グラフ。X 軸を秒/サイクルで切替）、Statistical Profiling（PC サンプリング）。SWO 端子のある Cortex-M3/M4/M7/M33 等が条件。一部の安価な互換 ST-LINK では動かない報告あり。 | https://community.st.com/stm32cubeide-mcus-28/stm32cubeide-how-to-use-swv-data-trace-timeline-graph-65763 / UM2609 |
| CUBE-05 | 故障解析 | Fault Analyzer | HardFault 等の発生時に、CFSR/HFSR 等の故障レジスタを解読し、原因（例: 不正アクセス、ゼロ除算）と発生箇所のコード位置・スタックフレームを示す。 | UM2609（索引 3-10） |
| CUBE-06 | 性能・カバレッジ | Build Analyzer | ビルド結果（ELF/MAP）から、メモリ領域ごとの使用量・シンボルごとのサイズを表示する。 | UM2609 / https://forum.digikey.com/t/stm32cubeide-build-analyzer/15502 |
| CUBE-07 | 性能・カバレッジ | Static Stack Analyzer | コンパイラの情報から関数ごとのスタック使用量と呼び出し経路の最大値を静的に見積もる。 | UM2609 |
| CUBE-08 | RTOS | FreeRTOS / ThreadX のカーネルオブジェクト表示 | タスク一覧（優先度・状態・スタック）、キュー、セマフォ、タイマ等のビュー。名前の無いオブジェクトは出ない、約 64 個まで等の制約が報告されている。RTOS Proxy でスレッドごとの呼び出し履歴。 | https://community.st.com/stm32-mcus-embedded-software-32/rtos-debug-with-stm32cubeide-19016 / https://community.st.com/stm32-mcus-embedded-software-32/rtos-debugging-with-cubeide-164365 |
| CUBE-09 | 止めて調べる | マルチコアのデバッグ | デュアルコア品（STM32H7 デュアル、STM32MP1 の M4 等）で各コアのセッションを並行で動かす。※ | UM2609 |
| CUBE-10 | UI・UX | VS Code 版（STM32CubeIDE for VS Code） | VS Code 拡張でのビルド・デバッグ。RTOS ビューは ThreadX の一部のみ対応と ST 社員が回答（2025-10）。 | https://community.st.com/stm32cubeide-for-visual-studio-code-mcus-133/threadx-is-not-supported-in-vscode-rtos-views-158388 |

### 1.2 STM32CubeMonitor 系

| ID | 分類 | 機能 | 具体的な内容 | 出典 URL |
|---|---|---|---|---|
| CUBE-11 | 止めずに見る | 実行中の RAM 変数の読み書き | ELF のデバッグ情報から変数名を取り、ST-LINK 経由で実行中に読み書きする（非侵入）。モータ制御等の止められない用途での調整に使う。 | https://www.st.com/en/development-tools/stm32cubemonitor.html / https://newsroom.st.com/?p=1053 |
| CUBE-12 | 止めずに見る | Direct / Snapshot の 2 つの取得方式 | Direct: デバッグポートで周期的に読む。Snapshot: ターゲット側のコードがバッファに貯めた値をまとめて読む（高速な変化を取りこぼさない）。 | 同上（製品ページ） |
| CUBE-13 | 時間軸の解析 | トリガ | 条件を満たしたときから取得を始める。 | 同上 |
| CUBE-14 | 自動化・連携 | Node-RED ベースのフロー | 取得・処理・表示を Node-RED のフローとして組む（設計モード／ダッシュボードモード）。汎用の Node-RED ノードと ST 独自ノードを併用。 | https://wiki.st.com/stm32mcu/wiki/STM32CubeMonitor:Introduction_and_main_principles_of_STM32CubeMonitor |
| CUBE-15 | UI・UX | ダッシュボード・遠隔表示 | ゲージ・棒グラフ・プロット等の部品。Web ブラウザから遠隔で見られる。 | 同上 |
| CUBE-16 | 自動化・連携 | 記録と再生 | 取得データをファイルに記録し、あとで再生する。 | 製品ページ |
| CUBE-17 | 電力 | STM32CubeMonitor-Power | STLINK-V3PWR（数 nA〜500 mA、1 SPS〜100 kSPS）、X-NUCLEO-LPM01A、STM32L562E-DK の電流計で電流を取得・グラフ化。間引き表示でもデータは落とさない。期間指定または無期限の記録。ULPBench を実行して ULPMark を推定。 | https://www.st.com/en/development-tools/stm32cubemonpwr.html |
| CUBE-18 | その他 | 用途別の派生版 | Power、RF、USB-PD（UCPD）向けの派生版がある。 | https://www.st.com/en/development-tools/stm32cubemonitor.html |

### 1.3 STM32CubeProgrammer

| ID | 分類 | 機能 | 具体的な内容 | 出典 URL |
|---|---|---|---|---|
| CUBE-19 | 接続・書き込み | 多様な接続 | JTAG・SWD に加え、ブートローダ経由の USB DFU・UART・SPI・CAN・I2C で書き込む。 | https://hhoegl.informatik.hs-augsburg.de/st/STM-CubeProgrammer-2.9.0/um2237-stm32cubeprogrammer-software-description-stmicroelectronics.pdf |
| CUBE-20 | 接続・書き込み | オプションバイト編集 | 読み出し保護（RDP）・書き込み保護・ブート設定等を GUI と CLI で読み書き。 | 同上（UM2237 2.4 節） |
| CUBE-21 | 接続・書き込み | 外部フラッシュ用ローダ | 外付け QSPI/OSPI 等のフラッシュ用ローダ（External Loader）を使って書く。独自ローダの作り方も文書化。 | 同上 |
| CUBE-22 | 自動化・連携 | CLI と API | GUI と同等の操作をコマンドラインから。C++ の API も提供。※（API の詳細は未確認） | 同上 |
| CUBE-23 | 接続・書き込み | セキュアな書き込み（SFI/SSP 等） | 暗号化したファームウェアを工場で安全に書く仕組み。※（今回は本文未確認） | 同上 |
| CUBE-24 | トレース | SWV 表示 | SWO の printf を表示する簡易ビュー。※ | 同上 |

---

## 2. NXP: MCUXpresso IDE / LinkServer / MCU-Link Pro

| ID | 分類 | 機能 | 具体的な内容 | 出典 URL |
|---|---|---|---|---|
| MCUX-01 | 接続・書き込み | LinkServer・J-Link・PEmicro の 3 系統 | NXP 自社の LinkServer（CMSIS-DAP の MCU-Link / LPC-Link2）に加え J-Link・PEmicro で接続。 | https://hepialsn.hesge.ch/mylab2/outils/mcuxpresso/MCUXpresso_IDE_User_Guide.pdf |
| MCUX-02 | トレース | SWO Profile（PC サンプリング） | SWO の PC サンプルから関数ごとの実行時間比率を出す。 | 同上 / https://cn.nxp.com/docs/en/application-note/AN14071.pdf |
| MCUX-03 | トレース | SWO Interrupt Trace | 割り込みの入退出を時系列で表示し、割り込みごとの回数・時間を集計。 | 同上（User Guide） |
| MCUX-04 | トレース | SWO Data Watch | DWT のデータ監視で変数への読み書きを、値と時刻つきで記録。 | 同上 |
| MCUX-05 | トレース | SWO ITM Console | ITM 経由の printf/scanf。プロジェクト生成時に出力先を ITM に切り替えられる。 | https://community.nxp.com/t5/MCUXpresso-IDE-Knowledge-Base/tkb-p/mcuxpresso-ide@tkb |
| MCUX-06 | トレース | 実行中のトレース開始・停止 | LinkServer では CPU を止めずに SWO トレースを開始・停止できる（J-Link では一瞬止める必要がある、と NXP が記載）。 | https://cn.nxp.com/docs/en/application-note/AN13234.pdf |
| MCUX-07 | 電力 | Energy Measurement | MCU-Link Pro（またはボード搭載の同等回路）で電流を測る。分解能 200 nA 未満・代表精度 1%。IDE 11.4.1 以降。LPC-Link2 や他社プローブでは使えない。 | https://nxp.com/design/software/development-software/mcuxpresso-software-and-tools-/mcu-link-pro-debug-probe:MCU-LINK-PRO / https://www.farnell.com/datasheets/3594598.pdf |
| MCUX-08 | 電力 | Power Profile（電力とコードの対応づけ） | SWO の PC サンプルと電力測定を時刻で突き合わせ、どのコードが電力を食うかを示す。MCU-Link Pro 等が必須。 | https://cache.nxp.com/docs/en/quick-reference-guide/MCUXpresso_IDE_Power_Profile.pdf（検索結果で確認。現在は 404） |
| MCUX-09 | ロジアナ・解読 | アナログ信号モニタ | MCU-Link Pro にアナログ入力（1.2〜3.6 V）。デジタルポート／アナログ入力用ケーブル付属。IDE での表示方法は未確認。 | MCU-Link Pro 製品ページ |
| MCUX-10 | 接続・書き込み | プローブのブリッジ機能 | MCU-Link Pro: VCOM（USB-UART）、USB-I2C/SPI ブリッジ、ターゲット給電 1.8/3.3 V・最大 400 mA、対応電圧 1.2〜5 V、ファームウェアを CMSIS-DAP / J-Link LITE で切替。 | 同上 |
| MCUX-11 | 自動化・連携 | LinkServer のスクリプト | 接続・回復手順をスクリプトで記述（ロックされた MCU の回復等）。 | https://mcuoneclipse.com/category/lpc/page/3/ |
| MCUX-12 | 止めずに見る | 実行中のグローバル変数表示 | Global Variables ビューで実行中に値を読み、グラフ化する。※ | User Guide |
| MCUX-13 | RTOS | FreeRTOS 等のカーネル認識（TAD） | タスク・キュー・タイマの表示。※ | User Guide |
| MCUX-14 | UI・UX | MCUXpresso for VS Code | VS Code 版でも SWO（ITM・プロファイル等）を表示。Cortex-M0/M0+ は SWO なし。 | https://docs.mcuxpresso.nxp.com/mcux-vscode/26.08/SWO.html |

---

## 3. Silicon Labs: Simplicity Studio（Energy Profiler・Network Analyzer）

| ID | 分類 | 機能 | 具体的な内容 | 出典 URL |
|---|---|---|---|---|
| SLS-01 | 電力 | Energy Profiler（AEM による実時間の電流測定） | 開発キット（WSTK 等）の AEM 回路で電流を連続測定し、波形・統計を表示。 | https://docs.silabs.com/simplicity-studio-5-users-guide/1.0/using-the-tools/energy-profiler/ |
| SLS-02 | 電力 | 電流とプログラムカウンタの対応づけ（Code Correlation） | 電流の測定値をそのときの PC と結びつけ、関数・ソース行ごとのエネルギー消費を示す。 | https://www.silabs.com/developer-tools/simplicity-studio/energy-profiler |
| SLS-03 | 電力 | 無線の送受信との対応づけ | PTI（Packet Trace Interface）で得た送受信区間を電流波形に重ねる。 | 同上 |
| SLS-04 | 電力 | 複数ノードの同時測定 | Multi-Node Energy Profiler（.isd ファイル）で複数ボードを同じ時間軸で測る。 | https://docs.silabs.com/energy-profiler/6.0.96/assets/energy-profiler.pdf |
| SLS-05 | ロジアナ・解読 | Network Analyzer（無線パケット解析） | PTI 経由で無線チップ自身が送受信したパケットを、ファームウェアに関係なく取得。各パケットに時刻・LQI・RSSI・CRC 結果。時刻は WSTK 側で付ける。 | https://www.silabs.com/blog/simplify-wireless-debug-with-silicon-labs-network-analyzer |
| SLS-06 | 時間軸の解析 | 複数ノードの統合キャプチャ | 複数の送受信点から同じログに重複なく統合（"perfect trace"）。リアルタイムのタイムライン、ネットワーク地図、トランザクションへの自動グループ化、セッション保存。 | 同上 / https://docs.silabs.com/network-analyzer/latest/network-analyzer-capturing-data-managing-sessions/ |
| SLS-07 | 時間軸の解析 | Energy Profiler と Network Analyzer の連携 | 基本的な連携（同じ .isd 形式・時間軸）。詳細は未確認。 | https://docs.silabs.com/simplicity-studio-5-users-guide/1.0/using-the-tools/energy-profiler/ |

---

## 4. Renesas: e² studio

| ID | 分類 | 機能 | 具体的な内容 | 出典 URL |
|---|---|---|---|---|
| E2S-01 | 止めずに見る | 実行中のメモリ表示 | エミュレータ（E2 / E2 Lite 等）のハードウェア機能で、実行中にメモリを表示。 | https://www.renesas.com/en-us/products/software-tools/tools/ide/e2studio.html |
| E2S-02 | トレース | リアルタイムトレース | エミュレータ・MCU 内蔵のトレースバッファで分岐・サイクルを記録（例: E1 で 256 分岐分）。 | 同上 / https://www.renesas.com/br/ja/document/apn/integrated-development-environment-e-studio-usage-trace-debug-features-rev100 |
| E2S-03 | ブレークポイント | ハードウェアイベントによるブレーク | ルネサス MCU のイベント検出（アドレス・データ条件）でブレーク。 | 製品ページ |
| E2S-04 | 性能・カバレッジ | Performance Analysis | 区間の実行サイクル計測（v2.0 で追加）。 | https://www.renesas.com/en/about/newsroom/renesas-electronics-america-adds-powerful-debugging-and-enhanced-usability-next-generation-e-studio |
| E2S-05 | RTOS | RTOS Resources ビュー | Partner OS Debugging Plug-in。FreeRTOS 等のタスク・キュー・タイマ・スタック。v7.8.0 でタスクごとのスタック使用量をデバッグ中に実時間表示。 | https://renesas.com/en/document/apn/e-studio-partner-rtos-aware-debugging-rx / https://renesas.com/ja/document/tnr/upgraded-revision-e-studio-integrated-development-environment-v780 |
| E2S-06 | 性能・カバレッジ | シミュレータのカバレッジ | RL78 等のシミュレータでブレーク・カバレッジ計測。 | https://www.renesas.com/sg/en/products/software-tools/tools/simulator/e2studio-simulator-for-rl78-family.html |
| E2S-07 | 止めずに見る | リアルタイムチャート（変数の時系列表示） | 未確認（今回の調査では公式記述を見つけられなかった）。 | — |

---

## 5. TI: Code Composer Studio（EnergyTrace・ROV）

| ID | 分類 | 機能 | 具体的な内容 | 出典 URL |
|---|---|---|---|---|
| CCS-01 | トレース | Core Trace | 実行命令のアドレスを記録しソース・逆アセンブルと対応づける。方式は MTB（MSPM0、4 エントリ・時刻なし）、ERAD（C2000、1〜4 KB）、ETB（Cortex-A72/A53/R5/一部 M4、C66x、C7x。オペコード・サイクル数）。 | https://software-dl.ti.com/ccs/esd/documents/users_guide_ccs_20.5.1/ccs_debug-trace.html |
| CCS-02 | トレース | Custom Core Trace | ETB で受信バッファ（循環／満杯で停止）、アドレス範囲、開始・停止トリガアドレスを指定。 | 同上 |
| CCS-03 | 電力 | EnergyTrace | MSP430 と EnergyTrace 対応 XDS110 で電流を測る。Overview・Current（nA）グラフ、Summary 表（CSV 出力）、Records 表。既定の最大取得 10 秒。 | 同上 |
| CCS-04 | 電力 | EnergyTrace++ | MSP430FR59xx/69xx・CC13xx/CC26xx で、電力と CPU の低電力モード・周辺/クロックの ON/OFF 状態を対応づけ（States ビュー）。CC13xx/26xx は 4 線 cJTAG が必要。 | 同上 |
| CCS-05 | その他 | トレース機能の排他 | 同時に有効にできるトレース機能は 1 つだけ。デスクトップ（Windows/Linux）のみ。 | 同上 |
| CCS-06 | RTOS | ROV（Runtime Object View） | TI-RTOS/SYS/BIOS 等のスレッド・ヒープ・CPU 負荷をメモリから読んで表示。JTAG ではコード追加なし・実行を乱さない。実行中に読める。停止時に自動更新。拡張可能。 | https://software-dl.ti.com/ccs/esd/documents/rov_guide/html/src/rov.html |
| CCS-07 | 止めずに見る | 実行中のメモリ・レジスタアクセス | デバイス実行中に非侵入でメモリとレジスタを読む。 | https://www.ti.com/ccs |
| CCS-08 | UI・UX | 配列のグラフ表示 | メモリ上の配列をグラフで表示（波形・FFT 等）。 | 同上 |
| CCS-09 | 自動化・連携 | スクリプト API（JavaScript / Python） | デバッグ操作をスクリプトで自動化。Python API は 20.5 以降。 | https://software-dl.ti.com/ccs/esd/documents/users_guide_ccs_20.5.1/index_debug-scripting.html |
| CCS-10 | 自動化・連携 | AI コーディング支援の章 | 20.5.0 のユーザガイドに "AI Coding Assistants" の節が追加。内容は未確認。 | 同上 |

---

## 6. Microchip: MPLAB X IDE（Data Visualizer）

| ID | 分類 | 機能 | 具体的な内容 | 出典 URL |
|---|---|---|---|---|
| MPLB-01 | 止めずに見る | Data Visualizer のデータ取得 | 仮想 COM または DGI（Data Gateway Interface。Curiosity Nano・Xplained Pro・Power Debugger・Atmel-ICE）でデータを受ける。 | https://developerhelp.microchip.com/xwiki/bin/view/software-tools/x/plugins/microchip-plugins/data-visualizer |
| MPLB-02 | 止めずに見る | DVRT（Data Visualizer Run Time）で変数を読み書き | グローバル変数をストリーミングで読み書きする。Data Stream Protocol で受信データのフィールドを実行時に解読。 | https://onlinedocs.microchip.com/oxy/GUID-4FF3C687-0C30-4D21-82D0-5AE401E8BE9D-en-US-11/GUID-D722CA26-1771-4A8A-B4CE-26098FE7FD88.html |
| MPLB-03 | 時間軸の解析 | グラフ（時系列・XY・簡易オシロ） | 時間スクロール、エッジトリガつき簡易オシロ、カーソル測定、CSV/JSON でスナップショット保存。 | 同上 |
| MPLB-04 | 電力 | 電力の監視 | Power Debugger 等で電力を表示し、最大・最小・平均を区間で見る。 | https://www.microchip.com/en-us/tools-resources/debug/atmel-data-visualizer |
| MPLB-05 | 止めずに見る | データ取得とデバッグの同時実行 | MPLAB X でデバッグしながら同時にストリーミング。データ点とソース行の対応づけは未確認。 | https://gallery.microchip.com/packages/MPLAB-Data-Visualizer-Plugin/1.4.1926 |
| MPLB-06 | その他 | シミュレータと Stimulus | 外部信号の注入、ピン・レジスタ値の周期的設定、実行トレース、データ書き出し。 | https://developerhelp.microchip.com/xwiki/bin/view/software-tools/x/debugging/simulator/ |
| MPLB-07 | 性能・カバレッジ | Code Coverage | 実行された／されなかった部分を表示（シミュレータ等）。 | https://developerhelp.microchip.com/xwiki/bin/view/software-tools/x/debugging/code-coverage/ |
| MPLB-08 | 止めずに見る | DMCI（Data Monitor and Control Interface） | 未確認（今回の調査では資料を見つけられなかった）。 | — |

---

## 7. Nordic: nRF Connect・PPK2

| ID | 分類 | 機能 | 具体的な内容 | 出典 URL |
|---|---|---|---|---|
| NRF-01 | 電力 | PPK2 の電流測定 | 100 kSPS、約 200 nA〜1 A（下限 500 nA とする資料もある）。 | https://www.mouser.com/datasheet/2/297/Power_Profiler_Kit_II_PB_v1_0-1928294.pdf |
| NRF-02 | 電力 | Source / Ampere meter の 2 モード | Source: 0.8〜5 V・最大 1 A を供給しつつ測る（SMU）。Ampere: 外部電源の経路に入れて測る。 | 同上 / https://www.cnx-software.com/2020/12/02/power-profiler-kit-ii-measures-power-consumption-in-nordic-semi-based-embedded-systems/ |
| NRF-03 | ロジアナ・解読 | 8 本のデジタル入力 | 電流波形と同じ時間軸で 8 ch のロジック状態を記録（簡易ロジアナ）。ファームウェアの GPIO で区間を示せる。 | 同上 |
| NRF-04 | UI・UX | Power Profiler アプリ | nRF Connect for Desktop の GUI。測定データを出力して後処理。 | 同上 |
| NRF-05 | RTOS | Thread Viewer（nRF Connect for VS Code） | Zephyr のスレッド情報を表示。デバッグ停止時のみ更新（実行中は古い値のまま）。 | https://docs.nordicsemi.com/r/NexOA~Hcq7~UmFj_rM~Nsw/iRdhFEWoolO6BxhS7dmGIg |
| NRF-06 | 止めずに見る | RTT 端末 | VS Code 拡張に RTT 端末を統合。J-Link が必須。 | https://docs.nordicsemi.com/r/bundle/nrf-connect-vscode/page/guides/bd_work_with_boards.html/how-to-connect-using-rtt |
| NRF-07 | ロジアナ・解読 | nRF Sniffer（Wireshark 連携の BLE スニッファ） | 未確認（今回は資料本文を確認できなかった）。 | — |

---

## 8. Espressif: ESP-IDF（apptrace・コアダンプ・モニタ）

| ID | 分類 | 機能 | 具体的な内容 | 出典 URL |
|---|---|---|---|---|
| ESP-01 | トレース | Application Level Tracing（apptrace） | 任意データ・ログをホストへ送る（双方向）。JTAG（最高速、OpenOCD 必須、ホストから開始・停止・吸い出し）または UART（コンソールとは別の UART）。 | https://docs.espressif.com/projects/esp-idf/en/latest/esp32/api-guides/tracing/index.html |
| ESP-02 | RTOS | SystemView 形式の出力 | FreeRTOS のタスク・ISR を SEGGER SystemView 形式で記録。ヒープ追跡の例もある。デュアルコア対応。 | 同上 |
| ESP-03 | 性能・カバレッジ | gcov カバレッジの吸い出し | 実機で取った gcov データを JTAG でオンデマンドに取り出す。 | 同上 |
| ESP-04 | トレース | 関数の入退出の自動トレース | function_tracing の例。 | 同上 |
| ESP-05 | 自動化・連携 | トレース形式・転送路の拡張 | esp_trace コンポーネントで独自エンコーダ・独自転送路を IDF 改変なしで追加。 | 同上 |
| ESP-06 | 故障解析 | コアダンプ（フラッシュ / UART） | パニック時に ELF 形式で保存（SHA256 で検証）。フラッシュの専用パーティション、または UART へ Base64 で出力。 | https://docs.espressif.com/projects/esp-idf/en/latest/esp32/api-guides/core_dump.html |
| ESP-07 | 故障解析 | コアダンプの内容 | 落ちたタスクのレジスタ・スタック、他タスクの TCB とスタック（優先度順・上限数）、オプションで .bss/.data/ヒープ、属性指定した変数。外部 RAM は含まない。 | 同上 |
| ESP-08 | 故障解析 | coredump-info / coredump-debug | 情報表示、または GDB をコアダンプに対して起動して変数・タスクを見る。 | 同上 |
| ESP-09 | 故障解析 | IDF Monitor のアドレス自動解読 | シリアル出力中のコードアドレスを addr2line でファイル・行に変換。パニック時のバックトレースも解読。 | https://docs.espressif.com/projects/esp-idf/en/v5.2/esp32c3/api-guides/tools/idf-monitor.html |
| ESP-10 | 止めて調べる | GDBStub（シリアルだけで GDB） | パニック時にターゲット上の GDBStub が動き、Monitor が自動で GDB を起動。v5 以降は実行時モード（Ctrl+C で停止）もある。JTAG 不要だが機能は限定的。 | 同上 |

---

## 9. OpenOCD

| ID | 分類 | 機能 | 具体的な内容 | 出典 URL |
|---|---|---|---|---|
| OOCD-01 | 接続・書き込み | 多数のアダプタ・ターゲット | FTDI・CMSIS-DAP・ST-LINK・J-Link 等。ARM・RISC-V 等。設定は Tcl スクリプト（TAP・CPU・リセット構成）。 | https://openocd.org/doc/html/index.html |
| OOCD-02 | 接続・書き込み | フラッシュ書き込み | フラッシュドライバによる消去・書き込み・検証（CRC 比較）・ダンプ。load_image / verify_image / dump_image。 | https://openocd.org/doc/html/General-Commands.html |
| OOCD-03 | 接続・書き込み | GDB からのフラッシュ書き込み | メモリマップを GDB へ送り、vFlash で GDB の load から書く。 | https://openocd.org/doc/html/GDB-and-OpenOCD.html |
| OOCD-04 | ブレークポイント | bp / wp | ソフト・ハード・コンテキスト・ハイブリッドのブレーク、読み・書き・アクセスのウォッチポイント（値とマスク条件）。 | https://openocd.org/doc/html/General-Commands.html |
| OOCD-05 | 止めずに見る | 実行中のメモリアクセス | 対応ターゲットでは GDB で実行中にメモリを読む（設定に注意。IDE ではキャッシュの問題）。 | https://openocd.org/doc/html/GDB-and-OpenOCD.html |
| OOCD-06 | 止めずに見る | RTT | 制御ブロックを探索し、チャネルを TCP ポートとして公開。ポーリング間隔設定。SWO の無い M0 でも使える。単一ターゲットのみ。 | https://openocd.org/doc/html/General-Commands.html |
| OOCD-07 | トレース | TPIU / SWO の設定 | CoreSight TPIU・SWO のコマンド。ITM ポートの有効化。デコードは外部ツール（Orbuculum 等）。 | https://openocd.org/doc/html/index.html（16.6.3 節） |
| OOCD-08 | 性能・カバレッジ | profile（PC サンプリング） | 一定時間 PC をサンプリングし gmon.out 形式（最大 100 万サンプル）で保存。アドレス範囲指定可。 | https://openocd.org/doc/html/General-Commands.html |
| OOCD-09 | RTOS | RTOS 認識 | eCos・ThreadX・FreeRTOS・linux・ChibiOS・embKernel・mqx・uCOS-III・nuttx・RIOT・Zephyr・rtkernel、SMP コアをスレッドとして見せる hwthread。 | https://openocd.org/doc/html/GDB-and-OpenOCD.html |
| OOCD-10 | 止めて調べる | SMP・複数 GDB 接続 | 複数コアを hwthread で 1 つの GDB に。複数 GDB の同時接続も可能（同期の注意あり）。 | 同上 |
| OOCD-11 | ロジアナ・解読 | IPDBG（FPGA 内のロジアナ・波形発生器） | FPGA 内の JTAG ハブ回路と TCP でつなぐ JTAG ホスト。ロジアナ・任意波形発生器の IP を JTAG 経由で操作。 | https://openocd.org/doc/html/Boundary-Scan-Commands.html |
| OOCD-12 | その他 | バウンダリスキャン（SVF / XSVF） | SVF・XSVF を実行（ドライラン、エラー無視、進捗表示等）。 | 同上 |
| OOCD-13 | 接続・書き込み | PLD/FPGA の書き込み | FPGA のビットストリーム書き込みコマンド。 | https://openocd.org/doc/html/index.html（14 章） |
| OOCD-14 | 自動化・連携 | Tcl スクリプト・Telnet・Tcl ポート | すべての操作を Tcl で記述、TCP 経由で外部から制御。 | 同上（7・21 章） |
| OOCD-15 | 止めて調べる | セミホスティング | ARM セミホスティング（ホストのファイル I/O・コンソール）。※（今回確認した章には記載なし） | — |

---

## 10. pyOCD

| ID | 分類 | 機能 | 具体的な内容 | 出典 URL |
|---|---|---|---|---|
| PYOCD-01 | 接続・書き込み | CMSIS-Pack によるターゲット対応 | Open-CMSIS-Pack のフラッシュアルゴリズム・メモリマップでデバイスを追加。 | https://pyocd.io/docs/ |
| PYOCD-02 | 接続・書き込み | gdbserver / commander / flash / erase | GDB サーバ、対話コマンド、書き込み・消去のサブコマンド。※（各サブコマンドの詳細は未確認） | 同上 |
| PYOCD-03 | 自動化・連携 | Python API | セッション・ターゲット・メモリ・フラッシュを Python から直接操作。 | 同上 |
| PYOCD-04 | 自動化・連携 | ユーザスクリプト | 接続・リセット等の手順をプロジェクト単位の Python スクリプトで上書き（デリゲート）。 | 同上 |
| PYOCD-05 | トレース | SWO/SWV | SWV を受けて ITM を出力（Orbuculum から enable_swv で利用可）。 | 同上 / https://github.com/orbcode/orbuculum |
| PYOCD-06 | 止めて調べる | セミホスティング・マルチコア | セミホスティング、マルチコアのデバッグ。 | https://pyocd.io/docs/ |
| PYOCD-07 | 自動化・連携 | 遠隔プローブ | 遠隔のプローブを専用プロトコルでネットワーク越しに使う。 | 同上 |
| PYOCD-08 | 接続・書き込み | セキュリティ・保護の解除 | 保護されたデバイスの扱い（Security and protection の章）。 | 同上 |
| PYOCD-09 | RTOS | RTOS 認識 | 未確認（今回の調査で対応 RTOS の一覧を確認できなかった）。 | — |
| PYOCD-10 | 止めずに見る | RTT | 未確認。 | — |

---

## 11. probe-rs（defmt・RTT を含む）

| ID | 分類 | 機能 | 具体的な内容 | 出典 URL |
|---|---|---|---|---|
| PRS-01 | 接続・書き込み | 多種プローブ・多アーキテクチャ | DAPLink（CMSIS-DAP）・ST-Link・J-Link・FTDI・ESP32 USB JTAG・WLink・Black Magic、Linux GPIO/spidev の SWD。ARM・RISC-V・Xtensa。 | https://github.com/probe-rs/probe-rs |
| PRS-02 | 接続・書き込み | CMSIS-Pack からのフラッシュアルゴリズム | Pack のアルゴリズムを取り込んで書き込み。 | https://probe.rs/docs/ |
| PRS-03 | 自動化・連携 | probe-rs run（Cargo のランナー） | cargo run で書き込み→実行→RTT/defmt を表示→パニック時にスタックトレース。 | 同上 / GitHub |
| PRS-04 | 止めずに見る | RTT・defmt | RTT のチャネル表示、cargo-embed の対話 RTT 端末。 | 同上 |
| PRS-05 | 自動化・連携 | ライブラリ（Rust・C） | プローブ操作を自作ツールに組み込む。 | https://probe.rs/docs/ |
| PRS-06 | UI・UX | VS Code デバッガ（DAP） | Debug Adapter Protocol を実装。SVD による周辺レジスタ、コアごとの設定（coreConfigs）、RTT、REPL での式評価。公式には Alpha 段階。 | https://probe.rs/docs/tools/debugger/ |
| PRS-07 | 止めて調べる | GDB サーバ | GDB からも接続。 | GitHub |
| PRS-08 | 故障解析 | コアダンプの作成 | ターゲットの状態をコアダンプとして保存し、あとで調べる。 | https://probe.rs/docs/ |
| PRS-09 | 性能・カバレッジ | 性能プロファイリング | ナレッジベースに記載あり。方式（PC サンプリング等）は未確認。 | 同上 |
| PRS-10 | トレース | SWO/ITM | 未確認（README は "trace functionality" とだけ記載）。 | — |
| PRS-11 | トレース | defmt（遅延フォーマット） | 書式文字列を ELF 内の表に置き、ターゲットは番号と引数だけ送る。ログレベル、モジュール単位のコンパイル時フィルタ、時刻、パニック報告（panic-probe）、転送は RTT・ITM・セミホスティング。ELF と専用リンカスクリプトが必要。 | https://defmt.ferrous-systems.com/ |
| PRS-12 | 性能・カバレッジ | defmt-test / embedded-test | 実機でのユニットテスト。 | 同上 |

---

## 12. Black Magic Probe

| ID | 分類 | 機能 | 具体的な内容 | 出典 URL |
|---|---|---|---|---|
| BMP-01 | 接続・書き込み | プローブ内蔵の GDB サーバ | OpenOCD 等の中間ソフト不要。USB CDC-ACM で GDB が直接つなぐ。 | https://black-magic.org/index.html |
| BMP-02 | 接続・書き込み | ターゲットの自動検出 | 設定ファイル不要。monitor swdp_scan → attach。 | 同上 |
| BMP-03 | 止めて調べる | ウォッチポイント・フラッシュブレーク・メモリ/レジスタ | GDB 経由の標準的なデバッグ機能、フラッシュ書き込み。 | 同上 |
| BMP-04 | トレース | SWO | SWO の受信。 | 同上 |
| BMP-05 | 止めて調べる | セミホスティング | ホスト I/O。 | 同上 |
| BMP-06 | 接続・書き込み | Hosted 版（BMDA） | PC 上で動く版。他社プローブ経由でも使える（対応プローブの一覧は未確認）。 | 同上 |
| BMP-07 | 接続・書き込み | ARM Cortex と RISC-V | 対応アーキテクチャ。 | https://github.com/blackmagic-debug/blackmagic |
| BMP-08 | 止めずに見る | RTT・ターゲット給電・UART | 未確認（今回確認したページに記載なし）。 | — |

---

## 13. VS Code 拡張: Cortex-Debug / probe-rs 拡張 / Embedded Tools（Microsoft）

| ID | 分類 | 機能 | 具体的な内容 | 出典 URL |
|---|---|---|---|---|
| VSC-01 | 接続・書き込み | 複数の GDB サーバ（Cortex-Debug） | J-Link・OpenOCD・ST-LINK GDB server・pyOCD・Black Magic・st-util（部分対応）。 | https://github.com/Marus/cortex-debug |
| VSC-02 | トレース | SWO の解読とグラフ（Cortex-Debug） | ITM をコンソール文字・バイナリ（符号付/無 32 bit、Q16.16、float）で解読、実時間グラフ。独自デコーダを JavaScript で追加。 | 同上 |
| VSC-03 | 止めずに見る | RTT（Cortex-Debug） | OpenOCD・J-Link で RTT。文字・バイナリ・グラフは SWO と同じ。 | 同上 |
| VSC-04 | 止めずに見る | Live Watch（Cortex-Debug） | 実行中に変数を周期的に読む（OpenOCD・J-Link・ST-Link）。 | 同上 |
| VSC-05 | 止めて調べる | 周辺レジスタ（SVD）・メモリ表示 | 関連拡張（mcu-debug）で提供。 | 同上 |
| VSC-06 | 止めて調べる | 逆アセンブル・命令単位ステップ・マルチコア・マルチセッション | VS Code の逆アセンブル窓を使う。 | 同上 |
| VSC-07 | RTOS | RTOS スレッド表示（Cortex-Debug） | GDB サーバの RTOS 認識（J-Link・OpenOCD・pyOCD）による呼び出し履歴。 | 同上 |
| VSC-08 | UI・UX | probe-rs 拡張 | §11 PRS-06 を参照。 | https://probe.rs/docs/tools/debugger/ |
| VSC-09 | RTOS | RTOS ビューア（Embedded Tools） | Azure RTOS（ThreadX）・FreeRTOS・Zephyr のオブジェクト表示。種類を自動判別。 | https://devblogs.microsoft.com/cppblog/serial-and-zephyr-support/ |
| VSC-10 | 止めて調べる | レジスタビューア（Embedded Tools） | CMSIS-SVD による周辺レジスタ表示。 | https://devblogs.microsoft.com/cppblog/vscode-embedded-development/ |
| VSC-11 | UI・UX | シリアルモニタ（Embedded Tools） | ポート・ボーレート・行末設定、時刻付与、送信欄。単独の拡張としても提供。 | https://devblogs.microsoft.com/cppblog/serial-and-zephyr-support/ |

---

## 14. GDB（主要機能: 逆実行・トレースポイント）

| ID | 分類 | 機能 | 具体的な内容 | 出典 URL |
|---|---|---|---|---|
| GDB-01 | 止めて調べる | 逆実行（Reverse Execution） | reverse-continue / reverse-step / reverse-stepi / reverse-next / reverse-finish、set exec-direction reverse。 | https://sourceware.org/gdb/current/onlinedocs/gdb.html/Reverse-Execution.html |
| GDB-02 | 止めて調べる | 逆実行の条件 | ターゲットが状態を戻せること。組込みでは record full（1 命令ずつ記録。非常に遅い）か、逆実行に対応した遠隔ターゲット（主に全システムエミュレータ）。I/O は戻せない。record btrace は PC 向けの分岐トレース（Intel 等）。 | 同上 |
| GDB-03 | トレース | トレースポイント | trace で位置を指定、collect で取る式・メモリを指定、止めずに記録し tfind で後から当時の値を見る。トレースファイルの保存・読み込み。 | https://sourceware.org/gdb/current/onlinedocs/gdb.html/Tracepoints.html |
| GDB-04 | トレース | トレースポイントの条件 | 遠隔ターゲットのみ。スタブがトレース収集を実装している必要があり、GDB 同梱のスタブは非対応。組込みプローブでの実装例は少ない（未確認）。 | 同上 |
| GDB-05 | 自動化・連携 | Python 拡張・MI | GDB を Python で拡張、MI で IDE から操作。※ | https://sourceware.org/gdb/current/onlinedocs/gdb.html/ |

---

## 15. Orbuculum（SWO・ETM の解析）

| ID | 分類 | 機能 | 具体的な内容 | 出典 URL |
|---|---|---|---|---|
| ORB-01 | トレース | orbuculum（多重化サーバ） | プローブから受けたトレースを TCP で複数クライアントへ配信（orbflow: 3402、旧形式: 3443）。 | https://github.com/orbcode/orbuculum |
| ORB-02 | トレース | 多様なトレース源 | BMP・J-Link・汎用 USB シリアル・FTDI 高速シリアル・OpenOCD・pyOCD・ECPIX-5（ECP5 FPGA でパラレルトレース）・ファイル・TCP・ORBTrace Mini。 | 同上 |
| ORB-03 | トレース | SWO（NRZ/Manchester）とパラレルトレース（1〜4 bit） | SWO Manchester 約 4.3 MB/s、4 bit パラレル約 12.5 MB/s。 | 同上 |
| ORB-04 | トレース | orbcat | ITM チャネルを標準出力へ（書式・時刻付き）。 | 同上 |
| ORB-05 | 性能・カバレッジ | orbtop | PC サンプリングで関数・行ごとの時間比率。JSON・履歴・dot・gnuplot 出力。 | 同上 |
| ORB-06 | 性能・カバレッジ | orbstat | 統計と KCacheGrind 形式の出力（呼び出しグラフ付きプロファイル）。 | 同上 |
| ORB-07 | 故障解析 | orbmortem（事後解析） | ETM（ETM3.5 既定、ETM4 は着手段階）または MTB の命令トレースを解読し、直前に実行した命令列を表示。パラレルトレース源が必要。 | 同上 |
| ORB-08 | 自動化・連携 | orbzmq | ITM とハードイベントを ZeroMQ の PUBLISH で配信。 | 同上 |
| ORB-09 | UI・UX | orblcd | 2 つの ITM チャネルで PC 上に LCD を模擬表示。 | 同上 |
| ORB-10 | トレース | orbdump / orbtrace | 生 SWO をファイルへ。ORBTrace（FPGA 製トレースプローブ）の構成。 | 同上 |

---

## 16. Trice

| ID | 分類 | 機能 | 具体的な内容 | 出典 URL |
|---|---|---|---|---|
| TRC-01 | トレース | ID ベースのバイナリログ | printf 風の呼び出しのまま、ターゲットは ID と値だけを送り、文字列はホスト側。1 呼び出し約 6〜100 サイクルで割り込み内でも使える。 | https://github.com/rokath/Trice |
| TRC-02 | トレース | 累積 ID 表（til.json） | 古い ID と書式を保持し、古いファームのログも読める（長期運用向け）。 | 同上 |
| TRC-03 | トレース | 多様な転送 | SEGGER RTT・UART・USB・独自転送。直接・リングバッファ・ダブルバッファ。 | 同上 |
| TRC-04 | その他 | 時刻・暗号化 | ホストツールで時刻付与、暗号化。 | 同上 |

---

## 17. Perfetto（時間軸の表示）

| ID | 分類 | 機能 | 具体的な内容 | 出典 URL |
|---|---|---|---|---|
| PFT-01 | 時間軸の解析 | ブラウザ上のタイムライン UI | トレースプロセッサを WebAssembly でブラウザ内実行（データは手元に留まる）。 | https://perfetto.dev/docs/getting-started/other-formats |
| PFT-02 | 時間軸の解析 | 多形式の取り込み | Perfetto protobuf、Chrome JSON（B/E/X → スライス、I → 点、C → カウンタ、s/t/f → フロー矢印）、perf.data、ftrace、Firefox profiler 形式等。ZIP/TAR 内の複数ファイルを 1 つの時間軸に統合。 | 同上 |
| PFT-03 | 自動化・連携 | SQL による解析 | slice・track・counter 等の表を SQL で問い合わせ（UI・シェル・Python）。 | https://pigweed.googlesource.com/third_party/android/platform/external/perfetto/+/7690274ebbe251cd4d892d1ea956de21993bdd7a/docs/trace-processor.md |
| PFT-04 | 自動化・連携 | 組込みからの変換例 | Zephyr の CTF を ctf2perfetto.py で Perfetto へ変換。 | https://docs.zephyrproject.org/latest/services/tracing/index.html |

---

## 18. RTOS・イベントの解析ツール

### 18.1 Micrium µC/Probe

| ID | 分類 | 機能 | 具体的な内容 | 出典 URL |
|---|---|---|---|---|
| UCP-01 | 止めずに見る | 実行中の変数の表示・変更 | ELF のシンボルから変数を選び、止めずに読み書き。 | https://micrium.atlassian.net/wiki/spaces/osiiidoc/pages/131375/uC-Probe |
| UCP-02 | UI・UX | 部品を並べた操作パネル | ゲージ・チャート・数値・ボタン等をドラッグして画面を作る。Excel 連携、データ記録、スクリプト。 | https://www.embedded.com/?p=4429890 |
| UCP-03 | 時間軸の解析 | オシロスコープ機能 | 最大 8 本の変数・I/O（ビット単位）を、制御ループ等の内部時刻に同期してターゲット側でサンプリング。任意チャネルでトリガ、プリ／ポストトリガ、自動スケール。 | https://www.eenewseurope.com/en/micriums-µc-probe-to-be-bundled-with-infineons-xmc-mcus/ |
| UCP-04 | RTOS | カーネル・TCP/IP 認識 | µC/OS-II・µC/OS-III の内部状態、µC/TCP-IP の状態表示。 | https://micrium.atlassian.net/wiki/spaces/osiiidoc/pages/131375/uC-Probe |
| UCP-05 | 接続・書き込み | 多様な通信路 | J-Link（JTAG/SWD）、RS-232、USB、TCP/IP、IDE プラグイン経由。デバッグポート以外ではターゲット常駐コードが必要。 | 同上 / https://www.silabs.com/documents/public/presentations/ew-2018-debugging-live-cortex-m-based-embedded-systems.pdf |

### 18.2 Azure RTOS（Eclipse ThreadX）TraceX

| ID | 分類 | 機能 | 具体的な内容 | 出典 URL |
|---|---|---|---|---|
| TRX-01 | RTOS | イベント記録 | コンテキスト切替・プリエンプション・中断・割り込み等を時刻と実行中スレッドつきで、ターゲット RAM の循環バッファへ（直近 N 件を保持）。アプリから記録の停止・再開。 | https://threadx.io/releases/6.5.0/tracex/main/overview-tracex.html |
| TRX-02 | 時間軸の解析 | イベントの時系列表示 | イベント間の差分ティックを GUI で測る。事後またはブレーク時にホストで表示。 | 同上 |
| TRX-03 | 性能・カバレッジ | Execution Profile | スレッド・ISR・アイドルの時間比率。 | https://threadx.io/releases/6.5.0/tracex/main/chapter4.html |
| TRX-04 | 性能・カバレッジ | Popular Services / Thread Stack Usage | よく呼ばれる RTOS サービス、スレッドごとの実スタック使用量。 | 同上 |
| TRX-05 | 故障解析 | Performance Statistics（優先度逆転の検出） | コンテキスト切替・割り込み・優先度逆転等の回数。 | 同上 |
| TRX-06 | RTOS | マルチコア版（TraceX/MC） | マルチコアの可視化。 | https://www.electronicspecifier.com/?p=105943 |

### 18.3 Zephyr のトレース

| ID | 分類 | 機能 | 具体的な内容 | 出典 URL |
|---|---|---|---|---|
| ZEP-01 | RTOS | 複数のトレース形式 | CTF、SEGGER SystemView（RTT のみ）、Percepio Tracealyzer、ユーザ定義（弱シンボルのフック）、外部モジュールの独自形式。 | https://docs.zephyrproject.org/latest/services/tracing/index.html |
| ZEP-02 | トレース | 転送路 | UART・USB・ファイル（native）・RTT・RAM（デバッガで読む）。Tracealyzer 用に ITM のストリームポート。 | 同上 |
| ZEP-03 | トレース | 名前付きイベント | sys_trace_named_event（名前＋4 バイト引数 2 個）。 | 同上 |
| ZEP-04 | RTOS | オブジェクト追跡 | カーネルオブジェクトを種類ごとに列挙。 | 同上 |
| ZEP-05 | 時間軸の解析 | 表示ツール | Trace Compass（スレッドのスケジューリング・割り込み）、babeltrace（文字表示）、Perfetto（変換スクリプト）、Percepio View（無料、スナップショットのみ）。 | 同上 |

### 18.4 FreeRTOS+Trace（Percepio Tracealyzer）

| ID | 分類 | 機能 | 具体的な内容 | 出典 URL |
|---|---|---|---|---|
| FRT-01 | 時間軸の解析 | 30 以上の連動ビュー | Trace View（スレッド・割り込みのタイムライン）、Event Log、User Event のデータプロット等。 | https://percepio.com/tracealyzer/features-capabilities/ |
| FRT-02 | 性能・カバレッジ | CPU Load Graph | タスク・割り込みごと及び全体の CPU 使用率。 | 同上 |
| FRT-03 | RTOS | Communication Flow | スレッド間の IPC・同期の関係図。オブジェクト（mutex・セマフォ・キュー）を使うスレッドの一覧。 | 同上 |
| FRT-04 | 時間軸の解析 | ユーザイベントからの状態遷移図 | ユーザイベントの値から状態機械図を生成。 | https://FreeRTOS.org/trace |
| FRT-05 | トレース | Streaming / Snapshot | Streaming: ネットワーク・USB・SD 等で連続転送。Snapshot: ターゲット RAM のリングバッファを必要時に吸い出し（特別なハード不要）。 | 同上 |
| FRT-06 | 接続・書き込み | デバッガと同じプローブで併用 | 通常のデバッガと同時に使える。 | 同上 |

---

## 19. ロジアナのソフト

### 19.1 Saleae Logic 2

| ID | 分類 | 機能 | 具体的な内容 | 出典 URL |
|---|---|---|---|---|
| SAL-01 | ロジアナ・解読 | 標準の解析器（Analyzer） | I2C・SPI・UART 等の解読。※（一覧は今回未確認） | https://support.saleae.com/logic-software/extensions-apis-and-sdks |
| SAL-02 | ロジアナ・解読 | High Level Analyzer（HLA） | 標準解析器の出力フレームを Python 3.8 で再解釈し、上位プロトコルのフレームを作る拡張。 | https://docs.saleae.com/extensions/guides/high-level-analyzer-extensions |
| SAL-03 | ロジアナ・解読 | Measurement 拡張（アナログ・デジタル） | 選択範囲から周波数・デューティ比等の指標を計算する拡張。 | https://docs.saleae.com/extensions/guides/extension-file-format |
| SAL-04 | ロジアナ・解読 | グリッチフィルタ | チャネルごとに記録時に適用（記録後は変更不可）。トリガと組み合わせて「一定時間変化しない」検出の代用。 | https://discuss.saleae.com/t/feature-request-change-glitch-filter-in-read-only-session/591 |
| SAL-05 | 自動化・連携 | Automation API（gRPC / Python） | 手動・時間指定・デジタルトリガの各取得モード、デバイス設定、記録・解析・出力・保存。ヘッドレス実行（Linux）。 | https://saleae.github.io/logic2-automation/ |
| SAL-06 | 自動化・連携 | 公式 MCP サーバ（実験的） | Logic 2 の設定で MCP サーバを有効化（既定 127.0.0.1:10530）。AI エージェントが取得・解析を直接操作。Logic 8 / Pro 8 / Pro 16。 | https://docs.saleae.com/mcp/guides/getting-started |
| SAL-07 | 自動化・連携 | Analyzer SDK（C++） | 低レベルの独自解析器を C++ で作る。 | https://support.saleae.com/logic-software/extensions-apis-and-sdks |

### 19.2 sigrok / PulseView

| ID | 分類 | 機能 | 具体的な内容 | 出典 URL |
|---|---|---|---|---|
| SIG-01 | ロジアナ・解読 | 積み重ね可能なプロトコルデコーダ | Python 3 で書かれたデコーダを重ねて上位層を解読（例: SPI → SD カード）。 | https://sigrok.org/wiki/Main_Page |
| SIG-02 | 接続・書き込み | 多種の測定器 | ロジアナ・オシロ・テスター・データロガー・LCR・電源・ファンクションジェネレータ等。 | 同上 |
| SIG-03 | 自動化・連携 | sigrok-cli・ライブラリ | libsigrok（機器）・libsigrokdecode（デコード）を他ソフトから使える。CLI で自動化。 | 同上 |
| SIG-04 | 自動化・連携 | 多くのファイル形式 | VCD・CSV・WAV・バイナリ・hex 等の入出力。 | 同上 |
| SIG-05 | UI・UX | PulseView | ロジアナ・オシロ・混合信号の GUI。 | 同上 |

### 19.3 DSView（DreamSourceLab）

| ID | 分類 | 機能 | 具体的な内容 | 出典 URL |
|---|---|---|---|---|
| DSV-01 | ロジアナ・解読 | Stream / Buffer モード | Stream: PC へ実時間転送（長時間）。Buffer: 本体メモリへ（高サンプルレート。例 U3Pro16 で 2 Gbit DDR3）。 | https://gotronik.pl/images/DSView_User_Guide.pdf |
| DSV-02 | ロジアナ・解読 | 約 100 種のデコーダ | sigrok 由来。独自デコーダを追加できる。 | 同上 / https://github.com/tim20190215/Protocol_Decoder |
| DSV-03 | ロジアナ・解読 | 単純トリガ・高度なトリガ | ユーザガイドに章あり。詳細（段数・条件）は未確認。 | https://gotronik.pl/images/DSView_User_Guide.pdf |

### 19.4 Digilent WaveForms

| ID | 分類 | 機能 | 具体的な内容 | 出典 URL |
|---|---|---|---|---|
| WVF-01 | ロジアナ・解読 | ロジアナ | 個別信号・パラレルバス・SPI/I2C/CAN/UART の解読表示。 | https://digilent.com/shop/software/waveforms/ |
| WVF-02 | ロジアナ・解読 | Protocol 機器（生成と解読） | UART・SPI・I2C・CAN・AVR 等の送受信（機種による）。 | https://www.meilhaus.de/en/Infos/digilent?b2b=0 |
| WVF-03 | その他 | オシロ・波形発生・パターン発生・スペアナ等 | 1 台で複数の計測器。パターン発生でターゲットへ刺激。 | https://digilent.com/shop/software/waveforms/ |
| WVF-04 | 自動化・連携 | Script エディタ（JavaScript）と SDK | アプリ内で計測器を JavaScript で制御。SDK で Python・C から操作。 | 同上 |
| WVF-05 | 時間軸の解析 | 計測器間のクロストリガ | 未確認。 | — |

---

## 20. 日本のツール

### 20.1 ハートランド・データ DT10 / DT+（テストポイント方式）

| ID | 分類 | 機能 | 具体的な内容 | 出典 URL |
|---|---|---|---|---|
| DT-01 | トレース | テストポイント方式 | ソースにテストポイント（計測用の短いコード）を挿入し、実行の通過を外部機器へ出す。CPU・OS 非依存。2 点間の実行時間、関数単位の実行時間を取る。 | https://monoist.itmedia.co.jp/mn/spv/2410/01/news001.html / https://mdspacific.com/product_category/dt10-dynamic-testing-tool-for-real-time-trace-2/ |
| DT-02 | 接続・書き込み | 出力経路の選択（DT10） | 非同期バス・GPIO・Ethernet・SD I/F・UART。 | https://mdspacific.com/product_category/dt10-dynamic-testing-tool-for-real-time-trace-2/ |
| DT-03 | 接続・書き込み | 専用記録機 DBOX+Trace | マイコンの GPIO（記事では 6 ポート）からデジタル信号を取り込んで記録。 | https://monoist.itmedia.co.jp/mn/spv/2410/01/news002.html |
| DT-04 | 時間軸の解析 | 長時間記録 | 最大 30 日間連続。 | 同上 / mdspacific |
| DT-05 | 電力 / ロジアナ・解読 | DBOX+Analog（アナログ同時記録） | 10 MSPS・12 bit のアナログ入力を、ソフトの動作と同じ時間軸で記録・比較。 | https://monoist.itmedia.co.jp/mn/spv/2410/01/news002.html |
| DT-06 | RTOS | タスク切替・割り込み・プロセス間の受け渡し | タスク・プロセス・スレッドの可視化、アプリ層とカーネル層の同時トレース（組込み Linux 含む）。 | 同上 / https://www.hldc.co.jp/product/dt/ |
| DT-07 | 止めずに見る | 変数の監視 | 実行経路と変数値をブレークせずに可視化。 | https://www.hldc.co.jp/product/dt/ |
| DT-08 | 性能・カバレッジ | 実行ログに基づくカバレッジ | 実機の実行から分岐の通過実績・未実行の異常系を示す。 | https://www.hldc.co.jp/product_service/development/dtplus/ |
| DT-09 | 性能・カバレッジ | 処理時間・周期・応答時間のばらつき | 関数ごとの処理時間、タスク周期、応答時間のばらつき検知。一度の試験で複数関数を同時に取る。 | 同上 |
| DT-10 | 自動化・連携 | 期待値との比較・証跡の自動生成 | 期待値と実測の比較、設計書・試験仕様と対応づけた証跡の生成。 | 同上 / mdspacific |
| DT-11 | 時間軸の解析 | 機器間通信との同時確認 | 実行の可視化と同時にデバイス間通信を確認。 | https://www.hldc.co.jp/product/dt/ |
| DT-12 | 自動化・連携 | AUTOmeal（テスト自動化）との連携 | Raspberry Pi で入出力ボード・シリアル/USB を操作するテスト基盤（シナリオは Python）。DT+Trace で試験中の動作を確認。 | https://monoist.itmedia.co.jp/mn/spv/2410/01/news002.html |
| DT-13 | 故障解析 | 出荷後の不具合ログ収集 | リリース後のログ収集環境の自動構築。カーネルビルドなしでログ出力を切替。 | https://www.hldc.co.jp/product/dt/ |
| DT-14 | その他 | オーバーヘッド低減（AI） | 「DT+2.0」「AI でオーバーヘッドを最小化」と記載のみ。内容は未確認。 | 同上 |

### 20.2 京都マイクロコンピュータ PARTNER

| ID | 分類 | 機能 | 具体的な内容 | 出典 URL |
|---|---|---|---|---|
| KMC-01 | トレース | 大容量のリアルタイムトレース | PARTNER-Jet2 Model20 で 4 GB のトレースメモリ。PARTNER-Jet3 はトレース付き Model15 と無し Model10。 | https://monoist.itmedia.co.jp/mn/articles/1404/21/news096.html / https://monoist.itmedia.co.jp/mn/articles/2506/04/news053.html |
| KMC-02 | 止めて調べる | 多コア対応 | Jet2 で 16 CPU 以上の構成。 | https://monoist.itmedia.co.jp/mn/articles/1404/21/news096.html |
| KMC-03 | RTOS | Linux 対応 | 業界初の Linux 対応 ICE（初代 PARTNER-Jet、2003 年）。プロセス・カーネルのデバッグ。 | https://www.kmckk.co.jp/download/w_linux/whitepaper_linux.pdf |
| KMC-04 | 接続・書き込み | 多アーキテクチャ | Arm・RISC-V・SuperH 等。 | https://monoist.itmedia.co.jp/mn/spv/2506/04/news053.html |
| KMC-05 | その他 | ファームウェア更新で機能追加 | Jet3 はハード交換なしでデバッグ機能を追加できる構造。 | 同上 |
| KMC-06 | UI・UX | VS Code プラグイン | Jet3 で VS Code 向けプラグインを提供。対応機能は未確認。 | 同上 |

### 20.3 コンピューテックス PALMiCE4

| ID | 分類 | 機能 | 具体的な内容 | 出典 URL |
|---|---|---|---|---|
| CTX-01 | 止めずに見る | リアルタイムモニタ | 変数・周辺をハードウェアで最小 10 µs 周期でサンプリングしグラフ化。実行中に値を変更。条件で記録開始・停止、CSV 保存。 | https://www.computex.co.jp/products/palmice4/index.htm |
| CTX-02 | トレース | 内蔵トレース（ETB/MTB）と外部トレース（ETM/PTM 4 GB） | 外部は Model-T のみ（Mictor 38 ピン）。関数呼び出し構造での表示、実行中のトレース停止。 | 同上 |
| CTX-03 | 止めて調べる | Undo トレース | レジスタ・メモリの値を以前の状態へ戻す（逆実行に相当）。 | 同上 |
| CTX-04 | 性能・カバレッジ | Core Benchmark | コア別の関数実行履歴、CPU 使用率の時間推移、関数ごとの時間比率。関数を名前でグループ化（"Bluetooth" 等）して集計。コード改変不要・OS 非依存。全モデル。 | 同上 |
| CTX-05 | RTOS | OS 対応 | µITRON・TOPPERS・FreeRTOS・Zephyr・Linux の状態表示・タスクトレース。FreeRTOS はタスク遷移・API 呼び出し履歴・エラー表示。 | 同上 / https://newscast.jp/news/0886279 |
| CTX-06 | 止めて調べる | 異種マルチコア | オプションなしでマルチコア。コア別の実行・停止。 | 同上 |
| CTX-07 | 故障解析 | バグ検出 | 配列・ヒープのオーバーフロー検出（CSIDE V7）。例外のベクタキャッチ。 | 同上 |
| CTX-08 | ロジアナ・解読 | ロジアナモード | 4 ch・最大 200 MHz（Model-T）。デバッガと同じ機器でロジアナ。 | 同上 |
| CTX-09 | 接続・書き込み | ホットプラグ・フラッシュ書き込み | 稼働中のシステムに後から接続。内蔵フラッシュ・シリアル（SPI）フラッシュ（RZ/A1）書き込み。 | 同上 |

### 20.4 ソフィアシステムズ

| ID | 分類 | 機能 | 具体的な内容 | 出典 URL |
|---|---|---|---|---|
| SOP-01 | トレース | EJ-Extreme Long Time Trace | フル ICE。内蔵 HDD で 100 GB 超の長時間・分岐トレース（2007 年前後の情報。現行の販売状況は未確認）。 | https://monoist.itmedia.co.jp/mn/articles/0701/24/news115.html |
| SOP-02 | 接続・書き込み | JTAG-ICE（EJ-Debug）・HyperSTAC | 製品系列の存在のみ確認。機能は未確認。 | 同上 |

### 20.5 横河ディジタルコンピュータ（advice / microVIEW-PLUS）

| ID | 分類 | 機能 | 具体的な内容 | 出典 URL |
|---|---|---|---|---|
| YDC-01 | 時間軸の解析 | adviceLUNA システムマクロトレース | 関数・スレッドの実行履歴とタスク遷移をチャート表示。SMP 対応。 | https://monoist.itmedia.co.jp/mn/articles/1204/23/news049.html |
| YDC-02 | 時間軸の解析 | TRQerAM | システムの動きと通信データの流れを同時に可視化・検証する動的テスト／解析ツール（2015 年発表）。 | https://monoist.itmedia.co.jp/mn/spv/1511/11/news115.html |
| YDC-03 | その他 | microVIEW-PLUS | 未確認（今回の検索で公式情報を見つけられなかった）。 | — |

---

## 21. 横断のまとめ

### 21.1 複数のツールに共通する機能（＝ fprobe が当然持つべき基本機能）

| 共通機能 | 持っているツール（ID） | fprobe への含意 |
|---|---|---|
| SWD/JTAG でのフラッシュ書き込み・検証、CMSIS-Pack のアルゴリズム利用 | CUBE-19, MCUX-01, OOCD-02, PYOCD-01, PRS-02, BMP-01 | 標準ツール（OpenOCD・pyOCD・probe-rs）経由で使えること自体が基本要件 |
| GDB サーバ／DAP によるデバッグ、VS Code 連携 | OOCD, PYOCD-02, PRS-06/07, BMP-01, VSC-01, KMC-06 | CMSIS-DAP v2 準拠で各ツールから使える |
| 実行中の変数・メモリの読み出し（Live Watch） | CUBE-03/11, MCUX-12, CCS-07, VSC-04, UCP-01, OOCD-05, CTX-01, DT-07 | ほぼ全員が持つ。差は「周期」と「取りこぼし」 |
| 変数の時系列グラフ | CUBE-04/14, MPLB-03, VSC-02, UCP-03, CTX-01 | 同上 |
| SWO/ITM（printf・PC サンプリング・例外・データトレース） | CUBE-04, MCUX-02〜05, OOCD-07, PYOCD-05, BMP-04, VSC-02, ORB | 高速 SWO（Manchester/NRZ）受信と解読は必須 |
| RTT | OOCD-06, PRS-04, VSC-03, NRF-06, TRC-03 | 必須。SWO の無い M0 でも使える |
| PC サンプリングによるプロファイル | CUBE-04, MCUX-02, OOCD-08, ORB-05/06, PRS-09 | 必須 |
| RTOS 認識（スレッド一覧・スタック・オブジェクト） | CUBE-08, MCUX-13, E2S-05, CCS-06, OOCD-09, VSC-07/09, NRF-05, CTX-05 | GDB サーバ側／ホスト側の両方で |
| RTOS イベントトレース（タイムライン・CPU 負荷） | ESP-02, TRX, ZEP, FRT, DT-06, YDC-01 | SystemView/CTF/Tracealyzer 形式との互換が鍵 |
| 電流測定と波形表示 | CUBE-17, MCUX-07, SLS-01, CCS-03, MPLB-04, NRF-01 | 半導体メーカーの評価環境はほぼ全社が持つ |
| 故障の自動解析（HardFault 解読・コアダンプ） | CUBE-05, ESP-06〜09, PRS-08 | 必須 |
| プロトコルデコーダ（I2C/SPI/UART 等） | SAL-01, SIG-01, DSV-02, WVF-01 | sigrok 互換のデコーダ資産を使えると有利 |
| スクリプト・API による自動化 | CUBE-22, CCS-09, OOCD-14, PYOCD-03, PRS-05, SAL-05, SIG-03, WVF-04 | Python API + MCP は fprobe の本命 |

### 21.2 特定ツールにしかない（または少数しか持たない）差別化機能

| 差別化機能 | ツール（ID） | 何が強いか |
|---|---|---|
| 電流と PC（コード）の対応づけ | SLS-02, MCUX-08（Power Profile）, CCS-04（電力と CPU 状態） | 「どの関数が電力を食うか」を直接示す |
| 電流と無線パケットの対応づけ・複数ノードの統合キャプチャ | SLS-03/05/06 | 無線チップ自身の PTI で、スニッファなしに完全なパケット記録 |
| 電流測定器に 8 ch のロジック入力を同居 | NRF-03（PPK2） | GPIO で区間を示して電流と同じ時間軸に重ねる。fprobe の「ロジアナ＋電源計測」の最小形 |
| デバッガ本体にロジアナモード | CTX-08（PALMiCE4: 4 ch / 200 MHz） | 国産 ICE で唯一確認できた「デバッガ＝ロジアナ」。fprobe の直接の先行例 |
| テストポイント方式＋専用記録機＋アナログ同時記録＋30 日記録 | DT-01〜05 | CPU・OS 非依存、ETM 不要で長時間。期待値比較・証跡生成まで（DT-10） |
| ハードウェアでの 10 µs 周期の変数サンプリング | CTX-01 | デバッグポートのポーリングより高速・等間隔 |
| ターゲット側同期のオシロ（制御ループに同期） | UCP-03, CUBE-12（Snapshot） | ポーリングでは取りこぼす速い変化を確実に取る |
| 実行中のトレース開始・停止（CPU を止めない） | MCUX-06 | J-Link 経由では一瞬止まる、と NXP が明記 |
| Undo トレース／逆実行 | CTX-03, GDB-01/02 | 過去の状態へ戻す。組込みでは実用例が少ない |
| 止めずに値を採る GDB トレースポイント | GDB-03/04 | 標準の仕組みなのに組込みプローブでの実装がほぼ無い（空き地） |
| ETM/MTB の事後解析（直前の命令列） | ORB-07, CCS-01, CTX-02, KMC-01 | 「なぜ落ちたか」の決定打。fprobe は FPGA でパラレルトレース受信が可能 |
| KCacheGrind 形式の呼び出しグラフ付きプロファイル | ORB-06 | 既存の可視化ツールに乗る |
| ZeroMQ・TCP での多重配信 | ORB-01/08, OOCD-06（RTT を TCP） | 複数ツールが同じストリームを同時に読む |
| 関数をグループ化した CPU 使用率（"Bluetooth" 等） | CTX-04 | 大規模コードでの「どの機能が重いか」 |
| Communication Flow・状態遷移図の自動生成 | FRT-03/04 | RTOS の相互作用を図で理解 |
| 優先度逆転の自動検出 | TRX-05 | RTOS 特有の不具合を直接指摘 |
| コアダンプ（ELF）＋ GDB で事後解析 | ESP-06〜08, PRS-08 | 現場で落ちた状態を持ち帰る |
| シリアルだけの GDBStub とアドレス自動解読 | ESP-09/10 | 追加ハードなしの最低限デバッグ |
| ID ベースのバイナリログ（defmt・Trice） | PRS-11, TRC-01/02 | 帯域・CPU 負荷を桁違いに下げる。fprobe のテストポイント方式の上位層に使える |
| FPGA 内のロジアナを JTAG で操作（IPDBG） | OOCD-11 | 「FPGA 自身のデバッグ」も同じプローブで |
| 公式 MCP サーバ | SAL-06（Saleae、実験的） | AI エージェントからのロジアナ操作。fprobe の MCP の直接の競合 |
| Node-RED のフローで取得・処理・表示を組む | CUBE-14 | プログラムを書かずに監視画面を作る |
| 部品を並べて操作パネルを作る | UCP-02 | 調整・デモ用の画面を素早く作る |
| SQL でトレースを問い合わせる | PFT-03 | 大量のイベントを集計・検索（AI との相性が良い） |
| 計測器の統合（オシロ・波形発生・パターン発生） | WVF-03 | 刺激を与えて応答を観測する試験 |

### 21.3 fprobe にとっての示唆（調査担当の所見）

- 「電力（または外部信号）× コード × 時間」の対応づけは各社が自社 MCU・自社キットに閉じて実装している（Silicon Labs・NXP・TI）。fprobe はロジアナ・SWO・ADC を共通タイムスタンプで持つので、**ベンダー非依存で同じことができる**のが最大の差別化になり得る。
- ロジアナとデバッガの一体化の先行例は、確認できた範囲では PALMiCE4 のロジアナモード（4 ch）と PPK2 の 8 ch ロジック入力、DT+ の DBOX 程度で、どれもクロストリガ・深いバッファ・プロトコル解読を併せ持たない。
- AI 連携では Saleae が公式 MCP サーバを出し始めている（実験的）。デバッガ側の MCP は今回の対象ツールには見当たらなかった（未確認を含む）。
- GDB トレースポイント・逆実行は標準の枠組みがあるのに、組込みプローブ側の実装がほぼ無い。ETM 受信や DWT を使えば fprobe が埋められる可能性がある。
