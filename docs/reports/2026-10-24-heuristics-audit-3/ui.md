# ヒューリスティックの 3 回目の点検: 画面（host/ui/src）

- 範囲: `host/ui/src` の `.ts` と `.svelte` すべて（`*.test.ts` を除く）。152 ファイル・30,257 行。
- 方法: 検索で候補を拾わず、各ファイルを先頭から最後まで読んだ（一覧は末尾）。前の点検の報告と台帳は読んでいない。
- 基準（CLAUDE.md と課題の文）: 本体は測ったことだけを出す。経験則の閾値・仮定に頼る推定・それらによる判定や並べ替えを
  入れない。境界・既定の値は使う側が明示する引数にし、暗黙の既定にしない。打ち切りは切ったことと上限を返す。画面は
  考えない層（API の応答を描くだけ。計算・判定・暗黙の既定を持たない）。
- 分類: **H** = ヒューリスティック・暗黙の既定・黙った打ち切り・画面での判定。**?** = 判断がつかない（理由を書く）。

---

## H（ヒューリスティック）

### H1. XY の散布図が、同時に測っていない 2 つの値を組にする（保持の仮定）

`host/ui/src/lib/sampling.ts:262-276`

```ts
/** XY: a の各時刻に、その時刻までの b の最新の値を組む（サンプル・アンド・ホールド）。数でない組は除く */
export function pairXY(a, b) {
  ...
    while (j + 1 < b.x.length && b.x[j + 1] <= a.x[i]) j++;
    if (j < 0) continue;
    const x = a.y[i], y = b.y[j];
```

- なぜ: X の標本ごとに「それ以前で最新の Y の標本」を組む。2 つの値の時刻の差は描かず、点は同時に測ったように見える。
  「標本の間は値が変わらない」という仮定に頼る推定。文言も事実と違う: `i18n.ts:693`
  `"each X sample paired with Y at that moment"`、`i18n.ts:2632` 「X の各時刻における Y の値を組み合わせて」。
  使う側（DataSamplingPanel.svelte:274-277）はこの組をそのまま散布図にする。

### H2. メモリの窓が、誤りの文の長さで「アドレスが不明」と言い換える

`host/ui/src/components/panels/MemoryPanel.svelte:208`

```svelte
{#if error}<span class="bad" title={error}>{error.length > 60 ? $i18n("memory.badAddress", { address: input }) : error}</span>{/if}
```

- なぜ: 60 文字という経験則の閾値で、読めなかった理由を「Unknown address: …」（不明なアドレス）に置き換える。
  60 文字を超える誤りは接続断・ACK の誤り・範囲外など何でもありうるのに、画面が原因を決めて見せる（本当の文は
  ツールチップだけ）。

### H3. 0x10000 以上の整数を「番地・語の値」とみなして 16 進で出す

`host/ui/src/lib/verify.ts:306-310`、`host/ui/src/lib/crosstrigger.ts:249-253`

```ts
/** 値の短い表示: 0x10000 以上の整数は 16 進（番地・語の値）。文字はそのまま、ほかは JSON */
export function valueText(v: unknown): string {
  if (typeof v === "number" && Number.isInteger(v) && v >= 0x10000) return `0x${v.toString(16).toUpperCase()}`;
```

- なぜ: 値の大きさという経験則で意味（番地か）を推して書式を変える。verify.ts の方は規則の検査の **実測・期待値**
  （`pairs` → VerifyRules.svelte:116-117）、故障注入の checks の value・expected（`checkText`）、フックの結果の値
  （VerifyHooks.svelte:177）、違反の値に使われ、例えば 120000 µs や 70000 回が `0x1D4C0`・`0x11170` と出る。
  crosstrigger.ts の方は欄の既定の値の表示（ApiFields.svelte）と、読み戻した設定を欄へ入れる値（`formValues`）。

### H4. Dashboard の Live が、観測の周期を画面の暗黙の既定（100 Hz）で始める

`host/ui/src/components/panels/DashboardPanel.svelte:31`・`:141`

```ts
const POLL_MS = 300, STATUS_MS = 1000, LIVE_RATE = 100;
...
live = await startWatch(call, act, OWNER, exprs, now.rate || LIVE_RATE);
```

- なぜ: 観測が動いていなければ、使う人に聞かず 100 Hz で `watch.set` する。周期は時刻の分解能と取りこぼしを左右する
  測定の条件で、欄にも出ない暗黙の既定。

### H5. CPU Load が最大の窓を「警告」の色で時間軸に置く

`host/ui/src/components/panels/CpuLoadPanel.svelte:137-139`

```ts
tl.setMarks("cpuLoad", "recording", peak ? [{ t: peak.t0, tEnd: peak.t1, tone: 3,
                                             label: $i18n("cpuLoad.peak", { load: percent(peak.load) }) }] : []);
```

- なぜ: 最大は事実だが、tone 3（warn = 黄）で印を置くのは「異常・注意」という判定の色。同じ画面の Event Log・
  Interrupts は長い順の上位を「普通の色: 異常とは言わない」（eventlog.ts:160-163、InterruptsPanel.svelte:60-63）と
  明記して tone 0 にしており、ここだけ判定の色が付く。

### H6. 黙った打ち切り（切ったことも上限も出さない）

1. `host/ui/src/lib/rtos.ts:105`
   ```ts
   const stats = await conn.call("recording.query", { sql: TASK_STATS_SQL, limit: 10000 }, quiet);
   ```
   タスクごとの集計の行を 10000 で切るが、`truncated` を見ない・返さない。CPU Load の表（使用率の分母 `covered` も）が
   黙って欠けうる。
2. `host/ui/src/components/panels/EventLogPanel.svelte:241`
   ```ts
   const out = await conn.call("recording.query", { ...q, limit: 1000 }, { quiet: true });
   ```
   記録の概要（stream・kind ごとの件数）を 1000 行で切るが、`partial` だけを見て `truncated` を出さない。
   切ったときは件数の合計・割合（`kindStats`）も欠けた行の上で計算される。

---

## ?（判断がつかない）

### ?1. 画面が持つ暗黙・事前入力の既定の値（測定・操作の条件）

欄に見えるものもあるが、ほかの窓は「既定を画面に持たない・空から」（EventLog の棒の数、Terminal の SWO の速さ、
CrossTrigger の欄）としており、方針がそろっていない。見えていれば「明示」とみなすか判断がつかない。

- `host/ui/src/lib/cycles.ts:11` `const c = count.trim() === "" ? 1 : Number(count);`（空なら 1 回。SourcePanel.svelte:55 も `"1"`）
- `host/ui/src/lib/etm.ts:61` `const oldest = form.oldest.trim() === "" ? 0 : parseInteger(form.oldest);`（空なら 0。欄には出ない）
- `host/ui/src/lib/lowlevel.ts:137` `mode: … : "while", level: … : "high"`（「API の既定」と書くが仕様から読まず固定）、
  `:148` `intIn(f.value.trim() || "0", 0n, 255n)`（mask があり value が空なら 0）
- `host/ui/src/components/panels/LogicAnalyzerPanel.svelte:184-185`
  `{ address: "", addressMask: "0xFFFFFFFF", data: "0", dataMask: "0", onWrite: true, onRead: false }`、
  `:254` `const num = (s) => Number(s.trim()...)`（空の mask 欄は `Number("")` = 0 で黙って送る）。
  同じ `xtrig.set_match` を CrossTriggerPanel は仕様の欄（空から）で出す
- `host/ui/src/components/panels/FpgaObsPanel.svelte:41-45` `userIr "0x2"`・`tckMhz 10`・`words 64`・`syncPeriod 20`
- `host/ui/src/components/panels/LinuxPanel.svelte:19-20`（port 22・method ptrace・regs "r0"）、`:52-53`（往復 8 回・間隔 1.0 秒）
- `host/ui/src/components/panels/SwoTracePanel.svelte:30` `swoBaud: 2_000_000`（terminal.ts:121-122 は「2 MHz を暗黙に使わない」）
- `host/ui/src/components/panels/SystemPanel.svelte:103` `triggerPost` の既定 1 秒、`:85` `rle: false` を画面が固定
  （「RLE は変化が少ないと十数秒」という経験からの選択）、`:272` la_stream は `rle: true` を固定

### ?2. 標本の間を前の値で埋める描き方・読み方（保持の仮定）

H1 ほど強くないが、測っていない時刻の値を出す。標準的な描き方とも言え、判断がつかない。

- `host/ui/src/lib/sampling.ts:399-408` `heldAt`: カーソル A・B の値を「x 以下の最後の点」で出し、その点の時刻は出さない
  （measureBetween の a・b・delta）
- `host/ui/src/components/charts/Chart.svelte:210` `if (pen && s.step) g.lineTo(left + plotW(), lastY);`（最後の標本の値を
  グラフの右端まで伸ばす）、`:302-305`（階段でない線のホバーは「いちばん近い点」の値を、ホバーの時刻の見出しで出す）
- `host/ui/src/lib/timeline/decimate.ts:213-214`（値の段の画素に「その前の最後のサンプルの値」を含める）・
  `query.ts:40-44`（値の段のホバー・縦線は保たれている値）。値の段の意味として仕様にあるかは未確認

### ?3. 画面での計算（事実だが「画面は計算しない」に当たるか）

推定ではないが、デーモンの応答を描くだけでなく画面が集計・換算する。

- `host/ui/src/lib/interrupts.ts:39-77`（割り込みごとの回数・合計・自己時間・平均・負荷を記録の行から計算。負荷の分母は
  `SPAN_SQL` の rtos の最初〜最後）、`:117-133` 分布図（`narrow`: 整数で種類が棒の数以下なら値ごとに 1 本にし、使う人が
  入れた棒の数を変える）
- `host/ui/src/lib/rtos.ts:104-114`（`covered = Math.max(1, …)`）・`:160-198` `loadWindows`（記録から窓ごとの負荷を
  デーモンと同じ定義で再計算）
- `host/ui/src/lib/eventlog.ts:207-215` `kindStats`（割合・1 秒あたり）
- `host/ui/src/lib/sampling.ts:318-336` `health`（取りこぼしの割合・実際の周期）、`:131-148` `Stats`（表の最小・最大・平均）、
  `host/ui/src/lib/power.ts:65-78`（電圧の窓の統計）

### ?4. 値の形（キーの名前）で種類を決める

デーモンの値の形（fprobe.dwarf.values）が型の印を持たず、画面がキーの有無で判定する。構造体のメンバーが `pointer`+`to`・
`enum`+`value`・`string`+`values`・`length`+`head`・`unavailable` だと別の種類に見える。「名前の慣習での判定」に近いが、
デーモンの符号化が決める名前なので判断がつかない。

- `host/ui/src/lib/vartree.ts:27-39` `kindOf`、`:44-46` `isFloatType`（型名の正規表現。`float32_t` 等の typedef は浮動小数と
  分からず `3 (0x3)` と出る）
- `host/ui/src/lib/sampling.ts:105-110` `numberOf`・`:113-128` `displayValue`・`:280-300` `arrayWave`、
  `host/ui/src/lib/breakpoints.ts:59-71` `formatValue`
- `host/ui/src/lib/eventlog.ts:95-114` `summary`（本文の `text`・`method`・`task`・`to`・`name`… の有無で要約を選ぶ。
  CSV の summary の列にも入る `:131`）
- `host/ui/src/components/panels/RtosObjectsPanel.svelte:73-91` `describeFields`（`o.kind` でなく欄の有無で表し方を選ぶ）

### ?5. 時刻 0 を「無い」とみなす

`host/ui/src/components/panels/CrossTriggerPanel.svelte:190`、`host/ui/src/components/panels/LogicAnalyzerPanel.svelte:307`

```ts
const recordingTo = (t: number | null) => (t !== null && t > 0 ? { source: "recording" as const, t } : null);
```

- 理由: レジスタの時刻が 0 なら時間軸へのリンクを出さない（「—」）。0 が「起きていない」の符号かは API の仕様次第で、
  画面の判断かどうか判断がつかない。あわせて、レジスタの時刻（タイムベースのカウント）を記録の時刻とみなしている。

### ?6. 複数の事実から 1 つを選んで出す（ほかを落とす）

- `host/ui/src/lib/crosstrigger.ts:202-207` `laTriggerTime`: 内蔵と深いバッファの両方がトリガしたら新しい方だけ
- `host/ui/src/lib/fpgaobs.ts:112-119` `statusKey`: stuck・overflow・running・driving を優先順で 1 語にまとめる（stuck なら
  overflow は文言に出ない。バッジの色は別に付く）
- `host/ui/src/lib/codeat.ts:99-106` `fileToOpen` と `SourcePanel.svelte:33-39`: 止まっていなければ「直前のサンプル」の行を
  自動で開いて中央へ動かす（印・ツールチップは「推定しない」と言うが、開く行の選び方は画面の優先順）

### ?7. 再起動を番号の戻りで推す

- `host/ui/src/lib/state.ts:133-134`
  ```ts
  const restarted = otherBoot ||
    (known.length > 0 && (fresh.length === 0 || fresh[fresh.length - 1].seq < known[known.length - 1].seq));
  ```
- `host/ui/src/lib/breakpoints.ts:131` `if (page.next < this.since) { // 起動し直した: 番号が戻った`
- 理由: boot の識別があるのに、番号が戻った・空になったことからも再起動と推す（古いデーモン向けの代わりの規則か不明）。

### ?8. 画面が自分の規則で段・根を隠す・畳む・並べ替える

- `host/ui/src/components/panels/SystemPanel.svelte:311-321` と `host/ui/src/lib/system.ts:111-118` `flatChannels`・
  `host/ui/src/lib/timeline/rows.ts:180-195` `systemOrder`: 縮図（幅 400）で全体が同じ値のピンの段を自動で隠し、
  stream の名前の固定の表（SYSTEM_GROUPS・SYSTEM_FOLDED）で並べ・畳む。判定は事実（変化が無い）だが、隠すのは画面の判断
- `host/ui/src/lib/timeline/state.ts:127-134` `overviewSpecs`: 縮図のロジアナの ch を 8 本で切る（切ったことを出さない）
- `host/ui/src/lib/callgraph.ts:42-49` `rootsOf`（根を種類の固定の順と最大スタックの大きい順に並べる）と
  `CallGraphPanel.svelte:58-59`（先頭の根の最深の経路を自動で開く）

### ?9. 古いタイルを「探し切った」とみなす

`host/ui/src/lib/timeline/tile.ts:106-107`・`:188` `carryComplete: l.carry_complete ?? true`

- 理由: 申告が無ければ「範囲の始めの値は探し切った」とみなし、「不明」の斜線を出さない。古いデーモンの互換のための
  仮定だが、仮定が外れると欠けを隠す。

### ?10. 打ち切りの表示が不十分

- `host/ui/src/components/panels/SwoTracePanel.svelte:19`・`:73-75`・`:145`: 終わりの 500 件だけを表に出し、件数は全体の
  `total` だけを出す（「N 件のうち 500 件・上限 500」とは出さない）
- `host/ui/src/components/panels/DashboardPanel.svelte:51` `new SampleStore(400)`: グラフの部品の点を 400 で捨て、捨てた数を
  出さない（Data Sampling は `discarded` を出す）
- `host/ui/src/components/panels/VerifyRules.svelte:122` `limit: r.violations.length`: 上限の欄に表示した件数を入れており、
  使った上限ではない
- `host/ui/src/lib/format.ts:37` `JSON.stringify(e.result).slice(0, 80)`: Activity の行の結果を 80 文字で切り、印（…）を
  付けない（詳細の欄には全部出る）

### ?11. 情報の足りないときの暗黙の値

- `host/ui/src/components/panels/PeripheralsPanel.svelte:117`・`:171` `defs[path]?.size ?? 32`: SVD の定義が無ければ
  レジスタを 32 ビットとみなして書く・表示する

### ?12. 色・文言が判定を含む

- `host/ui/src/components/panels/DataSamplingPanel.svelte:178` 条件での取得の区間を tone 2（good = 緑）で時間軸に置く
- `host/ui/src/lib/i18n.ts:1724`（EN）・`:3663`（JA） `safety.fatalHint`: 「実行中に読み出す値は更新されていません」と断定する。
  pc_in（使う側が渡した関数の中に PC がある）のときは値が更新され続けることもありうる
- `host/ui/src/components/panels/FlashPanel.svelte:578` 書き込み計測のセクタの欄の placeholder `0x08060000`（特定の品種の
  番地。同じファイルの :30 は「特定の品種の番地・長さを初めから入れない」）

---

## 参考: 読んで H・? にしなかった主なもの

- 規格・仕様が決める値: VTOR 0xE000ED08（callgraph.ts:76）、DP のレジスタ名（lowlevel.ts:20-30）、AP の IDR 0xFC の事前入力
  （LowLevelPanel.svelte:59）、SVD の access の既定 read-write（peripherals.ts:33）、FAULT_EXCEPTIONS の名前（fault.ts:44）、
  CMSIS の SystemCoreClock（target.ts:27-37）。
- 画面の操作・描画だけのもの: ポーリングの間隔と待ち（poll.ts、screen.ts:371 の 5 秒）、目盛りの切りのよい値（chart.ts）、
  密度の描画への切り替え（compose.ts:170）、ラベルの最小幅、ズームの上限、ホバーの拾う幅、ホイールの率。
- 打ち切りを示しているもの: activity.ts、interrupts.ts（queryCut）、breakpoints.ts・logview.ts・linux.ts（discarded）、
  terminal.ts、sampling.ts（discarded・gaps）、Chart の散布図、CallGraph・Profile（topCut）、Power（findPages）。
- 使う側が入れる値で動くもの: power.ts の findDips（閾値は利用者）、eventlog.ts の measureParams、Interrupts の棒の数・上位、
  System の窓・上位の数・N 件、captureRequest、Memory の番地。

---

## 読んだファイル（152 ファイル。いずれも先頭から最後まで全部読んだ）

- `host/ui/src/main.ts`
- `host/ui/src/App.svelte`
- `host/ui/src/components/Icon.svelte`
- `host/ui/src/components/Timeline.svelte`
- `host/ui/src/components/TimelineMinimap.svelte`
- `host/ui/src/components/TimelineRows.svelte`
- `host/ui/src/components/charts/Chart.svelte`
- `host/ui/src/components/panels/ActivityPanel.svelte`
- `host/ui/src/components/panels/ApiFields.svelte`
- `host/ui/src/components/panels/ApiPanel.svelte`
- `host/ui/src/components/panels/BreakpointsPanel.svelte`
- `host/ui/src/components/panels/CallGraphPanel.svelte`
- `host/ui/src/components/panels/CallStackPanel.svelte`
- `host/ui/src/components/panels/CodeAtNote.svelte`
- `host/ui/src/components/panels/CodeAtToggle.svelte`
- `host/ui/src/components/panels/ConditionFields.svelte`
- `host/ui/src/components/panels/CpuLoadPanel.svelte`
- `host/ui/src/components/panels/CrossTriggerPanel.svelte`
- `host/ui/src/components/panels/DashboardPanel.svelte`
- `host/ui/src/components/panels/DataSamplingPanel.svelte`
- `host/ui/src/components/panels/DecoderFields.svelte`
- `host/ui/src/components/panels/DisassemblyPanel.svelte`
- `host/ui/src/components/panels/EditValue.svelte`
- `host/ui/src/components/panels/Empty.svelte`
- `host/ui/src/components/panels/EventLogPanel.svelte`
- `host/ui/src/components/panels/FaultPanel.svelte`
- `host/ui/src/components/panels/FavoritesPanel.svelte`
- `host/ui/src/components/panels/FlashPanel.svelte`
- `host/ui/src/components/panels/FpgaObsPanel.svelte`
- `host/ui/src/components/panels/HeapPanel.svelte`
- `host/ui/src/components/panels/InterruptsPanel.svelte`
- `host/ui/src/components/panels/LinuxPanel.svelte`
- `host/ui/src/components/panels/LoadState.svelte`
- `host/ui/src/components/panels/LocalsPanel.svelte`
- `host/ui/src/components/panels/LogPanel.svelte`
- `host/ui/src/components/panels/LogicAnalyzerPanel.svelte`
- `host/ui/src/components/panels/LowLevelPanel.svelte`
- `host/ui/src/components/panels/MemoryPanel.svelte`
- `host/ui/src/components/panels/PeripheralsPanel.svelte`
- `host/ui/src/components/panels/PowerPanel.svelte`
- `host/ui/src/components/panels/ProfilePanel.svelte`
- `host/ui/src/components/panels/RecordingsPanel.svelte`
- `host/ui/src/components/panels/RegistersPanel.svelte`
- `host/ui/src/components/panels/RtosObjectsPanel.svelte`
- `host/ui/src/components/panels/SafetyConditions.svelte`
- `host/ui/src/components/panels/SamplerNotice.svelte`
- `host/ui/src/components/panels/SourcePanel.svelte`
- `host/ui/src/components/panels/SwoTracePanel.svelte`
- `host/ui/src/components/panels/SystemPanel.svelte`
- `host/ui/src/components/panels/TabBar.svelte`
- `host/ui/src/components/panels/TerminalPanel.svelte`
- `host/ui/src/components/panels/ThreadsPanel.svelte`
- `host/ui/src/components/panels/TimeLink.svelte`
- `host/ui/src/components/panels/TimelinePanel.svelte`
- `host/ui/src/components/panels/VarRow.svelte`
- `host/ui/src/components/panels/VarTree.svelte`
- `host/ui/src/components/panels/VerifyHooks.svelte`
- `host/ui/src/components/panels/VerifyInject.svelte`
- `host/ui/src/components/panels/VerifyPanel.svelte`
- `host/ui/src/components/panels/VerifyRules.svelte`
- `host/ui/src/components/panels/WatchPanel.svelte`
- `host/ui/src/components/panels/format.ts`
- `host/ui/src/components/panels/registry.ts`
- `host/ui/src/components/shell/ConfirmDialog.svelte`
- `host/ui/src/components/shell/DebugToolbar.svelte`
- `host/ui/src/components/shell/Dialogs.svelte`
- `host/ui/src/components/shell/DockArea.svelte`
- `host/ui/src/components/shell/MenuBar.svelte`
- `host/ui/src/components/shell/SafetyBar.svelte`
- `host/ui/src/components/shell/StatusLine.svelte`
- `host/ui/src/lib/activity.ts`
- `host/ui/src/lib/api.ts`
- `host/ui/src/lib/apicall.ts`
- `host/ui/src/lib/apiform.ts`
- `host/ui/src/lib/apispec.ts`
- `host/ui/src/lib/breakpoints.ts`
- `host/ui/src/lib/callgraph.ts`
- `host/ui/src/lib/chart.ts`
- `host/ui/src/lib/codeat.ts`
- `host/ui/src/lib/commands.ts`
- `host/ui/src/lib/confirm.ts`
- `host/ui/src/lib/context.ts`
- `host/ui/src/lib/crosstrigger.ts`
- `host/ui/src/lib/cut.ts`
- `host/ui/src/lib/cycles.ts`
- `host/ui/src/lib/dashboard.ts`
- `host/ui/src/lib/debugger.ts`
- `host/ui/src/lib/diagnose.ts`
- `host/ui/src/lib/dock.ts`
- `host/ui/src/lib/dockwindows.ts`
- `host/ui/src/lib/etm.ts`
- `host/ui/src/lib/eventlog.ts`
- `host/ui/src/lib/execution.ts`
- `host/ui/src/lib/fault.ts`
- `host/ui/src/lib/favorites.ts`
- `host/ui/src/lib/flash.ts`
- `host/ui/src/lib/format.ts`
- `host/ui/src/lib/fpgaobs.ts`
- `host/ui/src/lib/guard.ts`
- `host/ui/src/lib/heap.ts`
- `host/ui/src/lib/i18n.ts`（3,948 行。EN と JA の表を全部）
- `host/ui/src/lib/interrupts.ts`
- `host/ui/src/lib/linux.ts`
- `host/ui/src/lib/logic.ts`
- `host/ui/src/lib/logview.ts`
- `host/ui/src/lib/lowlevel.ts`
- `host/ui/src/lib/memory.ts`
- `host/ui/src/lib/notify.ts`
- `host/ui/src/lib/peripherals.ts`
- `host/ui/src/lib/poll.ts`
- `host/ui/src/lib/power.ts`
- `host/ui/src/lib/prefs.ts`
- `host/ui/src/lib/profile.ts`
- `host/ui/src/lib/protection.ts`
- `host/ui/src/lib/recordings.ts`
- `host/ui/src/lib/rtos.ts`
- `host/ui/src/lib/safety.ts`
- `host/ui/src/lib/sampler.ts`
- `host/ui/src/lib/sampling.ts`
- `host/ui/src/lib/screen.ts`
- `host/ui/src/lib/snapshots.ts`
- `host/ui/src/lib/source.ts`
- `host/ui/src/lib/stack.ts`
- `host/ui/src/lib/state.ts`
- `host/ui/src/lib/swotrace.ts`
- `host/ui/src/lib/system.ts`
- `host/ui/src/lib/target.ts`
- `host/ui/src/lib/terminal.ts`
- `host/ui/src/lib/time.ts`
- `host/ui/src/lib/ui.ts`
- `host/ui/src/lib/valuewrite.ts`
- `host/ui/src/lib/vartree.ts`
- `host/ui/src/lib/verify.ts`
- `host/ui/src/lib/watchshare.ts`
- `host/ui/src/lib/waveform.ts`
- `host/ui/src/lib/timeline/compose.ts`
- `host/ui/src/lib/timeline/cursor.ts`
- `host/ui/src/lib/timeline/decimate.ts`
- `host/ui/src/lib/timeline/decimator.ts`
- `host/ui/src/lib/timeline/events.ts`
- `host/ui/src/lib/timeline/frame.ts`
- `host/ui/src/lib/timeline/layout.ts`
- `host/ui/src/lib/timeline/overlay.ts`
- `host/ui/src/lib/timeline/query.ts`
- `host/ui/src/lib/timeline/render-2d.ts`
- `host/ui/src/lib/timeline/render-gl.ts`
- `host/ui/src/lib/timeline/renderer.ts`
- `host/ui/src/lib/timeline/rows.ts`
- `host/ui/src/lib/timeline/state.ts`
- `host/ui/src/lib/timeline/tile.ts`
- `host/ui/src/lib/timeline/titles.ts`
- `host/ui/src/lib/timeline/worker.ts`
