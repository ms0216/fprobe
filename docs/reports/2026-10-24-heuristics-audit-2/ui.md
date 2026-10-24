# ヒューリスティックの 2 回目の点検: 画面（host/ui/src）

- 日付: 2026-10-24
- 範囲: `host/ui/src` の `.ts` と `.svelte` のすべて（`*.test.ts` を除く）143 ファイル
- 方法: 検索で候補を拾わず、各ファイルを先頭から最後まで読んだ（1 回目の報告は読んでいない）
- 基準（CLAUDE.md）: fprobe 本体にヒューリスティックを入れない。結果は測ったことだけで決まる事実。経験則の閾値・仮定に頼る推定・
  それらによる判定や並べ替えを入れない。境界の値は使う側が明示する引数にし、暗黙の既定にしない。名前の慣習での判定も経験則。
  打ち切りは切ったことと使った上限を返す。画面は考えない層（API の応答を描くだけ）
- 分類: **H** = ヒューリスティック、**?** = 判断がつかない（理由を添える）

## まとめ

| 分類 | 件数 |
|---|---|
| H | 8 |
| ? | 24 |

画面の大部分は API の応答をそのまま描いている（Power の低下の閾値・System の窓・条件を数える N・Call Graph のベクタ表・
コア クロック・スタックの範囲は、使う人が入れるまで空で、暗黙の既定を持たない）。見つかったのは主に次の 4 種類:

1. **名前・文字の形での判定**（H 5 件・? 数件）: 拡張子でファイルの形式、接尾辞で RTOS のオブジェクトの種類、引数の名前で
   チャンネルの欄、文字の形でパスかデバイス名か、誤りの文の形で表示を消す。
2. **仮定した既定の値**（H 2 件・? 数件）: 申告が無いときに 100 MHz・10 チャンネル・ch 0〜7 = ピン を仮定、System の「結果」
   の欄が空なら最初の番地の changed を使う。
3. **画面の打ち切りを示さない**（? 1 件にまとめた）: API が `truncated` を返すのに出さない、または画面が上限で黙って切る。
4. **画面での集計**（?）: 分布図の棒の数 24・上位 10・上位 6 タスク＋その他・スキャン/秒を画面で計算。

---

## H（ヒューリスティック）

### H1. 誤りの文から「AI 向けの案内」と見える部分を名前の形で消す
- `host/ui/src/lib/api.ts:24-38`
```ts
export const TOOL_NAMESPACES = ["session", "ui", "probe", "target", ...];
const TOOL = new RegExp(`\\b(?:${TOOL_NAMESPACES.join("|")})_[a-z0-9_]+\\b`);
const PARAM = /\b[a-z][a-z0-9_]*=[^\s、。）]+/;
...
  .replace(/^(?:[A-Z][A-Za-z0-9_.]*(?:Error|Exception|Expired|Connected|Disconnected|Exit)|NoData): /, "");
  const plain = message.replace(/（[^（）]*）/g, (group) => (TOOL.test(group) || PARAM.test(group) ? "" : group));
  const sentences = plain.split("。").filter((s) => !TOOL.test(s) && !PARAM.test(s));
```
- なぜ: デーモンの誤りの文のうち「`名前空間_何か` を含む文」「`x=y` を含む文・括弧」を丸ごと落とす。どれが AI 向けの案内かを
  名前の慣習（`target_halt` の形・`=` の形）で推して判定している。事実（例「addr=0x2000_0000 で読めない」のような値を含む文、
  `foo_bar` を含む関数名の誤り）まで消えうる。全パネルの誤り表示（`errorText`）がこれを通る。

### H2. SWO の誤りを、文に `swo_configure` があるかで「誤りでない」とみなして出さない
- `host/ui/src/components/panels/SwoTracePanel.svelte:79-80`
```ts
const text = errorText(e);
if (!/swo_configure/.test(text)) error = text;               // まだ設定していないのは誤りにしない
```
- なぜ: 誤りの種類を文の中の語で判定している（誤りの符号でなく）。`swo_configure` を含む別の誤り（設定の失敗の案内等）も
  黙って消える。さらに H1 の `errorText` は `swo_configure` のような道具の名前を含む文を消すので、この判定が当たるかどうか
  自体が H1 の消し方に依存する。

### H3. System の「結果」の欄が空なら、最初のメモリの名前の `changed` を暗黙に使う
- `host/ui/src/components/panels/SystemPanel.svelte:353`（使う所 362、ボタンの可否 649・651、placeholder 644）
```ts
const effectText = $derived(effectExpr.trim() || (watches[0] ? `${termName(watches[0].name)} changed` : ""));
```
- なぜ: 解析（`recording.correlate`）の主の引数「結果」を、使う人が入れていないのに画面が決めて送る。表の並びの先頭という
  たまたまの順で決まる暗黙の既定。placeholder に出ているが、欄は空のまま「解析」「候補を自動探索」が押せる。

### H4. ファイルの形式を拡張子で決め、知らない拡張子は ELF とみなす
- `host/ui/src/lib/flash.ts:32-38`（使う所 51-63、`FlashPanel.svelte:60`）
```ts
/** ファイルの形式（拡張子で。デーモンは中身でも決める）。BIN は番地が要る */
export function formatOf(path: string): "elf" | "hex" | "bin" {
  ...
  return ext === "bin" ? "bin" : "elf";
}
```
- なぜ: 名前の慣習で形式を判定し、それで「番地の欄を出すか・`address` を送るか」を決めている。`.img`・`.raw` 等の生の
  バイナリは ELF とみなされて番地が送られない。拡張子の無いファイルも同じ。形式はデーモンが中身で決めるので、画面は形式の
  欄（または番地の欄を常に出す）にすべき所。

### H5. RTOS のオブジェクトの種類を `kind` の接尾辞で分類する
- `host/ui/src/components/panels/RtosObjectsPanel.svelte:58-64`
```ts
const familyOf = (k: string): Kind => (k.endsWith("queue") || k === "k_msgq" ? "queue"
  : k.endsWith("semaphore") || k === "k_sem" ? "semaphore" : k.endsWith("mutex") ? "mutex"
  : k.endsWith("timer") ? "timer" : k.endsWith("event_group") || k === "k_event" ? "event_group"
  : k.endsWith("buffer") ? "buffer" : "other");
```
- なぜ: 名前の慣習（`…queue`・`…mutex`）と特定の RTOS の型名（Zephyr の `k_msgq` 等）で種類を推して、タブの件数と絞り込みを
  決める。知らない RTOS の型は「その他」に入る。種類（family）はデーモンが返すべき事実。表示の絞り込みだけで値は変えない。

### H6. 解読器の設定の名前でチャンネルの欄かを判定する
- `host/ui/src/lib/logic.ts:28-31`（使う所 `DecoderFields.svelte:24`）
```ts
const CHANNEL_PARAMS = new Set(["channel", "scl", "sda", "sck", "mosi", "miso", "cs", "ws", "sd", "tck", "tms", "tdi",
                                "tdo", "trst", "swclk", "swdio", "clock", "enable"]);
export const isChannelParam = (name: string) => CHANNEL_PARAMS.has(name);
```
- なぜ: `la.decoders` の params の名前を見て、ch の選択にするか文字の欄にするかを決める。プラグイン等の解読器の線の名前
  （`rx`・`tx`・`dq0` 等）は文字の欄になり、`enable` という名前の非チャンネルの設定は ch の選択になる。設定がチャンネルかは
  `la.decoders` が型として返すべき事実。

### H7. FPGA 観測: プローブがシステムのクロックを申告しなければ 100 MHz と仮定し、機器へ送る半周期を計算する
- `host/ui/src/components/panels/FpgaObsPanel.svelte:30`（使う所 44・124、`lib/fpgaobs.ts:48-52`・`64-69`）
```ts
const sysHz = $derived(Number(caps?.sys_clock_hz) || 100e6);
...
user_ir: ir, half_period: tck!.half, ...
```
- なぜ: 申告が無いときの仮定の値で、機器に送る `half_period` と画面に出す「実際の TCK」を計算する。仮定が外れると TCK は
  違う速さで動き、画面の「実際 x MHz」も事実でなくなる。申告が無ければ TCK（MHz）の欄を使わず、半周期を直接入れさせるべき所。

### H8. Load SVD: 文字の形でパスかデバイス名かを決める
- `host/ui/src/components/shell/Dialogs.svelte:115-116`
```ts
const isPath = /[\\/]|\.svd$/i.test(svd);
const done = await app.act("target.svd.load", isPath ? { path: svd } : { device: svd });
```
- なぜ: `/`・`\` を含むか `.svd` で終わるかで、API のどの引数にするかを名前の形で決める。カレントの `STM32F401.SVD`（大文字・
  拡張子違い）や、`/` を含むデバイス名は逆に扱われる。欄を分けるか選択にするべき所。

---

## ?（判断がつかない）

### ?1. 「hook <id>」の利用者名で役割を「hook」に分ける
- `host/ui/src/lib/activity.ts:12-15`
```ts
return e.client.startsWith("hook ") ? "hook" : e.role;
```
- 理由: 名前の形での判定だが、利用者名はデーモンが付ける決まった形（フックの動作）。デーモンが `role` か別の欄で事実として
  返していないので画面で分けている。決まった約束なら H でないが、名前の慣習に頼る点は同じ。

### ?2. 古いデーモンの判定・「まだ無い」の判定を誤りの文の形で行う
- `host/ui/src/lib/terminal.ts:49-51`
```ts
const legacy = (e: unknown) => /terminal\.\w+ は無い|-32601|not found/i.test(String((e as Error)?.message ?? e));
const noTerminals = (e: unknown) => /terminals/.test(String((e as Error)?.message ?? e));
```
- `host/ui/src/lib/timeline/state.ts:537-540`
```ts
if (!/捕捉が無い|読む記録が無い|記録が空/.test(message)) this.set({ error: message });
```
- 理由: 誤りの種類を文の語で判定して、経路の切り替え・誤りの非表示を決めている（H2 と同じ型）。古いデーモンとの互換の分岐で、
  符号（`E_NO_DATA` 等）が使えない時期のものかもしれない。文が変わると黙って経路・表示が変わる。

### ?3. 場所の文字（`func+0x4 (file.c:12)`）から正規表現でファイルと行を拾う
- `host/ui/src/lib/debugger.ts:86-89`（使う所 95・442。`showAddress` は常にこれで行を決める）
```ts
const m = /([\w.\-/\\]+\.[A-Za-z]+):(\d+)/.exec(where);
```
- 理由: 人向けの文の形に頼る。`debug.symbol.info` が `file`・`line` を構造として返していればそれを使うべき。返していないなら
  API 側の欠け。

### ?4. Memory の番地の欄の解き方の順（式 → シンボル → 16 進の推し）
- `host/ui/src/lib/memory.ts:10-33`
```ts
if (/^\d+$/.test(s)) return parseInt(s, 16);
const bareHex = /^[0-9a-f]+$/i.test(s) ? parseInt(s, 16) : null;     // 2000A0FC。同じ名前の変数があれば変数
... if (v && typeof v === "object" && typeof v.pointer === "number") return v.pointer;
... if (bareHex !== null) return bareHex;
```
- 理由: 曖昧な入力（`abc` は変数か 16 進か、ポインタの式は指す先かポインタ自身の番地か）を決まった順で解く。説明（tip）に
  書いてあり規則としては一貫しているが、利用者の意図を推して選んでいる面がある。

### ?5. 解読器の設定の文字から型を推す
- `host/ui/src/lib/logic.ts:51-61`
```ts
if (new RegExp(`^${int}(\\s*,\\s*${int})+$`, "i").test(s)) return s.split(",").map((x) => parseNumber(x));
if (new RegExp(`^${int}$`, "i").test(s)) return parseNumber(s);
```
- 理由: 文字の設定（例 `"1,2"`・`"0x10"` を文字のまま渡したい設定）が数・数の並びに変わる。`la.decoders` が設定の型を
  返していれば型で決めるべき。返していないなら画面の推しは避けられない。

### ?6. 申告が無いときの仮定（ロジアナの ch の数と並び・電圧の測定の可否・JTAG の IR 長）
- `host/ui/src/components/panels/LogicAnalyzerPanel.svelte:42` `la_channels ?? 10`、`:179-180` `emptyPattern(10)`
- `host/ui/src/lib/waveform.ts:71-75` `legacyLayout()`: ch 0〜7 = ピン、8・9 = 内部
- `host/ui/src/components/panels/PowerPanel.svelte:42` `capabilities ? Boolean(capabilities.aux) : true`
- `host/ui/src/lib/fpgaobs.ts:66` `irLen: chain.ir_len ?? 4`（開いた設定に chain が無いとき欄に 4 を出す）
- 理由: どれも古いデバイス・申告の無い構成のための既定。表示の名前・欄の数に効くだけで測った値は変えないが、
  「申告に従う」原則（CLAUDE.md の移植性）から外れる仮定。H7 ほど影響は大きくない。

### ?7. System の「波形を捕捉」の暗黙の捕捉の設定と、時刻の換算の仮定
- `host/ui/src/components/panels/SystemPanel.svelte:83-86`
```ts
const done = await tl.capture({ rate_hz: Number(caps.la_sample_hz ?? 100e6) / 4, pretrigger: Math.floor(depth / 2),
                                deep: !depth, crossOnly: true, rle: false });
```
- 同 `:90` `Number(caps.timebase_hz ?? 100e6) / 1000`（前後 1 ms の視点）、`:373` `correlation?.timebase_hz ?? 100e6`
- 理由: 速さ（最高の 1/4）・プリトリガ（深さの半分）を画面が決めて送る暗黙の既定。申告が無ければ 100 MHz を仮定。
  捕捉の結果（何を測ったか）がこの既定で決まる。視点の換算は表示だけ。

### ?8. 捕捉の既定の設定（メニュー・Logic Analyzer の「キャプチャ」）
- `host/ui/src/lib/commands.ts:105-106` `tl.capture({ rate_hz: null, pretrigger: 1024, deep: false })`
- `host/ui/src/components/panels/LogicAnalyzerPanel.svelte:164` `rate_hz: Number(load("la.rate", "25000000")), pretrigger: Number(load("la.pretrigger", "1024"))`
- `host/ui/src/components/panels/TimelinePanel.svelte:36-38` 覚えた速さが選択肢に無ければ `rates[Math.min(2, rates.length - 1)]`（3 番目）を選ぶ
- 理由: Timeline のパネルでは欄に出ているが、メニューと Logic Analyzer のボタンは欄を見せずに既定・前の値で捕捉する。
  3 番目の速さを選ぶのも理由の無い選択（欄には出る）。

### ?9. 対象の品種に偏った既定の値を欄に入れる
- `host/ui/src/components/panels/FlashPanel.svelte:26,29,30` `"0x08000000"`・`"16K"`
- `host/ui/src/components/panels/MemoryPanel.svelte:23` `"0x20000000"`
- `host/ui/src/components/shell/Dialogs.svelte:16,18` `"stm32f401xe"`・`"STM32F401"`
- 理由: 欄に見えていて利用者が送る前に確かめられるので「暗黙」ではない。ただし STM32 / Cortex-M の SRAM という特定の対象の
  経験則の値で、別のターゲット（nRF・RP2040）では誤った番地を消す・読むきっかけになる（消去は確認がある）。

### ?10. 画面での打ち切りを示さない（API が `truncated` を返すのに出さない・画面が黙って切る）
- API の打ち切りを捨てる:
  - `host/ui/src/lib/interrupts.ts:79-83` `limit: 100000` → `truncated` を返すが、`InterruptsPanel.svelte` は出さない
  - `host/ui/src/lib/activity.ts:60-68` `MAX_CALLS = 100_000` で `recording.query`、`truncated` を見ない
  - `host/ui/src/components/panels/ProfilePanel.svelte:24,91,103,108-109,118-119` `top: 500 / 1000 / 10000 / 100000`、切れたことを出さない
  - `host/ui/src/components/panels/CallGraphPanel.svelte:48,63` `top: 100000`・`top: 10000`
  - `host/ui/src/components/panels/EventLogPanel.svelte:120` `SELECT DISTINCT stream ... limit: 100`（検索の stream の選択肢）
- 画面が自分で切る:
  - `host/ui/src/components/panels/EventLogPanel.svelte:215-216` 棒の中の区間を `.slice(0, 500)`（見出しは棒の件数を出すので
    数は分かるが、切ったとは言わない）
  - `host/ui/src/components/charts/Chart.svelte:37-41` 散布図は新しい `maxPoints`（3000）点だけ
  - `host/ui/src/lib/sampling.ts:70` `STORE_LIMIT = 50_000`（グラフの点を古い方から捨てる。統計は全体）
  - `host/ui/src/lib/breakpoints.ts:103` `KEEP = 2000`、`lib/linux.ts:263`・`lib/logview.ts:35` `KEEP = 5000`、
    `lib/terminal.ts:188` 文字 `200_000`
- 理由: 表示を軽くするための上限で値は変えない。ただし「打ち切りは切ったことと使った上限を返す」に対し、画面がそれを
  利用者に伝えていない所がある（Power・Event Log の検索・CPU Load・Decode・Heap・相関は伝えている）。

### ?11. 分布図の棒の数・上位の数を画面が決める
- `host/ui/src/components/panels/EventLogPanel.svelte:202` `timing.measure` に `bins: 24` を暗黙に渡す
- `host/ui/src/lib/interrupts.ts:103-121` 画面で 24 本に分け、整数で幅が狭ければ 1 値 1 本、長い順の上位 `LONGEST = 10` を含む棒に印
- 理由: 測った回の数・長さ自体は変えないが、分布の見え方（ピークが分かれる・まとまる）は棒の数で決まり、それを利用者が選べない。
  上位 10 は順位の事実だが、10 という数は暗黙の境界。

### ?12. CPU Load の積み上げで上位 6 タスク以外を「その他」にまとめる
- `host/ui/src/lib/rtos.ts:205-211`・`218-230`
```ts
export function busyTasks(series: LoadSeries, max = 6): string[] { ... .slice(0, max) ... }
```
- 理由: 表には全タスクが出るので事実は欠けないが、グラフでは 7 位以下が名前を失う。6 は暗黙の境界。

### ?13. スキャン/秒を画面で計算する（32 ビットの巻き戻りを仮定）
- `host/ui/src/components/panels/SystemPanel.svelte:168-171`
```ts
scansPerSecond = prevScans && s.time_us > prevScans.t_us
  ? Math.round(((s.fpga.scans - prevScans.scans + 2 ** 32) % 2 ** 32) / ((s.time_us - prevScans.t_us) / 1e6)) : null;
```
- 理由: 2 回の読みの差から画面が率を作る（同じパネルの冒頭のコメントは「画面では…毎秒を計算しない」）。カウンタが 32 ビットで
  1 周期の間に 1 回までしか巻かないという仮定。値は事実の差だが、画面が考えている。

### ?14. 「時間軸で表示」で、変化の無いピンを暗黙に隠す
- `host/ui/src/components/panels/SystemPanel.svelte:319-325`、`host/ui/src/lib/system.ts:111-118`、`lib/timeline/rows.ts:187-195`
- 理由: 縮図（幅 400 の `recording.overview`）で全体が同じ値の ch を隠す。縮図の画素の中の変化は `x` になるので見落としは無いはずだが、
  利用者が頼んでいない段の非表示（設定から戻せる）。

### ?15. 故障の見出しの「主な原因」から FORCED と *VALID を外す
- `host/ui/src/lib/fault.ts:91-97`
```ts
const main = report.causes.filter((c) => !VALID_BITS.has(c.bit) && c.bit !== "FORCED").map((c) => c.bit);
```
- 理由: どのビットが「主」かを画面が決めている。アーキテクチャの定義（FORCED は格上げの印、*VALID は番地が有効の印）に基づくので
  経験則とまでは言えないが、判定ではある。表には全ビットが出る。

### ?16. タイムベースが分からないとき 1 で割る（時刻の単位のまま秒として出す）
- `host/ui/src/lib/interrupts.ts:81-83` `const [t0, t1, hz] = s.rows?.[0] ?? [0, 0, 1]; ... hz || 1`
- 同 `:88` `const hz = r.clock_hz || 1;`（コメントは「クロック数のまま」だが、パネルは `formatDuration` で秒として出す）
- 理由: 仮定の推定ではないが、分からない値を 1 として秒の表示に出すので、事実と違う単位で見える。

### ?17. デーモンの 1 ページの件数を仮定して読み足しを止める
- `host/ui/src/components/panels/PowerPanel.svelte:106-114` `if (got.length < 1000) break;`（ほかに上限 20 ページ。切ったことは出す）
- `host/ui/src/lib/logview.ts:37,84` `LIMIT = 200` 「デーモンの既定と同じ」で `< LIMIT` なら止める
- 理由: API の既定の件数を画面が覚えて、それより少なければ終わりとみなす。API が `next`・`more` を返していればそれで決めるべき。

### ?18. Data Sampling の `watch.series` の読む件数を周期から決める
- `host/ui/src/components/panels/DataSamplingPanel.svelte:146` `const last = Math.min(4096, Math.max(64, Math.ceil(rate)));`
- 理由: 300 ms ごとに約 1 秒分を読む。読みが遅れて 1 秒を超えると間の点が抜ける（統計にも入らない）が、抜けたことは出ない。
  境界（64・4096・1 秒分）は画面の暗黙の値。

### ?19. SWO の ITM のポートを 0〜3 に固定して設定する
- `host/ui/src/components/panels/TerminalPanel.svelte:185-186`
```ts
? await app.act("swo.configure", { ports: [0, 1, 2, 3], swo_baud: swoBaud, core_clock_hz: Number(coreClock) || null })
```
- 理由: 説明（tip）に書いてあるが、利用者が選べない引数。経験則の「よく使うポート」に近い。

### ?20. 故障の番地の場所の名前を「0x で始まるか」で出し分ける
- `host/ui/src/components/panels/FaultPanel.svelte:178`、同型 `RtosObjectsPanel.svelte:156` `o.name.startsWith("0x")`
- 理由: 文字の形で「名前が引けたか」を判定している。表示だけで、引けたかは API が欄で返すべき事実。

### ?21. 旧来の RTT の経路でチャンネル 0 だけ書けるとみなす
- `host/ui/src/lib/terminal.ts:63-64` `writable: c.index === 0`
- 理由: `terminal.*` の無い古いデーモンへの互換の経路の仮定。

### ?22. 画面の文言がデーモンの順位を「best」と呼ぶ
- `host/ui/src/lib/i18n.ts:544` `"system.autoInvestigateTip": "... and show the best 20"`（日本語 2319 は「上位 20 件」）
- 理由: 並べ替え自体はデーモン（`recording.correlate`）の仕事で範囲外だが、英語の文言が「最良」という判定の語で示している。
  何で並べた上位かを書くべき。

### ?23. 「FPGA の条件（回/秒）」の見出しと、実際の「最後の N 件の回数」の食い違い
- `host/ui/src/lib/i18n.ts:414`（日本語 2189）`"system.conditions": "FPGA conditions (per second)"`、同 `509`・`511`・`2284`・`2286` の「頻度・回/秒」
- 理由: 画面は `fpga_obs.count` の「最後の N 件で成立した回数」をそのまま出している（毎秒を計算しない）のに、見出しは毎秒と言う。
  判定ではないが、測ったことと違う量を名乗っている。

### ?24. 時間軸の印の線を 50 件まで・縮図の異常の印を 2000 件に間引く
- `host/ui/src/lib/timeline/overlay.ts:39`・`226`（視点の始めより 64 件より前に始まった区間の印は描かない）、`lib/timeline/query.ts:85-96`
- 理由: 描画の都合の上限。値は変えないが、縮図の異常の印は 2000 を超えると等間隔に間引かれ、間引いたことは出ない。

---

## 読んだファイル（143。すべて先頭から最後まで読んだ）

- `host/ui/src/App.svelte`（146 行・全部読んだ）
- `host/ui/src/components/Icon.svelte`（35 行・全部読んだ）
- `host/ui/src/components/Timeline.svelte`（278 行・全部読んだ）
- `host/ui/src/components/TimelineMinimap.svelte`（99 行・全部読んだ）
- `host/ui/src/components/TimelineRows.svelte`（98 行・全部読んだ）
- `host/ui/src/components/charts/Chart.svelte`（358 行・全部読んだ）
- `host/ui/src/components/panels/ActivityPanel.svelte`（219 行・全部読んだ）
- `host/ui/src/components/panels/ApiPanel.svelte`（221 行・全部読んだ）
- `host/ui/src/components/panels/BreakpointsPanel.svelte`（383 行・全部読んだ）
- `host/ui/src/components/panels/CallGraphPanel.svelte`（198 行・全部読んだ）
- `host/ui/src/components/panels/CallStackPanel.svelte`（57 行・全部読んだ）
- `host/ui/src/components/panels/CodeAtNote.svelte`（69 行・全部読んだ）
- `host/ui/src/components/panels/CodeAtToggle.svelte`（23 行・全部読んだ）
- `host/ui/src/components/panels/ConditionFields.svelte`（43 行・全部読んだ）
- `host/ui/src/components/panels/CpuLoadPanel.svelte`（244 行・全部読んだ）
- `host/ui/src/components/panels/DashboardPanel.svelte`（401 行・全部読んだ）
- `host/ui/src/components/panels/DataSamplingPanel.svelte`（610 行・全部読んだ）
- `host/ui/src/components/panels/DecoderFields.svelte`（55 行・全部読んだ）
- `host/ui/src/components/panels/DisassemblyPanel.svelte`（141 行・全部読んだ）
- `host/ui/src/components/panels/EditValue.svelte`（63 行・全部読んだ）
- `host/ui/src/components/panels/Empty.svelte`（42 行・全部読んだ）
- `host/ui/src/components/panels/EventLogPanel.svelte`（508 行・全部読んだ）
- `host/ui/src/components/panels/FaultPanel.svelte`（335 行・全部読んだ）
- `host/ui/src/components/panels/FavoritesPanel.svelte`（90 行・全部読んだ）
- `host/ui/src/components/panels/FlashPanel.svelte`（621 行・全部読んだ）
- `host/ui/src/components/panels/FpgaObsPanel.svelte`（328 行・全部読んだ）
- `host/ui/src/components/panels/HeapPanel.svelte`（150 行・全部読んだ）
- `host/ui/src/components/panels/InterruptsPanel.svelte`（160 行・全部読んだ）
- `host/ui/src/components/panels/LinuxPanel.svelte`（375 行・全部読んだ）
- `host/ui/src/components/panels/LoadState.svelte`（28 行・全部読んだ）
- `host/ui/src/components/panels/LocalsPanel.svelte`（32 行・全部読んだ）
- `host/ui/src/components/panels/LogPanel.svelte`（127 行・全部読んだ）
- `host/ui/src/components/panels/LogicAnalyzerPanel.svelte`（641 行・全部読んだ）
- `host/ui/src/components/panels/LowLevelPanel.svelte`（357 行・全部読んだ）
- `host/ui/src/components/panels/MemoryPanel.svelte`（256 行・全部読んだ）
- `host/ui/src/components/panels/PeripheralsPanel.svelte`（244 行・全部読んだ）
- `host/ui/src/components/panels/PowerPanel.svelte`（244 行・全部読んだ）
- `host/ui/src/components/panels/ProfilePanel.svelte`（269 行・全部読んだ）
- `host/ui/src/components/panels/RecordingsPanel.svelte`（338 行・全部読んだ）
- `host/ui/src/components/panels/RegistersPanel.svelte`（95 行・全部読んだ）
- `host/ui/src/components/panels/RtosObjectsPanel.svelte`（195 行・全部読んだ）
- `host/ui/src/components/panels/SafetyConditions.svelte`（131 行・全部読んだ）
- `host/ui/src/components/panels/SamplerNotice.svelte`（60 行・全部読んだ）
- `host/ui/src/components/panels/SourcePanel.svelte`（316 行・全部読んだ）
- `host/ui/src/components/panels/SwoTracePanel.svelte`（199 行・全部読んだ）
- `host/ui/src/components/panels/SystemPanel.svelte`（828 行・全部読んだ）
- `host/ui/src/components/panels/TabBar.svelte`（42 行・全部読んだ）
- `host/ui/src/components/panels/TerminalPanel.svelte`（379 行・全部読んだ）
- `host/ui/src/components/panels/ThreadsPanel.svelte`（276 行・全部読んだ）
- `host/ui/src/components/panels/TimeLink.svelte`（41 行・全部読んだ）
- `host/ui/src/components/panels/TimelinePanel.svelte`（202 行・全部読んだ）
- `host/ui/src/components/panels/VarRow.svelte`（165 行・全部読んだ）
- `host/ui/src/components/panels/VarTree.svelte`（37 行・全部読んだ）
- `host/ui/src/components/panels/WatchPanel.svelte`（59 行・全部読んだ）
- `host/ui/src/components/panels/format.ts`（12 行・全部読んだ）
- `host/ui/src/components/panels/registry.ts`（81 行・全部読んだ）
- `host/ui/src/components/shell/ConfirmDialog.svelte`（46 行・全部読んだ）
- `host/ui/src/components/shell/DebugToolbar.svelte`（61 行・全部読んだ）
- `host/ui/src/components/shell/Dialogs.svelte`（241 行・全部読んだ）
- `host/ui/src/components/shell/DockArea.svelte`（29 行・全部読んだ）
- `host/ui/src/components/shell/MenuBar.svelte`（121 行・全部読んだ）
- `host/ui/src/components/shell/SafetyBar.svelte`（73 行・全部読んだ）
- `host/ui/src/components/shell/StatusLine.svelte`（73 行・全部読んだ）
- `host/ui/src/lib/activity.ts`（72 行・全部読んだ）
- `host/ui/src/lib/api.ts`（176 行・全部読んだ）
- `host/ui/src/lib/apicall.ts`（32 行・全部読んだ）
- `host/ui/src/lib/apiform.ts`（243 行・全部読んだ）
- `host/ui/src/lib/apispec.ts`（9 行・全部読んだ）
- `host/ui/src/lib/breakpoints.ts`（203 行・全部読んだ）
- `host/ui/src/lib/callgraph.ts`（116 行・全部読んだ）
- `host/ui/src/lib/chart.ts`（146 行・全部読んだ）
- `host/ui/src/lib/codeat.ts`（243 行・全部読んだ）
- `host/ui/src/lib/commands.ts`（135 行・全部読んだ）
- `host/ui/src/lib/confirm.ts`（52 行・全部読んだ）
- `host/ui/src/lib/context.ts`（30 行・全部読んだ）
- `host/ui/src/lib/cycles.ts`（19 行・全部読んだ）
- `host/ui/src/lib/dashboard.ts`（337 行・全部読んだ）
- `host/ui/src/lib/debugger.ts`（566 行・全部読んだ）
- `host/ui/src/lib/diagnose.ts`（53 行・全部読んだ）
- `host/ui/src/lib/dock.ts`（201 行・全部読んだ）
- `host/ui/src/lib/dockwindows.ts`（73 行・全部読んだ）
- `host/ui/src/lib/etm.ts`（95 行・全部読んだ）
- `host/ui/src/lib/eventlog.ts`（207 行・全部読んだ）
- `host/ui/src/lib/execution.ts`（62 行・全部読んだ）
- `host/ui/src/lib/fault.ts`（114 行・全部読んだ）
- `host/ui/src/lib/favorites.ts`（98 行・全部読んだ）
- `host/ui/src/lib/flash.ts`（178 行・全部読んだ）
- `host/ui/src/lib/format.ts`（38 行・全部読んだ）
- `host/ui/src/lib/fpgaobs.ts`（120 行・全部読んだ）
- `host/ui/src/lib/guard.ts`（10 行・全部読んだ）
- `host/ui/src/lib/heap.ts`（33 行・全部読んだ）
- `host/ui/src/lib/i18n.ts`（3620 行・全部読んだ）
- `host/ui/src/lib/interrupts.ts`（128 行・全部読んだ）
- `host/ui/src/lib/linux.ts`（284 行・全部読んだ）
- `host/ui/src/lib/logic.ts`（244 行・全部読んだ）
- `host/ui/src/lib/logview.ts`（87 行・全部読んだ）
- `host/ui/src/lib/lowlevel.ts`（163 行・全部読んだ）
- `host/ui/src/lib/memory.ts`（73 行・全部読んだ）
- `host/ui/src/lib/notify.ts`（36 行・全部読んだ）
- `host/ui/src/lib/peripherals.ts`（53 行・全部読んだ）
- `host/ui/src/lib/poll.ts`（108 行・全部読んだ）
- `host/ui/src/lib/power.ts`（145 行・全部読んだ）
- `host/ui/src/lib/prefs.ts`（16 行・全部読んだ）
- `host/ui/src/lib/profile.ts`（123 行・全部読んだ）
- `host/ui/src/lib/protection.ts`（17 行・全部読んだ）
- `host/ui/src/lib/recordings.ts`（39 行・全部読んだ）
- `host/ui/src/lib/rtos.ts`（277 行・全部読んだ）
- `host/ui/src/lib/safety.ts`（251 行・全部読んだ）
- `host/ui/src/lib/sampler.ts`（56 行・全部読んだ）
- `host/ui/src/lib/sampling.ts`（409 行・全部読んだ）
- `host/ui/src/lib/screen.ts`（381 行・全部読んだ）
- `host/ui/src/lib/snapshots.ts`（40 行・全部読んだ）
- `host/ui/src/lib/source.ts`（28 行・全部読んだ）
- `host/ui/src/lib/stack.ts`（53 行・全部読んだ）
- `host/ui/src/lib/state.ts`（165 行・全部読んだ）
- `host/ui/src/lib/swotrace.ts`（50 行・全部読んだ）
- `host/ui/src/lib/system.ts`（240 行・全部読んだ）
- `host/ui/src/lib/target.ts`（45 行・全部読んだ）
- `host/ui/src/lib/terminal.ts`（327 行・全部読んだ）
- `host/ui/src/lib/time.ts`（112 行・全部読んだ）
- `host/ui/src/lib/timeline/compose.ts`（291 行・全部読んだ）
- `host/ui/src/lib/timeline/cursor.ts`（60 行・全部読んだ）
- `host/ui/src/lib/timeline/decimate.ts`（255 行・全部読んだ）
- `host/ui/src/lib/timeline/decimator.ts`（205 行・全部読んだ）
- `host/ui/src/lib/timeline/events.ts`（61 行・全部読んだ）
- `host/ui/src/lib/timeline/frame.ts`（36 行・全部読んだ）
- `host/ui/src/lib/timeline/layout.ts`（105 行・全部読んだ）
- `host/ui/src/lib/timeline/overlay.ts`（282 行・全部読んだ）
- `host/ui/src/lib/timeline/query.ts`（97 行・全部読んだ）
- `host/ui/src/lib/timeline/render-2d.ts`（145 行・全部読んだ）
- `host/ui/src/lib/timeline/render-gl.ts`（235 行・全部読んだ）
- `host/ui/src/lib/timeline/renderer.ts`（20 行・全部読んだ）
- `host/ui/src/lib/timeline/rows.ts`（220 行・全部読んだ）
- `host/ui/src/lib/timeline/state.ts`（704 行・全部読んだ）
- `host/ui/src/lib/timeline/tile.ts`（228 行・全部読んだ）
- `host/ui/src/lib/timeline/titles.ts`（28 行・全部読んだ）
- `host/ui/src/lib/timeline/worker.ts`（20 行・全部読んだ）
- `host/ui/src/lib/ui.ts`（89 行・全部読んだ）
- `host/ui/src/lib/valuewrite.ts`（141 行・全部読んだ）
- `host/ui/src/lib/vartree.ts`（223 行・全部読んだ）
- `host/ui/src/lib/watchshare.ts`（72 行・全部読んだ）
- `host/ui/src/lib/waveform.ts`（125 行・全部読んだ）
- `host/ui/src/main.ts`（5 行・全部読んだ）
