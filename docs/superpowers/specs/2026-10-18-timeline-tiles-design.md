# 共通タイムラインのタイルと画面側の間引き・WebGL2 の時間軸（移行の段取り 1〜3）

- 日付: 2026-10-18
- 元: `docs/superpowers/specs/2026-10-18-ui-framework-evaluation.md`（結論: ブラウザ + Svelte/TS を続け、時間軸は WebGL2、
  毎フレームの間引きは画面の Web Worker、デーモンはバイナリのタイルを返す）の §5 の 1〜3。
- 対象: デーモン（`host/fprobe/api/`）、タイル（`host/fprobe/timeline.py`・`probe_timeline.py`）、画面（`host/ui/`）。

## 1. 目的

拡大・移動・縮小の間、毎フレーム（60 fps = 16.7 ms）の絵を、デーモンを待たずに作る。そのために

1. デーモンは「見えている範囲の材料」（**タイル**）を 1 回で返す（生の変化点・出来事、または事前計算の要約）。
2. 画面は Web Worker でタイルから毎フレーム間引く（定義は Python の `la.Waveform.view` 等と同じ。試験で突き合わせる）。
3. 間引いた画素の状態を WebGL2 のテクスチャで送り、シェーダで描く。文字・カーソルは Canvas 2D を重ねる。

今の `la.view` / `recording.waveform` / `recording.density` / `recording.overview` は AI（MCP）のために残す。
AI と画面は同じ定義の結果を見る。

## 2. タイルの API `timeline.tiles`

`kind: read`、`record: false`、`binary: true`（§3）。MCP の道具には出さない（AI は上の JSON の読みを使う）。

| 引数 | 既定 | 意味 |
|---|---|---|
| `source` | `capture` | `capture` = 最後の捕捉（時刻はサンプル番号）/ `recording` = 読む記録（時刻はタイムベース） |
| `t0`, `t1` | 0, 終わり | 範囲 [t0, t1)。捕捉は捕捉の中に切り詰める |
| `channels` | すべて | 波形のチャンネル |
| `streams` | すべて | 出来事の段（記録だけ。捕捉に指定すると誤り） |
| `bin` | 範囲 / 4096 | 要約にするときの区間の幅の目安（画面は「1 画素の半分」を頼む） |
| `max_edges` | 2,000,000 | 生の変化点（全チャンネルの和）の上限。超えたら波形は要約 |
| `max_events` | 100,000 | 段ごとの生の出来事の上限。超えたらその段は要約 |

結果（JSON の部分）:

```
{ format: 2, source, t0, t1, tick_s（1 単位の秒）, extent: [データの始め, 終わり] | null, capture?: la.view の capture,
  coverage?: 長期の記録で間引いた・消した部分,
  wave: { mode: "raw" | "summary" | "none", channels: [...],
          raw:     edges（数）, pieces: [{ t0, t1, tps（1 サンプルの単位数）, length, s0, s1, channels, levels }]
                   区画 "wave.<piece>.<ch>" = f64 の変化点（サンプル番号、s0 < t < s1、昇順）。levels[k] = s0 の値
          summary: start, bin, bins。区画 "wave.states" = u8 [チャンネル × bins]（0 / 1 / 2 = 変化 / 3 = 無し）,
          names?: [チャンネルの名前]（記録の meta.json の channel_names） },
  lanes: [ { stream, count（[t0, t1) に始まる件数）, mode: "raw" | "summary",
             rows: [{ id, rule, type, title, unit?, jump?, hidden? }]（段。§2.1）,
             raw:     items, kinds: [種類の名前], labels: [ラベルの文字列]。区画（出来事の並び = t の昇順）
                      "lane.<i>.t" / "lane.<i>.t_end"（f64）/ "lane.<i>.kind"（u16）/ "lane.<i>.row"（u16、rows の番号）/
                      "lane.<i>.label"（u32、labels の番号。0xFFFFFFFF = 無し）/ "lane.<i>.value"（f64、無ければ NaN）/
                      "lane.<i>.depth"（u8、入れ子の深さ）/ "lane.<i>.tone"（u8、0 = 普通 / 1 = bad / 2 = good / 3 = warn）/
                      "lane.<i>.address"（f64、飛び先の番地。無ければ NaN）
                      （t0 より前に始まって t0 以降に終わるものも含む。t_end の無い出来事は t_end = t。状態・値の段は
                      t0 の前の最後の出来事も含む = 範囲の始めの状態・保たれている値）
             summary: start, bin, bins。区画 "lane.<i>.counts"（u32、t が区間に入る件数）、"lane.<i>.row_counts"（u32、
                      段 × 区間）、values の段があれば "lane.<i>.row_min" / "row_max"（f64、段 × 区間、無ければ NaN） } ],
  sections: { 名前: { dtype, offset, length } } }
```

- **かたまり（piece）**: 捕捉は 1 つ（t0 = 0、tps = 1）。記録は波形のかたまりごと（間に隙間 = 波形の無い時間）。
  s0・s1 は範囲 [t0, t1) の画素が引くサンプルの範囲（`Recording.waveform_view` と同じ写し方）。
- **要約の区間**: 幅は 2 の冪（捕捉・普通の記録）か縮図の段の幅（長期の記録で `bin` ≥ 段 0 の幅のとき）。区間の境は
  絶対時刻で幅の倍数（タイルどうしでそろう）。数は 16384 まで（超えたら幅を広げる）。捕捉の最後の区間は捕捉の
  終わりで切れる。
- **要約の作り方**: 波形は `Waveform.view` / `Recording.waveform_view` を区間の数の幅で呼ぶ（長期の記録は
  かたまりの要約の索引を使う）。出来事は新設の `Recording.event_bins`（1 回の GROUP BY）。長期の記録では
  幅が段の幅で境がそろっていれば、閉じた分割は事前計算の縮図（`recording_long` の索引。段 3 以上は全体の索引、
  未満は分割の縮図）から、開いている分割は生で数える（`SegmentedRecording.event_bins`）。これが
  「縮図の索引の一般化」: 任意の範囲・任意の段で stream ごとの区間の件数を返す。
- 生か要約かは、波形はまとめて、出来事は stream ごとに決める（疎な stream は生のまま、ラベルを付けられる）。

### 2.1 段の規則（format 2。`fprobe/timeline_rows.py`）

記録の stream は画面の「まとまり」（畳める。見出しの段は stream 全体の件数の密度）。まとまりの中の**段**は、出来事の
(kind, a, 本体) から規則で決める。段の名前 `id` = `<stream>.<規則>[:<キー>]`（キーが整数なら 0x%08X）で、記録の中で一意。
画面は段の並び・表示をこの名前で覚える。

| 段の型 | 描き方 | 規則の例（stream.規則 → 段を分けるキー・ラベル・値） |
|---|---|---|
| intervals | 区間（色の帯。入れ子は深さで内側を細く、外側の色を上に残す） | rtos.task（a = TCB、本体 task）、rtos.isr（a = 例外番号、重なりで入れ子の深さ）、rtos.blocked・inversion・gap、user.interval（name、深さは本体）、testpoint.call（深さ b） |
| bus | 解読した値の箱（文字） | uart.rx / tx（ラベルは文字）、swo.itm、annotation.decode（`<protocol>.<種類>` の protocol ごと、誤りは赤） |
| state | 次の変化まで続く状態の帯 | target.state（RUNNING = good、HALTED = warn。飛び先 = PC → ソース） |
| points | 印（三角） | annotation.mark、host.call（本体 role = ai / human / script ごと。誤りは赤）、dap.ap / dp（飛び先 = 番地 → メモリ）、rtos.kernel、annotation.loss、user.event |
| values | 値のグラフ（前の値を保つ階段 + 下を薄く） | sample.word（a = 番地。飛び先 = メモリ）、aux.voltage（mV → V）、user.value（a = 番号、本体 value）、swo.data |

- 規則は Python の関数（生: 出来事ごと）と、同じ意味の SQL の式（要約: 1 回の GROUP BY で段 × 区間の件数・最小・最大）の
  両方で書き、同じ段に分けることを試験で確かめる（`test_timeline_rows.py`）。知らない stream は kind ごとの印の段。
- 既定で隠す段（rtos.switch・clock）は `hidden`。段の色は画面がまとまりの中で順に付ける（色の意味 tone がある出来事は
  意味の色: bad = 赤、good = 緑、warn = 黄）。
- 要約の段 × 区間は `Recording.query_events`（長期の記録は残っている分割ごとに問い合わせて足す。間引いた分割の段の
  件数は分からない = coverage）。
- **範囲の前の状態・値**（`carry_in`）: 状態・値の段は、t0 の前の最後の出来事を段ごとに足す。範囲の幅の 16・256・4096 倍と
  前を探し（何も無ければ記録の始めまで）、見つかった段だけ（長く変わらない段がほかにあっても、そこまでは探さない）。

## 3. バイナリのフレーム（JSON-RPC と共存）

同じ WebSocket（`/api`）で、テキストのフレーム = 今の JSON-RPC 2.0、**バイナリのフレーム = バイナリの応答**。

```
0     4  "FPB1"
4     4  u32 LE = N（JSON の長さ）
8     N  JSON-RPC 2.0 の応答（UTF-8）{"jsonrpc":"2.0","id":…,"result":{…, "sections":{…}}}
         0 で 8 バイト境界まで
…        区画（それぞれ 8 バイト境界から。offset はフレームの頭からのバイト数、length は要素の数、LE）
```

- 要求はいつもテキスト（JSON-RPC）。仕様（`fprobe_api.json`）で `"binary": true` のメソッドだけが、**成功の応答**を
  バイナリのフレームで返す。誤りはテキストの JSON-RPC の誤り。応答と要求の対応は JSON の id（順序に依らない）。
- 通知（`activity` 等）はテキストのまま。1 つの応答 = 1 つのフレーム（分けない。区画の位置が JSON の中にある）。
- 区画が 8 バイト境界なので、ブラウザは受け取った ArrayBuffer を複写せずに `Float64Array(buf, offset, length)` で
  見て、そのまま Web Worker へ渡せる（transfer）。
- 実装: `fprobe/api/binary.py`（`BinaryResult`・`encode_frame`・`decode_frame`）。デーモンは結果が
  `BinaryResult` なら `send_bytes`。Python の利用者（`DaemonClient`）は区画を `result["data"]`（array）に入れる。
  画面は `host/ui/src/lib/timeline/frame.ts`。
- WebSocket でない利用者（GDB の monitor・フックの動作）はバイナリの応答を受け取れないので、`binary` のメソッドは
  `E_NOT_ALLOWED`。MCP の道具にも出さない。
- 別の HTTP の口（`GET /tiles`）にしなかった理由: 認証（トークン・Origin の検査）・利用者の識別・調停を WebSocket と
  二重に持つことになる。同じ接続なら hello 済みの利用者として扱える。

## 4. 間引きの定義（Python と TS で同じ）

視点 [v0, v1)（整数）を W 画素に分ける。画素 px の範囲:

- 波形: `p0 = v0 + ⌊px·span/W⌋`、`p1 = v0 + ⌈(px+1)·span/W⌉`（`la.Waveform.view`・`Recording.waveform_view`）。
  かたまりごとに `s0 = trunc((p0 − t0)/tps)`、`s1 = min(length, max(s0+1, ⌈(p1 − t0)/tps⌉))`、サンプル [s0, s1) の
  「s0 の値（s0 自身の変化を含む）」と「(s0, s1) に変化があれば x」。かたまりをまたぐ画素は、同じなら
  その値、違えば x、どのかたまりにも入らなければ「無し」。
- 出来事: `p0 = v0 + ⌊px·span/W⌋`、`p1 = v0 + ⌊(px+1)·span/W⌋`、t が [p0, p1) の件数（`Recording.density`）。
- 要約からの間引き（近似）: 波形は画素に重なる区間の状態を合わせる（同じならその値、違えば x、無しは除く）。
  件数は区間の始めが画素に入るものの和（区間の境が画素の境にそろえば正確）。

### 4.1 段ごとの間引き（format 2）

- 件数（密度）: 段の出来事の t で、出来事の件数と同じ境（`countsOf`）。要約からは段 × 区間の件数で同じ近似（`summaryRowCounts`）。
- 値の範囲（values）: 画素 [p0, p1)（p1 は波形と同じ切り上げ）に入るサンプルの値と、p0 の前の最後のサンプルの値
  （保たれている値）の最小・最大。タイルの外にはみ出す画素・値の無い画素は無し（`valueEnvelope`）。要約からは画素に
  重なる区間の最小・最大（保たれている値は分からないので含めない。`summaryEnvelope`）。
- 区間・箱・状態・印: 見えている出来事（`visibleRange`。始めの前から続く区間も、状態は視点の前の最後の状態も）が 2W 以下
  なら 1 つずつ画素に塗る（後に始まるもの = 入れ子の内側を上に。始めの画素に印、1 画素に 2 つなら斜線）。多ければ件数の
  密度。幅 24 画素以上の区間・箱にラベル（内側の入れ子は外側のラベルと重ねない）、次の印まで 48 画素以上の印にラベル。

参照の実装は `host/tests/timeline_ref.py`（Python）と `host/ui/src/lib/timeline/decimate.ts`（TS）。
突き合わせ:

- Python: タイル → 参照の間引き = `Waveform.view` / `waveform_view` / `density`（`test_timeline_tiles.py`、デーモン越しは
  `test_api_daemon.py`）。
- TS: Python が作った固定データ（`host/ui/tests/fixtures/decimate.json`。`test_timeline_tiles.py` が最新かを確かめる）で、
  TS の間引き = Python の `la.Waveform.view`・`Recording.waveform_view`・`Recording.density`（`decimate.test.ts`）。
  段は `fixtures/rows.json`（全体・一部（範囲の前の状態・値）・要約のタイル）で、段ごとの件数・値の範囲 = Python の
  `timeline_ref.row_density`・`value_envelope`（`rows.test.ts`）。
  偽の機器のデーモン越し（バイナリのフレーム → TS の間引き = `la.view`）も結合試験で。

## 5. 画面の構成

```
TimelinePanel.svelte（Timeline のパネル。捕捉の道具・情報（選択・基準・差）・次 / 前の出来事・段の一覧のメニュー）
 ├ TimelineMinimap.svelte（縮図: 全体のタイルの stream ごとの密度・異常の印・見ている範囲の枠。クリック / ドラッグで移動）
 ├ TimelineRows.svelte（段の名前の列: まとまりの見出し（折りたたみ・件数）・段の名前と色・マウスの位置の値・× で隠す・
 │   ドラッグで並べ替え）
 └ Timeline.svelte（時間軸の部品。入力・ホバーの説明・描画の機会ごとに 1 回の paint）
TimelineState（lib/timeline/state.ts。Svelte に依らない）
 │   視点（整数 [start, end)）・源（capture = 最後の捕捉 / recording = 読む記録）・最新のフレームと縮図。
 │   段の表（rows.ts の RowRegistry: タイルの見出しから知った段。源を変えるまで消さない）と構成（RowConfig: まとまりと
 │   段の並び・隠す・既定で隠す段を見せる・畳む。源ごとに localStorage の fprobe.timeline.rows.<源>）→ 描く段の並び。
 │   カーソル: 選択（クリック・N / P）・基準（R・Shift + クリック）。マウスの縦線は部品の中。選んだ出来事。
 │   タイルの計画: 持っているタイル（全体のタイル + 最大 3）で視点を描けなければ（範囲の外・要約が 1 画素より粗い）、
 │   視点を前後 1 画面ずつ広げた範囲を bin = 1 画素の半分で頼む。読みは同時に 1 つ、その間は持っているタイルで描く。
 │   全体のタイル（最初に読む全範囲）は縮図・範囲の外の問い合わせのために持ち続ける。
 │   誰か（AI も）が捕捉したら（activity）捨てて読み直し、新しい捕捉なら全体を表示する。
 ├ 間引きと問い合わせ（decimate.ts・compose.ts・query.ts）: Worker（worker.ts、decimator.ts の TileStore を
 │   WorkerDecimator が呼ぶ）で。Worker の無い環境（vitest）は同じ TileStore をその場で（InlineDecimator）。タイルの
 │   ArrayBuffer は Worker へ transfer。frame（描く段の並び RowSpec[] を渡す）・pick（時刻の出来事: ホバー・クリック）・
 │   seek（次 / 前の出来事）・anomalies（縮図の異常の印）。値の段の目盛りは見たタイルの範囲の和（タイルが変わっても跳ねない）。
 └ 描画（renderer.ts → render-gl.ts / render-2d.ts、overlay.ts、寸法と色は layout.ts、段の高さは rows.ts）
     WebGL2: 段 × 画素の RGBA8UI テクスチャ 1 枚（compose.ts の段の描き方ごとの意味）と、y → 段の R16UI・段 → (上端・
     高さ・描き方・色) の RGBA32F の小さなテクスチャで、画面全体の 1 枚の四角形をシェーダで塗る（GPU の作業はデータの量・
     段の数に依らない）。Canvas 2D（fillRect、同じ値の続く画素をまとめる）は WebGL2 が無いときの代わり（遅いことを表示）。
     目盛り・時刻・トリガ・カーソル 3 種と差・選んだ出来事の枠・ラベル・値の段の目盛りは Canvas 2D の重ね。
     寸法は CSS の画素で決め、間引きと描画はデバイスの画素（× devicePixelRatio）。色は app.css の変数（明暗に従う）と
     段の色 12 色（layout.ts の CATEGORICAL）。段の構成を変えた直後は、前のフレームを新しい並びに合わせて描く（alignFrame）。
```

- 段の型と描き方は §2.1・§4.1。まとまりの見出しは stream の件数の密度（畳んでも全体の動きが見える）。
- 操作（SystemView・Tracealyzer・Ozone の共通の型。UI/UX の調査の第 3 部）:
  - ホイール = 拡大縮小（マウスの位置を中心）、Shift + ホイール・横スクロール = 移動、ドラッグ = 移動。
  - クリック = 選択カーソル（出来事の上ならその出来事を選び、その始めへ。枠で示す）。Shift + クリック = 基準のカーソル。
  - ダブルクリック = 出来事の元へ（飛び先のある段: メモリ（番地）・ソース（PC））。出来事の無い所は全体。
  - ホバー = 説明（段・ラベル・種類・開始・期間・値と単位・基準からの差・飛び先）。
  - キーボード（時間軸にフォーカス）: ←/→ = 1/10 画面ずつ移動（Shift で 1 画面）、Ctrl + ←/→ または N / P = 次 / 前の
    出来事（出来事を選んでいればその段、無ければ見えている段のどれか。画面の外なら真ん中へ）、↑/↓ または +/− = 拡大縮小、
    Home/End = 端へ、0 または F = 全体、R = 基準を選択の位置に（もう一度で外す）、Enter = 出来事の元へ、Esc = 選択を外す。
- 他のパネルへの知らせ（events.ts、window の CustomEvent。受けるのは各パネル）: `fprobe:timeline-select`（選択カーソルが
  動いた: 時刻・秒・選んだ出来事）、`fprobe:timeline-open`（出来事の元を開く: kind = memory / source、番地）。

## 6. 性能の確かめ方

試作（`prototypes/ui-bench`）と同じデータ（16 ch・約 990 万変化点、8 段 × 12.5 万件。`make_tile.py` が timeline.tiles と
同じ形のフレームにする。生データ 92.6 MB）・同じ視点の列（慣らし 60 + 900 フレーム）・同じブラウザ（Playwright で
入っている Edge / Chrome、1600 × 820、倍率 1）で、本体の部品を測る（`host/ui/bench/`。`run_all.py` で一通り）。
部品は Timeline.svelte の paint() と TimelineState が使うものそのまま（decimator・renderer・overlay）。

- `worker`: 画面と同じ 2 段の流れ。描画の機会ごとに、Worker から届いた結果があれば描き、すぐ次の視点を Worker へ出す。
  「描画のスレッドの作業」= 描画（WebGL2 + 文字の重ね）だけ。
- `inline`: 試作と同じく、1 回の描画の機会の中で間引き + 描画（作業 = 両方）。
- `--vsync`: 垂直同期あり（画面 100 Hz での実際の滑らかさ）。それ以外は垂直同期を外す。
- `--spin`: 垂直同期を外した `worker` の測定で、新しい結果の無い描画の機会にも前の絵を描き直す。垂直同期を外した
  Chromium は、絵を出さなかった機会の後の描画の機会を約 18 ms 遅らせる（spin なしでは間隔が一律 18 ms になった。
  間引きの往復は p50 1.7 ms で、遅れはブラウザの描画の機会の側）。流れの実力は spin、実際は `--vsync` で見る。

## 7. 結果（2026-10-18、Ryzen 5 2600・Radeon RX 580・Windows 11・Edge / Chrome 155、表示 100 Hz）

| 測定 | 描画のスレッドの作業 p50 / p95 / p99 | 間引き p50 / p95 / p99 | フレーム間隔 p50 / p95 / p99 / max | 16.7 ms 超 | プロセスの最大 MB |
|---|---|---|---|---|---|
| **Edge・WebGL2・Worker（vsync）** | 0.2 / 0.3 / 0.4 | 1.3 / 7.1 / 9.8 | 10.0 / 10.3 / 19.8 / 20.3 | 1.0 % | 538 |
| **Edge・WebGL2・Worker（spin）** | 0.1 / 0.2 / 0.3 | 1.4 / 9.4 / 12.1 | 2.3 / 10.2 / 13.1 / 15.6 | 0.0 % | 548 |
| Chrome・WebGL2・Worker（spin） | 0.1 / 0.3 / 0.4 | 1.7 / 10.9 / 14.0 | 2.7 / 11.6 / 14.8 / 16.2 | 0.0 % | 625 |
| Edge・WebGL2・同じスレッド | 1.4 / 7.5 / 10.1 | 1.2 / 7.3 / 9.9 | 1.8 / 7.8 / 10.6 / 12.8 | 0.0 % | 554 |
| Edge・Canvas 2D・Worker（vsync） | 2.8 / 4.0 / 6.7 | 1.2 / 8.5 / 12.0 | 10.0 / 20.0 / 20.2 / 30.1 | 19.0 % | 571 |
| Edge・Canvas 2D・Worker（spin） | 2.2 / 3.5 / 4.6 | 1.1 / 6.8 / 9.4 | 9.1 / 22.7 / 32.0 / 39.8 | 15.4 % | 606 |
| Edge・Canvas 2D・同じスレッド | 3.6 / 8.6 / 11.6 | 1.0 / 6.5 / 9.3 | 7.4 / 15.6 / 25.3 / 32.0 | 3.7 % | 576 |
| （参考）試作 Edge・WebGL2 | 2.0 / 11.0 / 18.9（間引き込み） | — | 2.5 / 11.2 / 19.7 / 35.9 | 2.0 % | 538 |

- **本体の部品は 60 fps を保つ**: WebGL2 + Worker で、垂直同期なしの実力は p99 13.1 ms・最大 15.6 ms（16.7 ms を
  超えたフレーム 0）、垂直同期ありは 100 Hz の 10 ms に乗り、1 つ飛ばした（20 ms）のは 1.0 %（60 fps の 16.7 ms は
  超えない）。試作（p99 19.7・最大 35.9）より良い。
- 描画のスレッドの作業は 0.2〜0.4 ms（間引きは Worker）。拡大・移動の間も入力・他のビューの更新の余裕が大きい。
- 間引きは試作の JS（p95 9.8 ms）とほぼ同じ（p95 7〜11 ms）。整数の境を正確に求める分が増え、画素の境の配列を
  チャンネルで使い回す分が減った。
- Canvas 2D（WebGL2 が無い端末の代わり）は縮小した視点で 60 fps を割る（16.7 ms 超が 4〜19 %）。画面に
  「Canvas 2D で描いています（遅い）」と出す。
- デーモン側のタイルの作成（16 ch・400 万変化点の捕捉、純 Python）: 全体の要約 79 ms、1/50 の範囲の生データ
  11 ms + 符号化 2 ms（0.6 MB）、全体の生データ（上限を 500 万に上げた場合）603 ms + 67 ms（30.5 MB）。
  変化点の表（捕捉ごとに 1 回）は 1.2 s。取り直しは視点が大きく動いたときだけ。

### 7.1 段の多い時間軸（G01 の段の型すべて。2026-10-18、同じ PC・Edge 155）

`make_rows_tile.py`: 同じ波形 16 ch（約 990 万変化点）+ RTOS のタスク 8 段（12.5 万区間）+ 割り込み 4 段（10 万、1 割に
入れ子の深さ）+ 値 4 段（20 万サンプル）+ 解読の箱 2 段（12 万）+ まとまりの見出し 4 = 38 段、97.3 MB。同じ視点の列。

| 測定 | 描画のスレッドの作業 p50 / p95 / p99 | 間引き p50 / p95 / p99 | フレーム間隔 p50 / p95 / p99 / max | 16.7 ms 超 | プロセスの最大 MB |
|---|---|---|---|---|---|
| **Edge・WebGL2・Worker（vsync）** | 0.3 / 0.5 / 0.7 | 1.4 / 8.2 / 9.6 | 10.0 / 10.1 / 20.0 / 20.1 | 1.3 % | 568 |
| **Edge・WebGL2・Worker（spin）** | 0.3 / 0.6 / 0.8 | 2.0 / 12.0 / 15.2 | 3.5 / 13.3 / 16.0 / 19.4 | 0.8 % | 578 |
| Edge・WebGL2・同じスレッド | 1.7 / 8.3 / 11.3 | 1.4 / 7.9 / 11.0 | 2.1 / 8.5 / 11.9 / 15.8 | 0.0 % | 619 |
| Edge・Canvas 2D・Worker（vsync） | 14.3 / 20.3 / 23.1 | 1.6 / 8.3 / 10.2 | 20.0 / 30.1 / 40.0 / 50.0 | 90 % | 587 |
| （参考）段の少ない版 Edge・WebGL2・Worker（spin、差し替え後） | 0.2 / 0.4 / 0.6 | 1.6 / 8.8 / 13.1 | 2.7 / 9.7 / 13.9 / 15.8 | 0.0 % | 601 |

- WebGL2 は 38 段でも描画のスレッドの作業 p99 0.8 ms（段の数に依らない）。フレーム間隔の p95 は 10.1 ms（vsync）/ 13.3 ms
  （spin）で 16.7 ms 以内。20 ms（100 Hz の 1 つ飛ばし）は vsync で 1.3 %（段の少ない版 1.0 %）。
- Canvas 2D は段の型ごとの fillRect が増え、60 fps に届かない（WebGL2 の無い端末の代わり。遅いことを表示）。

## 8. 残り（段取り 4 以降）

- 画面の状態の API（`ui.state.get/set` と通知、MCP の道具、URL）は 2026-10-18 に足した（`host/fprobe/probe_ui_state.py`・
  `host/ui/src/lib/screen.ts`。視点・カーソル 3 種・開いている段・選んだ出来事・フレーム）。
- G01 の段・カーソル 3 種・縮図・次 / 前の出来事・段の構成・ホバー・選択と飛び先は 2026-10-18 に足した（§2.1・§4.1・§5）。
  残り: ほかの窓・AI のマウスのカーソルを時間軸に描く（今は共有の状態に載るだけ）、マウスの位置の値を値の段の名前の
  列にも出す（今は説明だけ）、目盛りの帯を縦にスクロールしても見えるように（今は段と一緒に流れる）、seek は持っている
  タイルの生の段の中だけ（要約の段・範囲の外は次のタイルを読むまで探せない）、捕捉の源では記録の段は出ない。
- 捕捉と記録を 1 つの時間軸に重ねる（今は表示の源を切り替える）。
- 記録中の記録の伸び（今は「読み直す」で範囲を取り直す）。
- タイルの先読み（視点の動きの向き）・Worker の中のタイルの合成（今は 1 フレーム = 1 タイル。足りない部分は空白）。
- 高い DPI・タブレット（タッチの拡大・移動）・macOS / Linux での測定。

