<script lang="ts">
  // Data Sampling（Ozone の Data Sampling・J-Scope・IAR の Live Watch・CubeIDE の Live Expressions・Keil の Logic
  // Analyzer・MPLAB Data Visualizer のグラフ）: 式をターゲットを止めずに周期的に読む（watch.*）。
  // 表 = 式ごとの最新の値・最小・最大・平均・件数・時刻（時間軸へのリンク）、取得の状態（周期・取りこぼし・誤り・方式）。
  // グラフ = 時系列（複数・右の目盛り・一時停止・窓）/ XY（2 つの式の散布）/ 配列（止まっているときに debug.evaluate）/
  // 条件での取得（watch.trigger → watch.capture）。CSV に書き出す。時系列・条件での取得のグラフはカーソル A・B
  // （クリック・Shift + クリック）で 2 点の値・差・間の統計を測る（デーモンの watch.measure）。
  // 時間軸: グラフのダブルクリック・行のダブルクリック・時刻のリンクで reveal、時間軸の選択はグラフの縦線に。
  // 観測の組は Dashboard・AI と分け合う（lib/watchshare.ts: 足すだけ・自分の式だけやめる・取り合いを出す）。
  // 式の一覧と周期はデーモンの画面の状態（ui.state の sampling）で別の窓・別の PC・AI と同じものを見る。周期の欄は
  // 取っている間は実際の周期（watch.status）。誤った式は watch.set が外し（rejected）、その行に印と理由を出す。
  // 表の最新の値と配列の要素は、値のダブルクリックで書ける（debug.write_expression。走行中は止めずに、止まっていればフレームで。道はデーモンが選ぶ。
  // lib/valuewrite の exprWriteCall）
  import { untrack } from "svelte";
  import Req from "./Req.svelte";
  import { interval, intervalNow } from "../common/interval";
  import Chart from "../charts/Chart.svelte";
  import { download, shortNumber, toCsv, type Series } from "../../lib/chart";
  import { shared } from "../../lib/context";
  import SamplerNotice from "./SamplerNotice.svelte";
  import { allowed } from "../../lib/guard";
  import { pollWatch } from "../../lib/samplingpoll";
  import { elementExpr, exprWriteCall, exprWriteMethod } from "../../lib/valuewrite";
  import { parseInput } from "../../lib/vartree";
  import { errorText } from "../../lib/api";
  import { i18n } from "../../lib/i18n";
  import { suggestedOf } from "../../lib/apispec";
  import { load, save } from "../../lib/prefs";
  import {
    addExpr, arrayWave, captureSeries, chartSeries, health, parseExprList,
    rateOptions, removeExpr, SampleStore, SharedSampling, toggleCursor, triggerParams, windowDomain, xyPoints,
    type ChartCursors, type SampledExpr, type TriggerForm,
  } from "../../lib/sampling";
  import { axisTime, formatDuration, usSeconds } from "../../lib/time";
  import { onTimeline } from "../../lib/timeline/events";
  import { readWatch, startWatch, stopWatch, type ExpressionFacts } from "../../lib/watchshare";
  import Empty from "./Empty.svelte";
  import SamplingArray from "./sampling/SamplingArray.svelte";
  import SamplingModeBar from "./sampling/SamplingModeBar.svelte";
  import SamplingHealth from "./sampling/SamplingHealth.svelte";
  import SamplingMeasure from "./sampling/SamplingMeasure.svelte";
  import SamplingTable from "./sampling/SamplingTable.svelte";

  const OWNER = "dataSampling";
  const POLL_MS = 300, STATUS_MS = 1000;
  const MODES = ["time", "xy", "array", "capture"] as const;
  type Mode = (typeof MODES)[number];
  const modes: readonly string[] = MODES;

  const { app, conn, tl, reveal } = shared();
  const call = conn.call.bind(conn);
  const act = app.act.bind(app);

  let list = $state<SampledExpr[]>(parseExprList(load("dataSampling.expressions", "[]")));
  let input = $state("");
  // 周期は使う人が選ぶ（画面の既定を持たない。選ぶまで Start できない）
  const positive = (v: number) => (Number.isFinite(v) && v > 0 ? v : null);
  let rate = $state<number | null>(positive(Number(load("dataSampling.rate", ""))) ?? positive(Number(suggestedOf("watch.set", "rate_hz"))));   // 最初の値は仕様の既定
  let windowS = $state(Number(load("dataSampling.window", "10")));
  let mode = $state<Mode>((load("dataSampling.mode", "time") as Mode));
  let paused = $state(false);
  let selected = $state<string | null>(null);
  let status = $state<any>(null);              // watch.status
  let running = $state<string[]>([]);          // いま観測している式（全員の）
  let facts = $state<ExpressionFacts | null>(null);   // 自分の式と観測の組の関係（watch.status の expressions。API の事実）
  let startedHere = $state(false);             // このビューで Start した（取り合いの表示に使う）
  let busy = $state(false);
  let gen = $state(0);                         // 値を足すたびに増やす（表とグラフを描き直す）
  let frozen = $state<Series[] | null>(null);  // 一時停止の間のグラフ
  let cursor = $state<number | null>(null);    // 時間軸の選択（µs）
  let xName = $state(load("dataSampling.x", ""));
  let yName = $state(load("dataSampling.y", ""));
  let maxDtUs = $state("");                    // XY の組の時刻の差の上限（µs。空 = 渡さない = 外さない）
  let xyResult = $state<any>(null);            // watch.xy の応答（組み方・時刻の差はデーモンが決める）
  let arrayExpr = $state(load("dataSampling.array", ""));
  let member = $state("");
  let wave = $state<{ values: number[]; length: number } | null>(null);
  let waveError = $state<string | null>(null);
  let waveSource = { expr: "", member: "" };   // 描いている配列を読んだ式（要素に書く式の元）
  let trig = $state<TriggerForm>({ variable: "", op: ">", value: "", edge: true, preMs: "", postMs: "" });
  let trigError = $state<string | null>(null);
  let capture = $state<any>(null);             // watch.capture
  const store = new SampleStore();

  const target = $derived($app.target);
  const connected = $derived(!!target?.state && target.state !== "DISCONNECTED");
  const halted = $derived(target?.state === "HALTED");
  const mine = $derived(list.map((e) => e.expr));
  const sampling = $derived(mine.some((e) => running.includes(e)));
  const h = $derived(health(status));
  const lost = $derived(startedHere ? (facts?.missing ?? []) : []);
  const others = $derived(facts?.others ?? []);
  // 周期の欄: 取っている間は実際の周期（AI・ほかの窓が別の周期で始めても食い違わない）
  const shownRate = $derived(h.running ? h.rate : rate);

  // 式の一覧と周期をデーモンの画面の状態で分け合う
  let sharedReady = false;
  const sharing = new SharedSampling(conn, (next, r) => {
    list = next;
    if (r) rate = r;
  });
  $effect(() => () => sharing.dispose());
  $effect(() => {
    if ($app.link !== "open") {
      sharedReady = false;
      return;
    }
    void untrack(() => sharing.load(list, rate)).then(() => (sharedReady = true));
  });
  $effect(() => {
    const snapshot = list.map((e) => ({ ...e }));
    const r = rate;
    if (untrack(() => sharedReady)) void sharing.save(snapshot, r);
  });

  $effect(() => save("dataSampling.expressions", JSON.stringify(list)));
  $effect(() => {
    save("dataSampling.rate", rate === null ? "" : String(rate));
    save("dataSampling.window", String(windowS));
    save("dataSampling.mode", mode);
    save("dataSampling.x", xName);
    save("dataSampling.y", yName);
    save("dataSampling.array", arrayExpr);
  });

  // 観測の状態（全員の組・周期・取りこぼし）を周期的に読む
  $effect(() => {
    if (!connected) return;
    return intervalNow(() => readStatus(), STATUS_MS);
  });

  async function readStatus() {
    try {
      const w = await readWatch(call, mine);
      status = w.status;
      running = w.names;
      facts = w.facts;
    } catch {
      status = null;
      running = [];
      facts = null;
    }
  }

  // 自分の式の一覧が変わったら、組との関係を読み直す（次の周期の読みまで古いままにしない）
  $effect(() => {
    void mine.join("\n");
    if (untrack(() => connected)) void untrack(() => readStatus());
  });

  // 観測している間、値（watch.values）と描く式の列（watch.series）を読む
  $effect(() => {
    if (!sampling || !connected) return;
    return interval(() => poll(), POLL_MS);
  });

  let polling = false;
  async function poll() {
    if (polling) return;
    polling = true;
    try {
      const names = untrack(() => mine.filter((e) => running.includes(e)));
      const plotted = untrack(() => list.filter((e) => e.plot && running.includes(e.expr)).map((e) => e.expr));
      const xy = untrack(() => (mode === "xy" && xName && yName && names.includes(xName) && names.includes(yName)
        ? { x: xName, y: yName, maxDtUs: maxDtUs.trim() } : null));
      const captureOpen = untrack(() => !!(capture?.state && capture.state !== "done"));
      const out = await pollWatch(call, store, { names, plotted, xy, captureOpen });
      if (out.xy !== undefined) xyResult = out.xy;
      if (out.capture !== undefined) capture = out.capture;
      gen++;
    } catch {
      /* 次の周期で読み直す（観測が止められた等。状態の読みが表に出す） */
    } finally {
      polling = false;
    }
  }

  // 時間軸の 1 回クリック: その時刻の縦線（記録の時刻 → タイムベースの µs）
  $effect(() => onTimeline({
    select: (d) => {
      if (d.source === "recording") cursor = d.t * tl.snapshot.tickS * 1e6;
    },
  }));

  // 条件での取得の区間を時間軸の印に（記録の時刻に換算できるとき）。$tl を直に読むと印を置くたびに走り直すので、
  // 時刻 0 の store（変わったときだけ配る）から記録の 1 単位の秒だけを
  const origins = tl.origins;
  const tickS = $derived($origins.recording?.tickS ?? 0);
  $effect(() => {
    const c = capture;
    if (c?.state === "done" && c.t_start_us !== null && tickS > 0) {
      const t = (us: number) => Math.round((us * 1e-6) / tickS);
      tl.setMarks(OWNER, "recording", [{ t: t(c.t_start_us), tEnd: t(c.t_end_us ?? c.t_start_us), label: $i18n("sampling.trigger"), tone: 0 }]);
    } else {
      tl.setMarks(OWNER, "recording", []);
    }
  });
  $effect(() => () => tl.setMarks(OWNER, "recording", []));

  function add(e: Event) {
    e.preventDefault();
    list = addExpr(list, input);
    input = "";
  }

  async function remove(expr: string) {
    const wasSampling = sampling && running.includes(expr);
    list = removeExpr(list, expr);
    store.forget(expr);
    gen++;
    if (wasSampling && (await allowed(app))) {
      await stopWatch(call, act, OWNER, [expr]);
      const r = h.rate || rate;                   // 取っている組の周期のまま（入れていなければ足し直さない）
      if (list.length && r) await startWatch(call, act, OWNER, mine, r);
      await readStatus();
    }
  }

  async function start() {
    const r = rate;
    if (!list.length || r === null || !(await allowed(app))) return;
    busy = true;
    try {
      startedHere = await startWatch(call, act, OWNER, mine, r);
      await readStatus();
    } finally {
      busy = false;
    }
  }

  /** 周期を変える（取っている間は組全体の周期を置き換える） */
  async function changeRate(next: number) {
    rate = next;
    if (sampling && (await allowed(app))) {
      busy = true;
      try {
        await startWatch(call, act, OWNER, mine, next);
        await readStatus();
      } finally {
        busy = false;
      }
    }
  }

  async function stop() {
    if (!(await allowed(app))) return;
    busy = true;
    try {
      await stopWatch(call, act, OWNER, mine);
      startedHere = false;
      await readStatus();
    } finally {
      busy = false;
    }
  }

  function clear() {
    store.clear();
    frozen = null;
    gen++;
  }

  function togglePause() {
    paused = !paused;
    frozen = paused ? chartSeries(store, list) : null;
  }

  // 表の行（統計はデーモンが watch.set からの値すべてで数えたもの）
  const rows = $derived.by(() => {
    void gen;
    return list.map((e) => {
      const s = status?.stats?.[e.expr];                       // watch.status の stats（watch.set からの値すべて）
      return { e, last: store.latest(e.expr), st: { count: s?.count ?? 0, min: s?.min, max: s?.max, mean: s?.mean },
               discarded: store.discarded(e.expr), gaps: store.gaps(e.expr) };
    });
  });
  // グラフの点を上限で捨てた式（黙って捨てない）
  const discardedText = $derived(rows.filter((r) => r.discarded).map((r) => `${r.e.expr} ${r.discarded}`).join(", "));
  // 画面の読みが追いつかず読まなかった区間（watch.series の from_us。グラフに無い）
  const gapsText = $derived(rows.filter((r) => r.gaps.length).map((r) => `${r.e.expr} ${r.gaps.length}`).join(", "));
  const timeSeries = $derived.by(() => {
    void gen;
    return frozen ?? chartSeries(store, list);
  });
  const xDomain = $derived.by(() => {
    void gen;
    const end = frozen ? Math.max(...frozen.map((s) => (s.x.length ? s.x[s.x.length - 1] : -Infinity))) : store.end();
    return windowDomain(Number.isFinite(end) ? end : null, windowS);
  });
  $effect(() => {
    void [xName, yName, maxDtUs];
    xyResult = null;                           // 選び直したら前の組を描かない
  });
  const xy = $derived(xName && yName && xyResult?.x === xName && xyResult?.y === yName ? xyPoints(xyResult) : null);
  const xyFacts = $derived(xy ? $i18n("sampling.xyFacts", {
    n: xyResult.points.length,
    dt: xyResult.dt_max_us === null ? "—" : formatDuration(xyResult.dt_max_us * 1e-6),
    unpaired: xyResult.unpaired, excluded: xyResult.excluded,
  }) : "");
  // 条件で取った値: 表で「グラフ」に印を付けた式を、表の左右の目盛りで（印が無ければ取った式すべて）
  const capSeries = $derived.by(() => {
    const plotted = list.filter((e) => e.plot);
    const axis = new Map(list.map((e) => [e.expr, e.axis]));
    return captureSeries(capture?.samples, plotted.length ? plotted.map((e) => e.expr) : mine.concat(others))
      .map((s) => ({ ...s, axis: axis.get(s.name) ?? "left" }));
  });
  const waveSeries = $derived<Series[]>(wave ? [{ name: arrayExpr, x: wave.values.map((_, i) => i), y: wave.values }] : []);

  function exportCsv() {
    const series = mode === "capture" ? capSeries : mode === "array" ? waveSeries : timeSeries;
    if (!series.length) return;
    download(`fprobe-${mode === "array" ? "array" : "sampling"}.csv`, toCsv(series, mode === "array" ? "index" : "time_us"), "text/csv");
  }

  async function readArray(e: Event) {
    e.preventDefault();
    if (!arrayExpr.trim()) return;
    try {
      const r = await call("debug.evaluate", { expression: arrayExpr.trim(), max_elements: 4096 }, { quiet: true });
      wave = arrayWave(r?.value, member.trim());
      waveSource = { expr: arrayExpr.trim(), member: member.trim() };
      waveError = wave ? null : $i18n("sampling.arrayNotNumeric");
    } catch (err) {
      wave = null;
      waveError = err instanceof Error ? err.message : String(err);
    }
  }

  async function arm(e: Event) {
    e.preventDefault();
    const p = triggerParams(trig);
    if (typeof p === "string") {
      trigError = $i18n(p === "value" ? "sampling.badValue" : "sampling.badTime");
      return;
    }
    if (!sampling) {
      trigError = $i18n("sampling.needStart");
      return;
    }
    trigError = null;
    if (!(await allowed(app))) return;
    const r = await app.act("watch.trigger", p);
    if (r) {
      capture = r;
      mode = "capture";
    }
  }

  // 値を書く（式・配列の要素）。debug.write_expression（道はデーモンが選ぶ）
  const via = $derived(exprWriteMethod(target?.state));
  async function writeExpr(expression: string, input: string): Promise<string | null> {
    const c = exprWriteCall(target?.state, expression, parseInput(input));
    if (!c || !(await allowed(app))) return null;
    const done = await app.act(c.method as "debug.write_expression", c.params as any);
    if (done !== undefined && mode === "array" && wave && halted) await readArray(new Event("submit"));
    return null;
  }

  const openAt = (us: number) => reveal({ source: "recording", us });
  // 横軸はタイムベースの µs。表示は記録の始めからの秒（記録が無ければタイムベースの 0 から）
  const fmtX = $derived(axisTime(usSeconds($origins), 1e-6));

  // カーソル A・B（時系列・条件での取得。横軸はタイムベースの µs）。グラフの種類を変えたら消す
  let ab = $state<ChartCursors>({ a: null, b: null });
  let abMode: Mode = untrack(() => mode);
  $effect(() => {
    if (mode !== abMode) {
      abMode = mode;
      ab = { a: null, b: null };
    }
  });
  const measuring = $derived(mode === "time" || mode === "capture");
  // A・B の値と間の統計は watch.measure（デーモンが持つ値で測る。描いている点ではない）
  let measured = $state<any>(null);
  let measureError = $state<string | null>(null);
  $effect(() => {
    const a = ab.a, b = ab.b, source = mode === "time" ? "live" : "capture";
    measured = null;
    measureError = null;
    if (!measuring || a === null || b === null) return;
    const names = untrack(() => (mode === "time" ? timeSeries : capSeries).map((s) => s.name));
    call("watch.measure", { a_us: a, b_us: b, names, source }, { quiet: true })
      .then((r: any) => (measured = r), (e: unknown) => (measureError = errorText(e)));
  });
  const abMarks = $derived([...(ab.a !== null ? [{ x: ab.a, label: $i18n("sampling.cursorA") }] : []),
                            ...(ab.b !== null ? [{ x: ab.b, label: $i18n("sampling.cursorB") }] : [])]);
  const pick = (x: number, _bar: number | null, e: MouseEvent) => (ab = toggleCursor(ab, x, e.shiftKey));
  const at = (us: number) => fmtX(us, 1);                 // 1 µs の桁
</script>

<div class="pane sampling">
  <SamplerNotice mine="watch" />
  <form class="pane-bar" onsubmit={add}>
    <input class="expr" aria-label={$i18n("watch.placeholder")} placeholder={$i18n("watch.placeholder")} bind:value={input} />
    <button disabled={!input.trim()}>{$i18n("watch.add")}</button>
    <label title={$i18n("sampling.rateTip")}>{$i18n("sampling.rate")}<Req />
      <select value={shownRate ?? ""} disabled={busy} aria-required="true"
              onchange={(ev) => void changeRate(Number((ev.currentTarget as HTMLSelectElement).value))}>
        {#if shownRate === null}<option value="" disabled></option>{/if}
        {#each rateOptions(shownRate ?? 0) as r (r)}<option value={r}>{r >= 1000 ? `${r / 1000} kHz` : `${r} Hz`}</option>{/each}
      </select>
    </label>
    {#if sampling}
      <button type="button" disabled={busy || !connected} title={$i18n("sampling.stopTip")} onclick={stop}>{$i18n("sampling.stop")}</button>
    {:else}
      <button type="button" class="primary" disabled={busy || !connected || !list.length || rate === null}
              title={$i18n("sampling.startTip")}
              onclick={start}>{$i18n("sampling.start")}</button>
    {/if}
    <button type="button" class:on={paused} title={$i18n("sampling.pauseTip")} onclick={togglePause}>
      {$i18n(paused ? "sampling.resume" : "sampling.pause")}</button>
    <button type="button" title={$i18n("sampling.clearTip")} onclick={clear}>{$i18n("sampling.clear")}</button>
    <button type="button" onclick={exportCsv}>{$i18n("sampling.exportCsv")}</button>
    <span class="spacer"></span>
    <SamplingHealth {h} {status} {connected} />
  </form>

  {#if !connected}
    <Empty need="connected" />
  {:else}
    {#if lost.length}
      <div class="note warn" title={$i18n("sampling.missingTip")}>{$i18n("sampling.missing", { names: lost.join(", ") })}</div>
    {/if}
    {#if others.length}
      <div class="note muted" title={$i18n("sampling.othersTip")}>{$i18n("sampling.others", { names: others.join(", ") })}</div>
    {/if}
    <div class="body">
    <div class="left">
    {#if !list.length}
      <div class="empty"><strong>{$i18n("sampling.noExpressions")}</strong><span>{$i18n("sampling.noExpressionsHint")}</span></div>
    {:else}
      <SamplingTable {rows} {status} {h} {running} {via} bind:list bind:selected onopen={openAt}
                     onremove={(expr) => void remove(expr)} onwrite={writeExpr} />
    {/if}
    </div>

    <div class="right">
    <SamplingModeBar {modes} bind:mode bind:windowS {list} bind:xName bind:yName bind:maxDtUs bind:arrayExpr bind:member
                     {halted} bind:trig {trigError} {capture} {running} {connected} onread={readArray} onarm={arm} />

    <div class="plot" title={mode === "time" || mode === "capture" ? $i18n("sampling.chartTip") : ""}>
      {#if mode === "time"}
        <Chart series={timeSeries} {xDomain} {cursor} height={200} xFormat={fmtX} label={$i18n("panel.dataSampling")}
               onopen={(x) => openAt(x)} onpick={pick} marks={abMarks} />
        {#if discardedText}<p class="muted small">{$i18n("sampling.discarded", { limit: store.limit, names: discardedText })}</p>{/if}
        {#if gapsText}<p class="muted small">{$i18n("sampling.gaps", { names: gapsText })}</p>{/if}
      {:else if mode === "xy"}
        {#if xy}
          <Chart type="scatter" series={[{ name: yName, x: xy.x, y: xy.y }]} xLabel={xName} yLabel={yName} height={220}
                 legend={false} label="{yName} / {xName}" />
          <p class="muted small">{xyFacts}</p>
        {:else}
          <p class="empty">{$i18n("sampling.xyHint")}</p>
        {/if}
      {:else if mode === "array"}
          <SamplingArray {wave} {waveSeries} {waveError} {halted} {via}
                         onwrite={(k, input) => writeExpr(elementExpr(waveSource.expr, k, waveSource.member), input)} />
      {:else}
        {#if capSeries.length}
          <Chart series={capSeries} {cursor} height={200} xFormat={fmtX} label={$i18n("sampling.mode.capture")}
                 onopen={(x) => openAt(x)} onpick={pick} marks={abMarks} />
        {/if}
      {/if}
    </div>
    {#if measuring}
      <SamplingMeasure bind:ab {measured} {measureError} {at} />
    {/if}
    </div>
    </div>
  {/if}
</div>

<style>
  .sampling { display: flex; flex-direction: column; container-type: inline-size; }
  .body { display: flex; flex-direction: column; min-height: 0; }
  /* 広いときは表とグラフを左右に（下の段のパネルは横に長い） */
  @container (min-width: 900px) {
    .body { flex-direction: row; flex: 1; }
    .left { flex: 1 1 50%; overflow: auto; min-width: 0; }
    .right { flex: 1 1 50%; overflow: auto; min-width: 0; border-left: 1px solid var(--line); }
  }
  .plot { padding: 4px 8px; }
  .note { padding: 3px 8px; font-size: 12px; }
  .warn { color: var(--warn); }
  .small { font-size: 12px; margin: 0 0 4px; }
  button.on { border-color: var(--accent); color: var(--accent); }
</style>
