# 機能の拡充の全体計画（J-Link 周辺の全ツールの上位互換を目指す）

根拠: [J-Link 周辺アプリとの比較](../../reports/2026-10-17-jlink-comparison.md)、[UI/UX の調査](../../reports/2026-10-17-debug-ui-ux-survey.md)。
UI・UX とフレームワークの再検討は、この計画の後の別の段で行う（2026-10-17 のユーザーの方針）。

## 方針

- 機能はまず **ライブラリ（`host/fprobe`）+ API（`host/api/fprobe_api.json`、デーモン、MCP）** として、画面に依らない形で
  作る。AI はできた時点で使え、後の UI は API の上に作る。この段の画面は最小限（必要なら既存のタブに足す程度）。
- すべての機能に試験を付け、実機（Primer 20K + Nucleo F401RE 等）で確かめる。
- 並行して進める: ハードを使わない PC 側の部品は、独立した作業場所（git worktree）で別々の担当が同時に作る。
  API・MCP への組み込み、ゲートウェアのビルド、実機の試験は 1 か所（本流）で順に行う（ハードが 1 台のため）。
- 各サブプロジェクトは「設計 → 仕様書（`docs/superpowers/specs/`）→ 計画（`docs/superpowers/plans/`）→ 実装」。

## サブプロジェクト

| 番号 | 内容 | 上回る対象 | ハード | 並行 |
|---|---|---|---|---|
| F1 | 止めずに見る: RTT（複数チャンネル・双方向）、セミホスティング、SWO（ITM / DWT）の解読 | RTT Viewer・SWO Viewer | 手持ち | PC 側の解読・RTT の手順は並行。FPGA による高速化は本流 |
| F2 | デバッガの中身: DWARF の型（構造体・配列・ポインタ・列挙・ビットフィールド）、ローカル変数と引数、式の評価、CFI による正確な呼び出し履歴、行単位のステップ、逆アセンブル、メモリ、周辺レジスタ（SVD）、Flash 書き込み（速度）、Flash 上の無制限のブレークポイント | Ozone・IAR・Keil | 手持ち | DWARF・逆アセンブル・SVD は並行 |
| F3 | 共通の時間軸の記録: すべての出来事を時刻付きの 1 つの記録へ。保存・検索・統計・日単位の連続記録 | TRACE32・DT+ | 手持ち | 本流（F4〜F7 の土台） |
| F4 | 変数のライブ観測: FPGA が正確な時刻で周期的にメモリを読む | J-Scope・Keil Logic Analyzer・CubeMonitor | 手持ち | ゲートウェアは本流 |
| F5 | 性能解析: PC サンプリング（DWT → SWO）のプロファイル、2 点間の時間とばらつき、カバレッジ | Ozone・IAR・Streamline・DT+ | 手持ち | 解析の部品は並行 |
| F6 | RTOS: タスクの一覧・タスクごとの呼び出し履歴・タスクの時間軸 | SystemView・Tracealyzer | 手持ち（試験用ファームに RTOS） | 並行 |
| F7 | テストポイント（DT+ 方式）と通信の解読（UART・SPI・I2C・SWD） | DT+・外付けロジアナ | 手持ち | 解読は並行 |
| F8 | コアを広げる: Cortex-A7（Pi 2）、ARM11（Pi Zero）、RISC-V（Pico 2 の RP2350） | — | 配線が要る | 後 |
| F9 | 電源: 電流の計測、給電、1.2〜5 V | Energy Profiler・J-Link 上位機 | 追加のハードが要る | 後 |
| — | 命令トレース（ETM） | J-Trace・TRACE32 | ターゲット次第（要調査） | 後 |

## 並行の第 1 陣（ハードを使わない PC 側の部品）

| 担当 | 内容 | 主なファイル |
|---|---|---|
| A | DWARF: 型・ローカル変数と引数（位置式）・式の評価・CFI による巻き戻し | `host/fprobe/dwarf*.py` |
| B | 逆アセンブル（Thumb / Thumb-2）・SVD（周辺レジスタの定義の読み込みと値の解読） | `host/fprobe/disasm.py`, `host/fprobe/svd.py` |
| C | 通信の解読（UART・SPI・I2C・SWD）: `Transitions` から | `host/fprobe/decode/` |
| D | 止めずに見る: ITM / DWT のパケット解読、RTT（制御ブロックの探索・リングの読み書き）、セミホスティングの処理 | `host/fprobe/itm.py`, `host/fprobe/rtt.py`, `host/fprobe/semihosting.py` |

担当は **ライブラリと単体試験だけ** を作り、API（`fprobe_api.json`）・`mcp_server.py`・デーモンは触らない
（組み込みは本流でまとめて行い、ぶつからないようにする）。メモリやレジスタを読む部分は抽象の読み手（関数・
プロトコル）を受け取る形にし、偽物で試験する。実機・FPGA のビルド・USB/COM の機器には触れない。

本流（私）: F3 の設計、F1 の FPGA 側（RTT の高速化）と F4（周期的なメモリ読み出し）の設計、第 1 陣の組み込みと実機の試験。
