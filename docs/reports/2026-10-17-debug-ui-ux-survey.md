# 組込みデバッグ・トレース用アプリの UI/UX 調査（2026-10-17）

Web と公式資料（マニュアル PDF・公式 KB・製品ページ）による調査。確認できなかった点は「未確認」と書く。
機能の比較は [2026-10-17-jlink-comparison.md](2026-10-17-jlink-comparison.md)。

## 第 1 部 主要アプリ

### 1. SEGGER Ozone（J-Link 用のデバッガ）

- **画面:** IDE と同じドッキング式のマルチウィンドウ。切り離し・左右下へのドッキング・タブ化。窓はコマンド
  `Window.Show(...)` でも開ける。（[UM08025](https://www.segger.com/downloads/jlink/UM08025) §3.9）
- **流れ:** ウィザードでデバイスと ELF → ダウンロードして実行 → 停止したらソース・逆アセンブル・変数・レジスタ →
  トレースがあれば命令トレース・Code Profile・Timeline でさかのぼる。ELF の再ビルドを検知して再読み込みを提案。
- **Timeline:** Data（変数のグラフ）・Power（電流）・Code（関数の呼び出しを横棒で）の 3 段が共通の時間軸。割り込みは
  角丸・濃い色、RTOS プラグインでタスクごとに背景色。拡大すると命令の境界の目盛まで見える。（§4.22）
- **連携:** Sample Cursor（いまの実行位置）がソース・逆アセンブル・命令トレースと双方向に同期し、Timeline をクリック
  すると全窓がその時点に切り替わる。Hover Cursor は全段の値を表示、Time Reference は基準からの時間差を表示。
  Code 段の右クリックで「その関数にブレーク」「同じ関数の次/前の実行へ」。←→↑↓ Home End で移動。
- **時間軸:** ドラッグで全段が横に動く、Ctrl+ホイールで横・Shift+ホイールで縦に拡大、Fit Width / Height、Go To Time。
- **その他:** 値が変わった変数を何ステップ前に変わったかで色分け、SmartView（構造の表示）、命令トレースの正規表現検索。
- **自動化:** GUI の全操作に同じ働きのスクリプト関数。localhost:19200 の TCP ソケットでコマンドを送れる（1 接続）。
- **弱点:** Code 段は ETM と J-Trace PRO、Power 段は J-Link の給電が要る（J-Link では Data と Power だけ）。バッファ式の
  トレースは時刻が無く「1 命令 = 1 サイクル」で近似。J-Link に縛られる。（[SEGGER ブログ](https://blog.segger.com/correlating-and-visualizing-data-sampling-current-consumption-and-program-execution-via-ozones-timeline-window/)、[フォーラム](https://forum.segger.com/thread/6872-solved-trace-timeline-accuracy)）

### 2. SEGGER SystemView（RTOS のイベントトレース）

- **画面:** Timeline / Events list / Terminal / CPU Load / Contexts / Runtime / Heap / System / Data plot。全窓が同期。
  （[UM08027](https://www.segger.com/downloads/systemview/UM08027_SystemView.pdf)）
- **Timeline:** 1 行 = 1 コンテキスト（全体・割り込み・タスクを優先度順）。存在期間は薄い背景、Ready は灰色、切り替えは
  連結線。F/B キーでそのタスクの次/前の実行へ。
- **一覧との同期:** 選択位置の青線を画面の 10〜90% に固定してスクロールできる。任意のイベントを時間の基準に（R キー）。
  種類でフィルタ。マルチコアはコアごとの一覧が時刻で同期。
- **流れ:** ターゲットに記録コードを組み込む（RTT）→ Start Recording → 連続記録しながら表示 → 保存して解析。CLI あり。
- **弱点:** 記録用のコードが要る。イベントが多いと RTT が詰まる。商用は有償。

### 3. Percepio Tracealyzer（RTOS トレース）

- **画面:** 多数のビューをドッキングで配置。Trace View（縦・横）、CPU Load、Event Log、Communication Flow、Data Plot、
  Heap / Stack、Timing Metrics、State Machine 等。（[機能](https://percepio.com/tracealyzer/features-capabilities/)）
- **評価されている点:** 「views are linked for easy drill-down – double-click to see the details」。一覧・グラフ・統計の
  外れ値をダブルクリックするとタイムラインの該当箇所へ。SQL に似たクエリの Finder、補完付き検索。
- **時間軸:** Ctrl+ホイール・+/-・戻る/進むで拡大。グラフで区間をドラッグすると Trace View に表示。Shift で 2 点の間を
  測る。ブックマーク。大きな記録は Trace Preview で一部だけを開く。
- **弱点:** 有償で高価、Windows と Linux のみ、記録用ライブラリが要る。ソースへの移動は未確認。

### 4. SEGGER J-Scope（変数のライブプロット）

- **画面:** 1 枚の固定画面（ツールバー・グラフ・シンボル表（値・最小・最大・平均・倍率）・状態）。
- **モード:** HSS（プローブが周期的に読む。ターゲットの変更不要）、RTT（ターゲットが送る）、RTT Plus。
- **弱点:** リング式で古いデータは消える。コード・イベントと連動しない単機能。（[KB](https://kb.segger.com/J-Scope)）

### 5. Keil MDK µVision

- **Logic Analyzer:** 変数を登録して波形で表示。基準線とカーソルの差分、Shift で変化点に吸い付く。**「Code」ボタンで
  その値の変化を起こした行へ**（PC が記録されている場合）。SWO が要り Cortex-M0/M0+ は不可。（[マニュアル](https://www.keil.com/support/man/docs/uv4cl/uv4cl_db_dbg_logicanalyzer.htm)）
- **System Analyzer:** スレッド・割り込み・イベント・変数・電流を 1 本の時間軸に。操作に癖、電力は約 8.5 秒分のリング。
- **Event Recorder / Statistics / Component Viewer（CMSIS-View）:** 注釈 API でイベント、区間ごとの時間と電荷の集計、
  XML 定義で部品の内部状態を表示。（[CMSIS-View](https://arm-software.github.io/CMSIS-View/latest/index.html)）
- **弱点:** Windows 専用の古い UI。Keil Studio（VS Code）への移行中。

### 6. IAR C-SPY

- **Timeline:** Call Stack・Data Log・Interrupt Log・Power Log・State Log・Events を共通の時間軸に縦に並べる。右クリックで
  前後移動・拡大・単位（秒/サイクル）・**Go To Source**。
- **Power debugging:** Power Log の値は実行コードと対応付けられ、**ダブルクリックで該当コードへ**。関数ごとの消費電力、
  しきい値で停止。（[IAR](https://docs.iar.com/ewrx/5.2/en/c-spy-debugging/power-debugging/introduction-to-power-debugging.html)）
- **自動化:** `cspybat`、C-SPY マクロ。**弱点:** 機能がプローブ次第、高価、Windows 中心。

### 7. Silicon Labs Simplicity Studio Energy Profiler

- **画面:** Single-Node / Multi-Node / Scope。Scope では無線パケット等のイベントの行が波形の下に並ぶ。
- **流れ:** 書き込み → Start Energy Capture → デバイス選択 → OK の 4 手順。
- **コードとの対応:** SWO の PC サンプルで関数ごとのエネルギー表、関数をクリックでソースへ、**電流波形を実行中の関数で
  色分け**、範囲選択で平均電流と総エネルギー。（[Code Correlation](https://docs.silabs.com/simplicity-studio-5-users-guide/5.2.0/ss-5-users-guide-tools-energy-profiler/profiling-with-code-correlation)）
- 波形の 1 点のクリックでソースへ飛ぶ動作は未確認。**弱点:** Silicon Labs のキット専用。

### 8. Lauterbach TRACE32 PowerView

- **画面:** メニュー・ツールバー・**コマンドライン**・状態行の上に多数の窓。**GUI でできることはすべてコマンドでも
  できる**。窓の見出しを右クリックすると、その窓を開くコマンドが出る。（[チュートリアル](https://www2.lauterbach.com/pdf/debugger_tutorial.pdf)）
- **連携:** `/Track` で他の窓のカーソルに追従、`/ZoomTrack` で拡大率も揃う。Trace.Chart・統計・Duration Analysis・
  Trace.Find。（[トレース](https://www2.lauterbach.com/pdf/trace_tutorial.pdf)）
- **ロジアナとの一体化（本プロダクトに最も近い先行例）:** PowerIntegrator / PowerProbe がトレースと共通の時刻と
  クロストリガを持つ。（[PowerIntegrator](https://www.lauterbach.com/pi.html)）
- **自動化:** PRACTICE、操作の記録（LOG）、Remote API と Python ライブラリ。コミュニティ製の MCP もある。
- **弱点:** 非常に高価、見た目が古い、コマンドを覚えないと使いこなせない（推測）。

### 9. ハートランド・データ DT+（旧 DT10）

- **考え方:** デバッグの窓口を使わず、ソースに「テストポイント」を自動で入れ、外付けのハード（DBOX+Trace、GPIO /
  バス / Ethernet / UART、100 MSPS）で集める。最長 30 日分のログ。（[構成](https://hldc.co.jp/product_service/development/dtplus/configuration/)）
- **画面と機能:** 関数遷移スコープ、関数ごとの時間・周期のグラフ、通信ラインモニタ（SPI/UART/I2C）、ロジック波形
  （8 ch）、C0/C1 カバレッジ、テストポイントの場所を提案する Advisor、**アナログ波形・カメラ映像との同期解析**、CLI と
  Jenkins 連携。（[機能](https://hldc.co.jp/product_service/development/dtplus/features/)）
- **評価と不満（事例）:** テストポイントの自動挿入で手間が減る。画面が見づらい箇所、データが 30 分で約 2 GB と重い、
  「見える化・共有をもっと楽に」という要望。（[MONOist](https://monoist.itmedia.co.jp/mn/spv/2410/01/news001.html)）

## 第 2 部 その他

| アプリ | 画面の型 | 特徴 | 弱点 |
|---|---|---|---|
| [VS Code + Cortex-Debug](https://github.com/Marus/cortex-debug) | VS Code の標準デバッグ UI | 各種 GDB サーバ、SWO/RTT の表示とグラフ、Live Watch、SVD、RTOS のスレッド | launch.json を手で書く。時間軸の統合ビューが無い |
| [probe-rs VS Code](https://probe.rs/docs/tools/debugger/) | 同上 | `chip` と `programBinary` だけで書き込みからデバッグまで。RTT のチャンネルごとに端末タブ | Alpha |
| [STM32CubeMonitor](https://wiki.st.com/stm32mcu/wiki/STM32CubeMonitor:How_to_optimize_acquisition_speed) | Node-RED のフロー＋Web のダッシュボード | ELF から変数を選ぶ。ブラウザで遠隔から見られる | サンプリングの周期が PC 任せで揺らぐ |
| [Micrium µC/Probe](https://micrium.atlassian.net/wiki/spaces/osiiidoc/pages/131375/uC-Probe) | 自由配置のダッシュボード | 部品（メーター・LED・スライダ・グラフ）に変数を割り当て、コードを書かずに読み書き | Windows 専用 |
| [Arm Streamline](https://learn.arm.com/learning-paths/mobile-graphics-and-gaming/ams/streamline_example/) | タイムライン＋表 | 性能カウンタと処理を時間で対応付け。Calipers で区間、プロセス→関数→ソースと掘り下げ | 主に Linux / Android |
| [SEGGER RTT Viewer](https://kb.segger.com/J-Link_RTT_Viewer) | 端末のタブ | 仮想端末ごとにタブ、CLI | PC 側では正確な時刻を付けられない |

## 第 3 部 まとめ

### A. 優れたツールに共通する UI の型

1. **共通の時間軸のタイムラインが中心**（段を縦に積む）。Ozone・IAR・System Analyzer・SystemView・TRACE32。
2. **カーソルが 3 種類**: 選択カーソル（全窓をその時点に）、マウス位置の縦線（全段の値）、時間の基準点（差分）。
3. **ビューの双方向の連携**: どの一覧・統計・グラフからもダブルクリックでタイムラインへ。タイムラインで選ぶと
   ソース・変数の窓がその時点に。TRACE32 は拡大率まで揃える。
4. **データからソースへ 1 クリック**（IAR の Go To Source、µVision の Code ボタン、Energy Profiler の関数表）。
5. **統計と生データの往復**（外れ値をダブルクリックで実際の事例へ）。
6. **構造化された検索**（正規表現、SQL に似たクエリ、値の条件）。
7. **キーボードで同じ関数・タスクの次/前へ**。
8. **GUI の操作と同じ働きのコマンド**（Ozone のスクリプトとソケット、TRACE32 のコマンドと LOG）。

### B. どのツールもうまくできていないこと

| 穴 | 現状 |
|---|---|
| デバッガとロジアナが同じ時刻を共有する | 事実上 TRACE32＋PowerIntegrator（非常に高価）と DT+ だけ。Ozone の時間軸に外部の信号は入らない |
| 1 つの調査が 1 つのツールで閉じない | 電力・RTOS・変数・ログ・デバッグがツールごとに分かれ、1 つの記録にまとまらない |
| 長時間の記録と細部の両立 | リング式で短いか、長いが重く見づらい。要約から細部へ降りる UI と異常の自動マークが弱い |
| 調査の経緯が残らない | 「何を疑い、何を確かめ、どう結論したか」を記録と結び付けて残し共有する仕組みが無い |
| AI が人と同じ情報を同じ形で使えない | 自動化の口は操作のコマンド版だけで、画面の状態を返さない。人と AI が 1 つの画面を共有して交代する設計は無い |
| 導入までの手間 | 記録コードの組み込み、高価なプローブ、設定ファイルの手書き、起動スクリプト探し |
| プローブの能力が見えない | 「なぜこのビューが空なのか」が UI から分からない |

### C. fprobe の画面への提案（調査の担当のまとめ）

1. 主役は「1 本の共通時間軸の上に段を積む」画面（ロジアナ・解読・PC サンプル・RTOS・RTT/SWO のログ・変数・電圧・
   イベント・**AI の行動**）。周りのパネルはドッキング式、小さい画面ではタブ。
2. カーソルを 3 種類に分ける。基準点や範囲を URL にも反映し、リンクで同じ視点を再現できる。
3. 連携の規則を統一: 1 回クリック = 選ぶ（時刻を同期）、ダブルクリック = タイムラインへ、修飾キー付き = ソースへ。
4. 時間軸: ホイールで横の拡大、ドラッグでパン、Fit・Go To Time、全体の縮図（ミニマップ）に異常の印、拡大率に応じた
   間引き（既に `la.view` で実装）。
5. 検索は「条件 → 全件一覧 → 時間軸に印」。同じ条件をそのままハードのトリガ（クロストリガ）にできる。
6. ソースとの往復: 時間軸の点からその時刻の行へ、ソースの行の横に「通った回数・最後の時刻」。
7. 能力に応じた UI: 使えない機能は隠さず「X が必要」と理由を示す。
8. 最初のビューまで最短に: プローブを選ぶ → ELF を落とす → ターゲットを推定 → すぐタイムライン、の 3 手順。
9. AI の行動を段と操作の履歴として見せる。人と AI が画面の状態（カーソル・範囲・開いている段）を共有し、同じ「画面
   状態の API」を MCP でも公開する。
10. 一時停止と交代: 一時停止したら AI の未実行の操作の列を見せ、人が承認・編集・破棄してから返せる。状態を変える
    操作は人の確認を挟めるように分類する。
11. 調査の過程を残す: 印や範囲にメモ・仮説・結論、AI の推論の要約も同じ印に付け、記録とまとめて共有。
12. 標準との共存: VCD の書き出し、CMSIS-DAP 経由の標準ツール。画面は標準ツールに無い共通時間軸の解析に集中する。

### 確認できなかった点

Energy Profiler の波形の点からソースへの移動、Tracealyzer のソースへの移動と公式 CLI、µVision の Logic Analyzer の
変数の上限と VS Code 版での対応、µC/Probe のドラッグの操作、DT+ の画面の詳しい配置、Ozone の現行のライセンス条件、
TRACE32 の学習コストの第三者の評価。
