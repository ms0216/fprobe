# 専用の画面が無い API から、既定の画面にするものを選ぶ（2026-11-02）

日付: 2026-11-02。利用者は「ユースケースとその価値最大化を念頭に置いて」選ぶのを任せた（2026-11-02）。
親の仕様: docs/superpowers/specs/2026-10-22-ui-all-apis-design.md（段階 A: 全 API を API パネルで呼べる）、
2026-10-22-ui-stage-b-design.md（段階 B: お気に入り・専用の画面 9 件）、2026-10-23-ui-rebuild-design.md（考えない画面・
足し引きできる画面・強みで厳選した既定）。根拠の資料: docs/reports/2026-10-24-fprobe-value-analysis.md（場面 P1〜P12・
推奨 1〜5）、docs/reports/2026-10-22-ui-api-coverage.md（画面が呼ばない API の分類）。

## 1. 前提（決まっていること）

- どの API も API パネルから人が呼べ、お気に入りに足せる（段階 A・B）。ここで選ばなかった API も、人ができないことには
  ならない。専用の画面は「見せ方が要る」「その場面の流れの中で押せることに価値がある」ものに限る（段階 B §1）。
- 既定の画面は fprobe の強み（解析力 = ロジアナとデバッガの一体化・クロストリガ・共通タイムスタンプで「なぜ壊れたか」を
  見つける）を出す機能を厳選する（画面の作り直しの仕様 原則 4）。
- 画面は考えない層（CLAUDE.md）。値・並び・切った数・欠けは API が返したまま描く。秒・µs への単位の書き換え、16 進、
  時刻の欄（TimeLink）は見せ方として持ってよい。

## 2. 今の事実（2026-11-02 に画面のコードを読んで確かめた）

依頼の 38 を host/ui/src の呼び出し（`conn.call` / `app.act` の文字列）で照合した。`trace.etm.history` は Fault の
「直前の命令」（lib/etm.ts）が既に呼ぶので 38 には入っていない。ほかの 38 はどれも画面から呼ばれていない
（動的に名前を組む箇所も `rtos.timeline.get/load/start/stop`・`log.start/stop/read`・`cycles.measure`・`gpio.read/set` だけ）。

## 3. ユースケースと、そこで要る API

| ユースケース（根拠） | 場面 | 38 のうち効くもの |
|---|---|---|
| U1 組み込みの開発者が「なぜ壊れたか」を調べる（価値の分析 推奨 1・P5 壊れた後の解析） | パニック・アサートの出力から、どの関数・行で壊れたかを引く | `log.annotate`（UART・RTT のパニックの文字の 0x を関数・ファイル:行に） |
| U2 時間を測る（推奨 3・P2。Barr の再現しにくい不具合の上位 10 のうち 3 つが時間の問題） | 関数の区間の時間・遅い回・異常（long_call・missing_exit 等）を、記録と同じ時刻で見る。カバレッジ | `testpoint.*` 6 件（価値の分析 §5.3・§5.7-5 が「窓が無い」と名指し） |
| U2' RTOS の時間の問題（P2・P8。Barr: ジッタ・優先度・スタックのあふれ・デッドロック） | タスクの応答時間の分布と最悪の回、キューの満杯・空・待ち、タスクごとのスタックの最大とあふれの印 | `rtos.timeline.jobs` / `queues` / `memory` |
| U3 AI による開発の自動化を人が見守り交代する（推奨 横断・P9） | AI が始めたテストポイント・ログの受け取りの様子（受けた数・取りこぼし・解読できなかった数・警告）を人が同じ画面で見る | `testpoint.status`・`log.status`（通知 activity で読み直す） |
| U4 Linux のアプリ・Cortex-A のデバッグ（P11。Pi 2B・PYNQ-Z2 は JTAG のみ） | つながらないときに JTAG のチェーンの事実（装置の数・IDCODE・IR 長・TDO の張り付き）を見て、`jtag_chain` を渡して接続する | `jtag.scan` |
| U5 日常のデバッグ（P12。独自性は無いが壊れないこと） | — | `target.core.select`・`debug.elf.size`・`cycles.read` 等（下の §5 で選ばない理由） |

## 4. 選んだもの（既定の画面にする）

| 順 | API | 置き場所 | 価値の理由 |
|---|---|---|---|
| 1 | `testpoint.start` / `stop` / `status` / `report` / `calls` / `coverage` | **新しいパネル「Testpoints」**（View の解析のまとまり） | U2 の入口が画面に無かった（価値の分析 §5.3）。関数ごとの時間の分布・異常の時刻は、記録と同じタイムベース（`t`）を持つので時間軸へ飛べる = 共通タイムスタンプの強みが直接出る。カバレッジの通らなかった印は「試験を足す手がかり」で AI の自動化（U3）とも共有。AI が始めた受け取りの様子も通知で読み直す |
| 2 | `rtos.timeline.jobs` / `queues` / `memory` | **新しいパネル「RTOS Timing」**（解析のまとまり。CPU Load の時間軸を使う） | Barr の上位 10 の時間・資源の問題（ジッタ・優先度・スタックのあふれ・デッドロック）に直結する。最悪の回（応答時間・実行時間・スタックの最大）の時刻を時間軸へ飛ばせる。CPU Load は「割合」、こちらは「1 回ごとの分布と最悪の回」で役割が違う |
| 3 | `log.annotate` | **Terminal** のバーに「番地を引く」（選んだ行か、見えている源の全部） | U1。パニックの出力は Terminal に出る。番地 → 関数・ファイル:行の表から Source・Disassembly へ（`showAddress`）。機器に触らないので走行中でも押せる |
| 4 | `log.status` | **Log** のバーの下の 1 行（形式・行の数・重要度ごとの数・**解読できなかった数**・受けたバイト・警告・誤り） | 欠けを明示する（feedback: 判断より情報をそろえる）。解読できなかった行の数が見えないと、ログが「全部」に見える。AI が始めたログも同じ行で見える（U3） |
| 5 | `jtag.scan` | **接続のダイアログ**に「JTAG のチェーンを走査」と `jtag_chain` の欄 | U4。JTAG だけの対象（Pi 2B・Zynq）は `jtag_chain` が要るが、今の接続のダイアログは渡せなかった。走査の結果（IDCODE・設計者・IR 長の出どころ・曖昧なら候補・TDO の張り付き）を表で見せ、全部決まったときだけ返る `jtag_chain` を欄に入れる（接続は人が押す） |

13 の API。どれもホストの API は足さない（画面に計算を置かずに済む: 分布・最悪の回・割合・切った数・欠けは全部 API が返す）。

### 4.1 Testpoints パネル

- 始める: `method`（必須の列挙）を選ぶと、その方式で使う引数だけの欄を出す（欄は API の仕様 `fieldsOf` のまま。ApiFields）。
  方式ごとの欄の分け方は lib/testpoint.ts の表（gpio_serial / gpio_parallel / itm / rtt と共通）。表は見せ方で、
  **仕様のすべての引数がどこかの組に入っていること**を試験で確かめる（仕様に引数が足されたら試験が落ちて気づく）。
- 止める・読む: `testpoint.stop`（control）、`testpoint.report`・`calls`・`coverage` はタブで読む（呼ぶたびに押す。
  受け取りの最中は status だけを Poller で読み続ける）。
- 状態の行（`testpoint.status`）: 方式・受けた印の数・取りこぼし（losses）・経過・記録に書いているか・流し続けの
  overrun・`cyccnt_wraps`・`unsynced_symbols`・`skipped_before_sync`・警告・誤り。名前と値は API のまま。
- 関数のタブ: `functions` を API の順（子を含む時間の合計の多い順 = API が決めた順）に。回数・子を含む時間（合計・最小・
  最大・平均・σ）・自分だけの平均。`functions_total` と `limits.top` で切った事実を出す。
- 異常のタブ: `anomaly_counts`（種類ごとの数）と `anomalies`（`t_s`・`t`・種類・関数・detail）。`t` があれば時間軸へ飛ぶ
  （TimeLink、source = recording）。`anomalies_total` と上限を出す。開いたままの呼び出し（`open`）も同じ形。
- 呼び出しのタブ: `testpoint.calls` の `t0_s`・`t1_s`・`function`・`limit` は仕様の欄。結果は表（関数・始まり・時間・
  自分の時間・深さ・出口を見たか）。`total`・`truncated`・`dropped` を出す。
- カバレッジのタブ: 関数・C0・C1 の「通った / 全部」と割合（API の ratio を % に書くだけ）、通らなかった印（where・関数・
  種類・label）、分岐の組（API のまま）。`uncovered_total` と上限を出す。
- だれか（AI も）が `testpoint.start` / `stop` を呼んだら状態を読み直す（onCalls）。開いたときに前の受け取りの結果が
  あれば、今のタブを 1 回読む（AI が走らせた分も開けば見える）。
- 描かないもの（この段階）: 呼び出しの木（`tree`。入れ子の表示は Call Graph の作りに合わせて別に決める）、時間の区切り
  （`buckets`）。どちらも API パネルの JSON の木で読める。分岐の組（`decisions`）は API の JSON のまま出す。

### 4.2 RTOS Timing パネル

- 前提: `rtos.timeline.start`（CPU Load のパネル、または AI）で取った・取っている時間軸。無ければ API の誤りを出す
  （「取っていない」）。読むのは人が押したときと、だれかが `rtos.timeline.start` / `stop` を呼んだとき。
- タブ「ジョブ」（`rtos.timeline.jobs`）: タスクごと（API の順）にジョブの数・横取りの数・切れた数・実行時間・経過時間・
  周期・応答時間の分布（数・最小・中央・平均・p90・p99・最大・σ）。最悪の回（worst_exec・worst_wall・worst_response）
  は時間軸へ飛ぶ。区間（runs）も同じ形で。`notes` をそのまま出す。
- タブ「キュー」（`rtos.timeline.queues`）: オブジェクトごとに種類・長さ・最大・平均・最大 / 平均の使用率（API の割合を
  % に）・送り・受け・満杯（send_failed）・空（receive_failed）・待ち。`lost`（記録の欠け）を出す。
- タブ「スタック」（`rtos.timeline.memory`）: タスクごとに大きさ・最大・最小・最後・最大の時刻（時間軸へ）・あふれの印
  （API の `overflow`）。ヒープの最小の空き・その時刻。`memory=true` で取っていなければ API の誤りを出す。
- 時刻の 0 は CPU Load と同じ（`originFor`: 開いている記録のデータなら記録の始め、別のデータならデータの始め）。
- 推移（`points`）はこの段階では描かない（表の最大・最小・最後と、その時刻の時間軸への飛び先で見る）。欄の `points` は
  API の引数として出す（人と AI の呼び方を同じにする）。

### 4.3 Terminal の「番地を引く」

- 選んだ行があればその行、無ければ今の源の見えている文字全部（`plain(screen)`）を `log.annotate` に渡す。
- 結果は端末の下に表（書かれた値・引いた番地・関数+オフセット・ファイル:行）。番地のクリックで Source・Disassembly へ。
  API は「番地として書かれた数かどうかは決めない」ので、その旨（API の説明）をツールチップに出す。
- ELF が無ければ API の誤りをそのまま出す。

### 4.4 Log の状態の行

- `log.status` を、Log の読みと同じ時（開始・停止・読み足し）に読む。`running: false` だけ（まだ一度も始めていない）なら出さない。

### 4.5 接続のダイアログの JTAG

- 欄 `jtag_chain`（IR 長のカンマ区切り、TDO 側から。空なら送らない）。整数でないものがあれば送らずに欄の誤り。
- 「JTAG のチェーンを走査」: 接続の速さの欄の Hz で `jtag.scan`（未接続のときだけ。API が断る）。結果の表と notes・tdo。
  `jtag_chain` が null でなければ「この IR 長を使う」で欄に入れる（決まらなければ API が null を返し、候補を表に出すだけ）。

## 5. 選ばなかったもの（API パネル・お気に入りのまま）

| API | 選ばなかった理由（1 行） |
|---|---|
| `rtos.timeline.flow` | 有向グラフの配置を画面に作るのは重く、`dot` の文字を Graphviz で見るほうが正確。辺の回数は JSON の木で読める |
| `rtos.read` | 止めたときにタスクの文脈で式を読む 1 回の操作。引数（task・expression・frame）を入れて呼ぶだけでお気に入りで足りる |
| `user.trace.start` / `stop` / `stats` | 区間の時間は Testpoints（itm / rtt でも取れる）と Event Log の `timing.measure`（stream "user"）で見られる。channels の型の定義は JSON の欄が素直 |
| `watch.buffer_start` / `read` / `stop` | ファームの協力（配列と head）が要る特定の作り。値は Data Sampling の型で見るのが本来で、今は JSON で足りる |
| `trace.etm.start` / `stop` / `instructions` | 引数が部品ごとの番地（etm_base・tpiu_base・funnel）で、一度決めればお気に入りに置ける。止まった直前の命令は Fault に既にある |
| `trace.import` / `trace.snapshot` | 他の道具のファイル・RAM のバッファの取り込み。結果は時間軸（stream "rtos"）と CPU Load・RTOS Timing の窓に出るので、入口は API パネルで足りる |
| `scvd.load` / `scvd.view` | Keil の SCVD を持つ人だけ。木の結果は API パネルの JSON の木と同じ形 |
| `probe.design.get` / `switch` | Primer の構成の切り替え（基準の PYNQ-Z2 では使わない）。`target.connect` が要る構成へ自動で書き替える |
| `probe.name.set` | 一度だけの設定 |
| `target.core.select` | 複数ドロップの部品（RP2040）だけ。押す頻度は低く、お気に入りの 1 ボタンで足りる |
| `la.decoder_load` | 利用者の解読器を一度読むだけ。読んだ後は Logic Analyzer の解読器の一覧に出る |
| `gpio.release` | Low Level の GPIO の表は `gpio.set` の enable の欄で出力をやめられる。SPI の後の解放は API パネルで足りる |
| `uart.flow` / `uart.lines` | RTS/CTS を使う対象だけ。設定は 1 回、状態は JSON で足りる |
| `cycles.read` | 2 点の間は Source の `cycles.measure` にある。今の値の 1 回の読みは API パネルで足りる |
| `debug.elf.size` | ビルドの解析（Build Analyzer）で「なぜ壊れたか」の場面ではない（P12）。JSON の木で読める |
| `profile.export` | ほかの道具へ書き出す 1 回の操作（P8 は独自性が低い）。パスを入れて呼ぶだけ |

## 6. 既定の配置

新しい 2 つのパネルは View メニューの解析のまとまりに出す（閉じていても開ける）。利用者が承認した既定の配置
（解析の配置、2026-10-24）には足さない: 配置の中身は利用者の判断で、使われ方の記録（画面の作り直しの仕様 §3.4）で
見直す。案: 推奨 3（時間を測る）の場面では Testpoints を下の段の Logic Analyzer のタブに並べる。

## 7. 試験

- lib の関数を FakeConn で試す（このリポジトリの形）: 引数の組み立て（空の欄は送らない・誤りは欄ごと）、方式ごとの
  欄の組が仕様の全引数を覆う、結果の行（API の順のまま・切った事実）、時刻の欄（`t` が無ければ飛ばない）、通知で
  読み直す、`log.annotate` に渡す文字、`jtag_chain` の欄の読み取り、走査の読み（誤りは文で）。
- i18n の EN・JA のキーがそろうこと（既存の試験）。

## 8. やらないこと

- 画面で分布・最悪の回・割合・あふれを数えること（全部 API が返す）。
- 方式ごとの欄の既定の値を画面が持つこと（空 = 送らない。既定はデーモン）。
- 既定の配置を変えること（§6）。
