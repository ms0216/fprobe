# 設計原則の点検（検出のみ。修正は未着手）— 2026-10-25

対象: CLAUDE.md「設計原則（必ず守る）」の (1) 本体にヒューリスティックを入れない、(2) 画面は考えない層。
土台: `integ/round1`（93e2db66）。製品コードは変更していない。再現試験は不要（原則違反は試験にしにくい）ため付けていない。

重さ: A = 結果を黙って誤らせうる / B = 利用者（人・AI）に見える紛らわしさ・人と AI で見えるものが違う / C = 軽微。
確信度: 高 = コードで事実を確認 / 中 = 事実は確認、原則に反するかの線引きに幅 / 低（疑い）= 理由つきで載せる。

## 要約

| 重さ | 件数 | ID |
|---|---|---|
| A | 0 | （後述: 過去 5 回の点検で本体の経験則はほぼ外されており、結果を黙って誤らせる残りは見つからなかった） |
| B | 5 | P01, P02, U01, U02, U04 |
| C | 10 | P03〜P07, U03, U05, U06, U07, U08（U03 の ThreadsPanel の分子の選択だけ B 相当） |
| 疑い（確信度 低〜中） | 5 | P02, P03, P05, P06, U07 |
| 合計 | 15 | 本体 7（P01〜P07）、画面 8（U01〜U08） |

総評: 本体（host/fprobe）は、plugins/heuristics.py への切り出しと「使う側が明示する引数」化が行き届いている
（correlate の window_s、testpoint の long_s / silence_s、uart のボー、liveness 等）。残りは「実行時の暗黙の補正」と
「暗黙の既定の数」。画面（host/ui）は API の応答を描く方針がほぼ守られている（コメントにも「画面で数えない」と明記）。
残りは割合の計算、故障の判定、縮図の異常の分類、状態の判定の 4 か所。

---

## 本体（host/fprobe）

### P01 [B / 確信度 高] PCSR プロファイルが、使う側に告げずサンプラの周期に乱数のゆらぎを入れる
- 場所: `host/fprobe/probe_profile.py:139-143`（`_start_pcsr_sampling`）。受け側 `sampler.py:114-119`。
- 事実: `run.jitter = max(0, min(15, int(period_cycles).bit_length() - 2))` と周期から自動で決め、`sampler.start(jitter=run.jitter)`
  する。コメントは「周期の半分まで。割り込み等の周期と噛み合う偏り（エイリアシング）を防ぐ（実機で SysTick が 0% か 0.8% に偏った。真は 0.1%）」。
  引数 `profile.start` にも結果の `plan.as_dict()`（`probe_profile.py:108`）にも jitter は出ない（grep で `run.jitter` は 142-143 行のみ）。
- 原則: 「ターゲットは周期的な処理をする」という仮定に基づく補正を、使う側が選べず・結果にも出さず既定で適用している。
  サンプルの時刻が一様でなくなる（平均は同じ）ことを、結果を読む人・AI は知れない。境界の値は「使う側が明示して渡す引数」の原則に反する。
  なお「半分」という比も経験則。
- 直し方の案: `profile.start` に `jitter`（0〜15。既定 0 = ゆらぎなし、または必須）を足し、使った値を `plan.jitter` と
  `sampler_jitter_cycles`（実際の幅 = 2^k サイクル）で結果に出す。偏りの見え方は `profile.result` に周期を知らせて使う側が判断する。
  自動選択が要るなら `plugins/heuristics.py` に移す。
- API 仕様案: 入力 `profile.start{ method:"pcsr", rate_hz, jitter?:int(0..15) }`、出力 `plan{ ..., jitter:int, jitter_span_cycles:int }`。

### P02 [B / 確信度 中（疑い寄り）] `recording.correlate` が先行率の順に並べ、同じ順で上位 `limit`（既定 20）に切る
- 場所: `host/fprobe/correlate.py:27-28`（`AUTO_LIMIT = 20`）, `:524-525`（`_rank`）, `:571-598`（`_candidates`）, API 既定 `recording.correlate limit 20`。
- 事実: 自動の候補を作り、`preceded` の大きい順に並べ（同点は文字順）、先頭 `limit` 個だけ返す。切ったことは `truncated` / `causes_total` で申告する
  （測定の限界の申告としては原則に合う）。ただし「どれを残すか」の基準が `preceded` の大きさ。
- 原則: 並べ替えそのものは測った事実の順なので「判定」ではない。しかし plugins/heuristics.py（`correlate` 系のコメント）自身が
  「先行率 0.01・lift 2.9 が、先行率 1.0・lift 2.6 の本当の原因より上に来た」と書いており、`preceded` の順で切ると、
  いつも起きている候補（coverage ≈ 1）が先頭を占め、本物が 20 位の外に落ちうる。先行率が大きいほど原因らしいという仮定を、切り口に入れている。
  自動の候補の作り方（変化・列挙値・組合せ）自体も「何を試すか」の選択（`_candidates`）。
- 直し方の案: 切る基準を使う側の引数にする（`order_by: "preceded"|"coverage"|"name"` を必須、または切る前に全部返し `limit` は使う側が明示）。
  既定は「生成順」にして、順位づけは使う側に任せる。
- API 仕様案: 入力 `recording.correlate{ ..., order_by:"name"|"preceded"|"coverage", limit?:int }`（`limit` 省略 = 全件）。出力は今のまま `causes_total`, `truncated`, `order_by`（使った並び）を返す。

### P03 [C / 確信度 中（疑い）] 補助マイコンの `live` 判定が暗黙の待ち 10 ms と「計測間隔 1 ms」の仮定に依る
- 場所: `host/fprobe/aux_mcu.py:49-55`（`Aux.read(settle_s=0.01)`）。
- 事実: `samples` が 10 ms の間に増えたかで `live` を決める。コメントは「補助マイコンのプログラムの計測の間隔 1 ms より長い値」。
  結果には `settle_s` と前後の `samples` が付くので、事実は隠れていない。
- 原則: 待ち時間の既定が、特定の補助プログラム（1 ms）の仮定に結びつく。別の補助ファーム（計測が遅い）なら `live=False` と誤判定。
  `measured_volts`（UI `lib/power.ts`）は `live` を信用して 0 V を「データなし」にしているので、誤判定は電圧の欠けに見える。
- 直し方の案: `settle_s` を使う側が渡す引数にする、または `live` を判定せず `samples_before`/`samples_after`/`waited_s` の事実だけを返し、判定は使う側。
- 確信度が中の理由: 申告（settle_s・前後の数）があり、実害は補助ファームを替えたときだけ。

### P04 [C / 確信度 中] 結果の形を変える暗黙の既定の数（窓・箱の数・バケット）
- 場所（API 既定値を `host/api/fprobe_api.json` から抽出）: `rtos.timeline.load window_us 10000`（`probe_rtos_analysis.py:95-103` 付近。
  使った窓は `window_us` で返る）、`timing.measure bins 20 / longest 10`、`user.trace.stats bins 20 / longest 10`、
  `testpoint.report buckets 120 / tree_depth 8 / anomalies 100`、`profile.timeline bucket_s 0.1`、`testpoint` の Analyzer `bucket_s=1.0`
  （`testpoint/analysis.py:244`）、`recording.transactions rows 20`、`recording.cpu_load` の窓。
- 事実: 窓幅・箱の数・バケットの幅は、負荷のピーク、ヒストグラムの形、時間バケットの件数を変える。省略すると暗黙の値で計算される。
  多くは結果に使った値が出る（`window_us`）が、`bins`/`buckets` は箱の端 `lo/hi` で間接的に分かる程度。
- 原則: 「境界の値は使う側が明示して渡す引数にし、既定の暗黙の値にしない」。画面は窓を必須にして守っている
  （`CpuLoadPanel` の `windowUs`、`EventLogPanel` の `canMeasure`）が、API（AI 側）は暗黙の既定で動く。人と AI で操作が一対一にならない。
- 直し方の案: 分布・窓の幅は必須引数にする（または既定を使った場合に `defaulted:true` を結果に付ける）。切り捨ての上限（`limit`）は申告があるので対象外。
- 備考: `rate_hz`（`profile.start 10000`、`rtos.timeline.start 20000`）は取得の設定で、使った値が `plan` に出るので載せない。

### P05 [C / 確信度 低（疑い）] LA ストリームの時刻の不確かさに「4 サンプル」を固定で入れている
- 場所: `host/fprobe/recording_stream.py:34`（`TRIGGER_SLACK_SAMPLES = 4`）, `:71`。
- 事実: トリガの時刻が分かったとき、`StreamAnchor.uncertainty = 4 × サンプル周期`。コメント「符号器がトリガを見るまでの遅れ（数サンプル）」。
- 原則: 不確かさの幅が、測った値ではなく定数。RTL の実際の遅れ（固定の段数）なら事実として申告できるが、コード上はその根拠が無い。
  ケイパビリティで申告された値か、RTL の段数を CSR から読んだ値にするべき。
- 確信度が低い理由: RTL が本当に 4 サンプル以内なら安全側の申告になる。根拠の出どころ（ゲートウェア側の固定遅れ）を調べていない。
- 直し方の案: ゲートウェアが遅れ段数を CSR（caps）で申告し、`TRIGGER_SLACK_SAMPLES` をそこから読む。

### P06 [C / 確信度 低（疑い）] 時計の対応付けが「直近 64 点」の窓の直線に固定
- 場所: `host/fprobe/recording.py:29`（`CLOCK_WINDOW = 64`）, `:61-76`（`ClockMap`）。
- 事実: ホスト時刻と ticks の対応を直近 64 点の最小二乗で作り、不確かさ = 往復の半分 + 傾きの誤りの上限 × 距離で出す。
  境界の値で切り替えない設計は丁寧（コメントにある）。ただし窓の点数 64 は定数で、時計がゆっくり曲がる（温度ドリフト）場合に
  直線の仮定が外れる。不確かさの式は「各点の誤りが u 以下で直線」の仮定の上に立つ。
- 原則: 仮定（時計が窓の中で直線）に頼る推定。外れたときに気づけない。
- 直し方の案: 当てはめの残差の最大（`residual_max`）を不確かさに足す（`merge.py` は既にこれを持つ）。窓の点数を記録のメタに出す。

### P07 [C / 確信度 中] FPGA 観測の待ち時間に経験則の倍率と暗黙の既定
- 場所: `host/fprobe/fpga_obs.py:80`（`id_scans` 無しなら 40 スキャン）, `:91-93`（`* 1.5 + 1e-4` = 「命令の間の分を見込んで 1.5 倍」）, `:196`（`max(1.0, 40 * scan_s)`）。
  `open` の既定 `ir_len 4 / user_ir 2 / half_period 10 / words 64 / sync_period 100`（`:56`, `:75`）。
- 事実: 待ちの上限の倍率が経験則。超えると `TimeoutError` / `stuck`。測定値ではなく待ち時間だけの話。`scan_seconds()` が見積もりを返すので、
  それを「測った 1 スキャンの長さ」として使うと誤る。
- 原則: 待ちの倍率は測定結果を変えないので軽微。ただ「見積もり」を事実として返す API があるなら、見積もりである旨を付けるべき。
- 直し方の案: `timeout_s` を使う側の引数にし、`scan_seconds()` は `estimated:true` を付ける（または実測 = `scans` の増加から測る）。
- 備考: `half_period` 等の既定は接続設定で、使った値は `fpga_obs.status.config` に出る。

---

## 画面（host/ui/src）

画面の全体は読んだ（lib 約 60 ファイル、panels 約 50 ファイル）。多くは「API の順のまま」「画面で数えない」と明記され、守られている。
下は守られていない箇所。

### U01 [B / 確信度 高] 故障の判定を画面が持つ（例外名の集合・自動で開く条件）
- 場所: `host/ui/src/lib/fault.ts:43-48`（`FAULT_EXCEPTIONS = {"HardFault","MemManage","BusFault","UsageFault","SecureFault"}`, `isFault`）, `:62-71`（`watchFaults`）。
- 事実: `target.fault` の `exception.name` が上の集合に入るかで「故障の例外の中」と判定し、`FaultPanel` を自動で開く。`isFault` は
  `report.faulted || 名前の集合` で故障を決める。
- 原則: 画面に判定の基準（どの名前が故障か）がある。AI は同じ判定を API から得られない（名前を自前で照合する）。
  名前の集合は Cortex-M の例外名で、ほかのアーキテクチャ（RISC-V、Cortex-A の abort）では当たらない。
- 直し方の案: `target.fault` の結果に判定済みの事実を足す。画面は描くだけ（自動で開く条件も `fault.in_fault_exception` を読む）。
- API 仕様案: 出力 `target.fault{ ..., exception:{ name, number, is_fault_exception:bool }, in_fault:bool }`（`in_fault` = ビットが立っているか、または故障の例外の中。定義は API の docstring に）。

### U02 [B / 確信度 中] 縮図の「異常の印」の分類を画面が決める
- 場所: `host/ui/src/lib/timeline/query.ts:84-104`（`anomalies`）, `lib/timeline/decimator.ts:9,89-91`（`ANOMALY_BINS = 4096`）, `components/TimelineMinimap.svelte:57`。
- 事実: タイルの各出来事の `tone` から `failure → 1`、`stop / mark / other → 3（warn）`、`plain / running → 0` と決め、時間の箱ごとに bad/warn の最初の 1 つを残す。
  つまり利用者の「印」（annotation.mark）や SWO の other が画面上で「注意」の色になる。
- 原則: 何が異常かという分類（印と停止を warn にまとめる）が画面にある。`tone` 自体はデーモン（`timeline_rows.py` の `Rule.tone`、事実 = `ack != 1` 等）が決めているが、
  tone → 異常の写しと、箱ごとに 1 つに間引く基準は画面の中。AI は縮図に何が出ているかを API から得られない。
  間引き（箱の 1 つ）は見せ方だが、`mark/other` を異常とするのは見せ方を超える。
- 直し方の案: 縮図の印の分類をデーモンに移す（tone の値そのものをそのまま描き、色の割り当てだけ画面）。`mark` を異常に数えるなら API が `anomaly:true` を返す。
- API 仕様案: `timeline.tile` 出力に `tones:{ failure:int, stop:int, mark:int, other:int }` の件数と、`timeline.anomalies{ source, t0, t1, bins }` → `[{t, tone}]`。

### U03 [C（ThreadsPanel のみ B） / 確信度 高] 使用率の割合・棒の長さを画面が計算する
- 場所: `components/panels/HeapPanel.svelte:19,68-69`（`pct(h.used, h.total)` を棒と「(x%)」の文字に）, `components/panels/ThreadsPanel.svelte:121`
  （`pct = peak_used ?? used` ÷ `size` を丸め）, `components/panels/RtosObjectsPanel.svelte:153-155`（`used / total`・`peak_used / total` を棒の位置に）。
- 事実: 割合を `Math.round(1000 * part / whole) / 10` で作り、数字として表示する。`ThreadsPanel` は `peak_used` があれば `peak_used`、無ければ `used` を
  分子に選ぶ（どちらを割合の基にするかの判断が画面）。比べて `lib/stack.ts:32 usagePercent` は API の `fraction` を丸めるだけで正しい。
- 原則: 画面に割合の計算がある。数字として出す値は API の値であるべき（棒の長さだけなら見せ方）。
- 直し方の案: `heap.scan` の各ヒープに `used_fraction`、`rtos.threads` のスタックに `peak_fraction` / `used_fraction` を足し、画面は丸めて描く。
- API 仕様案: `heap.scan.heaps[].used_fraction:float`（`used / total`）, `rtos.tasks[].stack{ used_fraction, peak_fraction|null }`, `rtos.objects[].heap{ used_fraction, peak_used_fraction }`。

### U04 [B / 確信度 中] 状態の「ok」の緑の点を画面が複数の事実から組み立てる
- 場所: `components/panels/SystemPanel.svelte:451-452`（`mcuOk = connected && !error`、`fpgaOk = available && running && !stuck`）, `:512`, `:576`。
- 事実: `system.status` の複数のフラグを AND して「正常」の点を点灯。`stuck`/`overflow_seen` は別の色（bad/warn）で描く。
- 原則: 正常の判定（どのフラグの組が「ok」か）が画面。AI は「ok」を API から得られず、同じ式を自前で再現する。
  `FpgaObsPanel`（`lib/fpgaobs.ts statusKeys`）のように「立っている事実を全部並べる」形のほうが原則に合う。
- 直し方の案: 点を「事実の列」（接続している / 誤りがある / 止まっている）にして、色は個々の事実に割り当てる。「ok」という総合判定を画面で作らない。

### U05 [C / 確信度 高] 集計・件数を画面で数える小さな箇所
- 場所と内容:
  - `lib/rtos.ts:151-163`（`objectKinds`: オブジェクトの種類ごとの件数を数えてタブに出す）。
  - `lib/logic.ts:141-145`（`hiddenKinds`: 絞った種類の外の件数を合計）。
  - `components/panels/DataSamplingPanel.svelte:274-277`（読まなかった区間の長さの合計 `gaps.reduce`）。
  - `lib/eventlog.ts:235`（`kindRows` の `max`: 棒の目盛り。見せ方）, `ProfilePanel.svelte:169`（`maxFraction`）, `lib/profile.ts:64`（行の濃淡の `max`）— これら 3 つは棒の長さの目盛りで見せ方なので対象外に近い。
  - `lib/timeline/compose.ts:148`（`visible[r] = counts.reduce`）→ `TimelineRows.svelte:22`（段ごとの「見えている出来事の数」）。
- 原則: 数えた値を画面に出している（API が返していない）。単純な合計だが、AI と人で見える値が一対一でなくなる。
- 直し方の案: `rtos.objects` に `kind_counts`、`la.decode` に `hidden:{n, kinds}`、`watch.series` に `gaps_total_us`、`timeline.tile` に段ごとの `visible_count` を足す。

### U06 [C / 確信度 中] 条件の式の意味を画面が解釈する
- 場所: `lib/system.ts:68-71`（`fpgaOnly`: 条件が FPGA の信号だけで変化の項を含むか）, `parseCondition`。
- 事実: 相関の候補の条件を画面が構文解析し、「FPGA の条件の表に足せるか」を決める。
- 原則: 判定は画面。デーモンの `correlate.parse_condition` と文法が二重になる（片方だけ直すと食い違う）。
- 直し方の案: `recording.correlate` の各 `causes[]` に `kind:{mcu|fpga|pin}` と `fpga_addable:bool` を返す。

### U07 [C / 確信度 低（疑い）] システム解析の段の並び・畳み方を画面が決める
- 場所: `lib/timeline/rows.ts:176-207`（`SYSTEM_GROUPS`, `SYSTEM_FOLDED`, `systemOrder`, `systemHides`）。
- 事実: 「MCU のメモリの標本 → FPGA → ピン → DAP → …」の固定の並びと、`host / clock / la` を畳む、記録で変化の無いピン（API の `unchanged_channels`）を隠す。
  ユーザーの操作に付けた「システム解析」という設定の既定を画面が持つ。
- 原則: 「利用者が選んだ列での並べ替え」ではなく、画面側の既定の並び。ただし並びは見せ方で、隠すピンの根拠は API の事実。コメントに「頼まれずに隠すので数を出して見せ直せる」とある。
  AI が同じ並びを得る手段が `ui.state` にあるか未確認（疑い）。
- 直し方の案: 並びの既定を `ui.state` の `rows` に API で設定できるプリセットとして持ち（`ui.state.set{ rows: preset:"system" }`）、画面はそれを呼ぶだけ。

### U08 [C / 確信度 高] TCK の MHz → 半周期の換算と丸めを画面が行う
- 場所: `lib/fpgaobs.ts:48-52`（`tckToHalfPeriod`: `half = clamp(ceil(sysHz / (2 × mhz × 1e6)), 2, 65535)` と実際の速さ）, `FpgaObsPanel.svelte:31,49`。
- 事実: 利用者が MHz で入れると、画面が半周期に換算して `fpga_obs.open` に渡し、実際の速さを表示。丸め方（切り上げ = 求めた速さを超えない）は画面の判断。
- 原則: 計算が画面。AI は `half_period` を自前で換算するしかない。
- 直し方の案: `fpga_obs.open` に `tck_hz`（求めた上限）を受け付け、結果に `half_period` と実際の `tck_hz` を返す。
- API 仕様案: 入力 `fpga_obs.open{ tck_hz?:float | half_period?:int }`（どちらか）、出力 `config{ half_period:int, tck_hz:float }`。

---

## 調べたが対象外と判断したもの（記録）
- 画面: `Math.min/max` の拡大縮小・範囲の切り詰め、`format.ts`/`time.ts` の桁、`dashboard.ts` の `fraction`/`gaugeArc`/`sliderStep`（ウィジェット自身の見せ方）、
  `timeline/*` の間引き・ラベルの重なり除去、`ProfilePanel` の行の行番号順（列の並べ替え）、`CpuLoadPanel` の窓（必須の選択肢）、
  `*_TOP`/`*_LIMIT`（切ったら数と上限を出す = 限界の申告）、`flash.ts` の `DEFAULT_OPTIONS`（API の既定と一致）。
- 本体: `*_LIMIT`・`top` の切り捨て（`truncated` / `*_total` で申告）, `testpoint` の `long_s`/`silence_s`/`open_s`（使う側が明示）,
  `correlate.window_s`（必須）, `liveness`・`halt_risk`（事実だけ、慣習の表は plugins）, `dwt.py` の `headroom`（使う側が明示。既定 1.0 = 帯域の上限）,
  `inject.py` の `verdict`（使う側が書いた期待値との比較）, `rtos/flow.py inversions`（優先度の定義による事実）,
  `timeline_rows.py` の `tone`（`ack != 1` 等の事実）。
- plugins/ 配下（経験則を載せる所。原則の対象外）。
- 台帳 `docs/reports/2026-10-24-heuristics-audit-2` までに既に外されたもの（`halt_risk` の接頭辞、`diagnose` の結論、`disasm` の境界推定、`uart` のボー推定 等）は再掲していない。

## 調べた範囲 / 調べていない範囲
- 調べた: `host/fprobe/` 全体の grep（`default`, `heuristic`, `likely`, 数値定数の大文字定義 約 200 件、`sorted/key=`、`warnings.append`、日本語の「通常・見込・目安・かもしれ」）、
  `correlate.py`, `recording_stream.py`, `recording.py`, `probe_profile.py`, `profile/dwt.py`, `profile/cycles.py`, `aux_mcu.py`, `fpga_obs.py`, `flash_bp.py`,
  `testpoint/analysis.py`, `rtos/flow.py`, `timeline_rows.py`; API 仕様 `host/api/fprobe_api.json` の既定値 200 件のうち解析系; 画面 `lib/` と `components/panels/` の
  `reduce / sort / Math.* / しきい値 / class: による色分け / 数値定数` の grep と、該当ファイルの読み。
- 調べていない: `host/fprobe/decode/` の個々のプロトコル、`dwarf/`, `gdb/`, `linux/`, `rtos/*` の各 RTOS ごとの読み、`host/pynq`, `host/firmware`, ゲートウェア（RTL）側の定数、
  `i18n.ts` の文言の中の断定的な言い回し、`Chart.svelte`・`timeline/render-*` の描画の中身。実機・ネットワーク・合成には触れていない。
