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
  // 表の最新の値と配列の要素は、値のダブルクリックで書ける（走行中は watch.write、止まっていれば debug.write_expression。
  // lib/valuewrite の exprWriteCall）
  import { untrack } from "svelte";
  import Chart from "../charts/Chart.svelte";
  import { download, shortNumber, toCsv, type Series } from "../../lib/chart";
  import { shared } from "../../lib/context";
  import SamplerNotice from "./SamplerNotice.svelte";
  import { allowed } from "../../lib/guard";
  import { elementExpr, exprWriteCall, exprWriteMethod } from "../../lib/valuewrite";
  import { inputText, parseInput } from "../../lib/vartree";
  import EditValue from "./EditValue.svelte";
  import { errorText } from "../../lib/api";
  import { i18n } from "../../lib/i18n";
  import { load, save } from "../../lib/prefs";
  import {
    addExpr, arrayWave, captureSeries, chartSeries, displayValue, health, OPS, parseExprList,
    rateOptions, removeExpr, SampleStore, SharedSampling, toggleCursor, triggerParams, WINDOWS_S, windowDomain, xyPoints,
    type ChartCursors, type SampledExpr, type TriggerForm,
  } from "../../lib/sampling";
  import { TIMEBASE_ZERO, axisTime, formatDuration, usSeconds } from "../../lib/time";
  import { onTimeline } from "../../lib/timeline/events";
  import { missing, readWatch, startWatch, stopWatch } from "../../lib/watchshare";
  import Empty from "./Empty.svelte";
  import TabBar from "./TabBar.svelte";
  import TimeLink from "./TimeLink.svelte";

  const OWNER = "dataSampling";
  const POLL_MS = 300, STATUS_MS = 1000;
  const SERIES_READ = 4096;                                 // watch.series の last の最大（API の上限）
  const MODES = ["time", "xy", "array", "capture"] as const;
  type Mode = (typeof MODES)[number];

  const { app, conn, tl, reveal } = shared();
  const call = conn.call.bind(conn);
  const act = app.act.bind(app);

  let list = $state<SampledExpr[]>(parseExprList(load("dataSampling.expressions", "[]")));
  let input = $state("");
  // 周期は使う人が選ぶ（画面の既定を持たない。選ぶまで Start できない）
  const positive = (v: number) => (Number.isFinite(v) && v > 0 ? v : null);
  let rate = $state<number | null>(positive(Number(load("dataSampling.rate", ""))));
  let windowS = $state(Number(load("dataSampling.window", "10")));
  let mode = $state<Mode>((load("dataSampling.mode", "time") as Mode));
  let paused = $state(false);
  let selected = $state<string | null>(null);
  let status = $state<any>(null);              // watch.status
  let running = $state<string[]>([]);          // いま観測している式（全員の）
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
  const lost = $derived(startedHere ? missing(running, mine, Object.keys(h.rejected)) : []);
  const others = $derived(running.filter((e) => !mine.includes(e)));
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
    void readStatus();
    const timer = setInterval(() => void readStatus(), STATUS_MS);
    return () => clearInterval(timer);
  });

  async function readStatus() {
    try {
      const w = await readWatch(call);
      status = w.status;
      running = w.names;
    } catch {
      status = null;
      running = [];
    }
  }

  // 観測している間、値（watch.values）と描く式の列（watch.series）を読む
  $effect(() => {
    if (!sampling || !connected) return;
    const timer = setInterval(() => void poll(), POLL_MS);
    return () => clearInterval(timer);
  });

  let polling = false;
  async function poll() {
    if (polling) return;
    polling = true;
    try {
      const names = untrack(() => mine.filter((e) => running.includes(e)));
      const plotted = untrack(() => list.filter((e) => e.plot && running.includes(e.expr)).map((e) => e.expr));
      const last = SERIES_READ;                      // API の最大。読まなかった区間は from_us で分かる（gaps）
      const [values, ...series] = await Promise.all([
        call("watch.values", {}, { quiet: true }),
        ...plotted.map((name) => call("watch.series", { name, last }, { quiet: true })),
      ]);
      series.forEach((s: any) => store.merge(s.name, s.samples ?? [], typeof s.from_us === "number" ? s.from_us : null));
      for (const v of values?.values ?? []) if (names.includes(v.name) && v.time_us !== null) store.note(v.name, v);
      const [x, y, dt] = untrack(() => [xName, yName, maxDtUs.trim()]);
      if (untrack(() => mode === "xy") && x && y && names.includes(x) && names.includes(y)) {
        xyResult = await call("watch.xy", { x, y, last, ...(dt !== "" && Number.isFinite(Number(dt)) ? { max_dt_us: Number(dt) } : {}) },
                              { quiet: true });
      }
      if (untrack(() => capture?.state && capture.state !== "done")) capture = await call("watch.capture", {}, { quiet: true });
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
  const gapsText = $derived(rows.filter((r) => r.gaps.length).map((r) => `${r.e.expr} ${r.gaps.length}`
    + ` (${formatDuration(r.gaps.reduce((sum, [a, b]) => sum + b - a, 0) / 1e6)})`).join(", "));
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

  // 値を書く（式・配列の要素）。使う API はターゲットの状態で決める
  const via = $derived(exprWriteMethod(target?.state));
  async function writeExpr(expression: string, input: string): Promise<string | null> {
    const c = exprWriteCall(target?.state, expression, parseInput(input));
    if (!c || !(await allowed(app))) return null;
    const done = await app.act(c.method as "watch.write", c.params as any);
    if (done !== undefined && mode === "array" && wave && halted) await readArray(new Event("submit"));
    return null;
  }

  const openAt = (us: number) => reveal({ source: "recording", us });
  // 横軸はタイムベースの µs。表示は記録の始めからの秒（記録が無ければタイムベースの 0 から）
  const fmtX = $derived(axisTime(usSeconds($origins), 1e-6));
  const pct = (r: number) => (r * 100).toFixed(r < 0.01 ? 2 : 1);

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
  const num = (v: number | null) => (v !== null && Number.isFinite(v) ? shortNumber(v) : "—");
</script>

<div class="pane sampling">
  <SamplerNotice mine="watch" />
  <form class="pane-bar" onsubmit={add}>
    <input class="expr" aria-label={$i18n("watch.placeholder")} placeholder={$i18n("watch.placeholder")} bind:value={input} />
    <button disabled={!input.trim()}>{$i18n("watch.add")}</button>
    <label title={$i18n("sampling.rateTip")}>{$i18n("sampling.rate")}<span class="req" aria-hidden="true">*</span>
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
    {#if h.running}
      <span class="mono muted" title={$i18n("sampling.sharedRateTip")}>{$i18n("sampling.sharedRate", { rate: shortNumber(h.rate) })}</span>
      {#if h.rounds !== null}<span class="mono muted" title={$i18n("sampling.sinceStartTip", { s: (h.sinceStartS ?? 0).toFixed(1),
        hz: h.measuredHz !== null ? shortNumber(h.measuredHz) : "—" })}>{$i18n("sampling.rounds", { n: h.rounds })}</span>{/if}
      {#if h.overruns}
        <span class="warn mono" title={$i18n("sampling.lostTip")}>{$i18n("sampling.lost", { n: h.overruns, pct: h.lossRatio === null ? "—" : pct(h.lossRatio) })}</span>
      {/if}
      {#if h.errors}<span class="bad mono" title={$i18n("sampling.errorsTip")}>{$i18n("sampling.errors", { n: h.errors })}</span>{/if}
      {#if status?.stats_lost_events}<span class="warn mono" title={$i18n("sampling.statsLostTip")}>{$i18n("sampling.statsLost", { n: status.stats_lost_events })}</span>{/if}
    {:else if connected}
      <span class="muted">{$i18n("sampling.idle")}</span>
    {/if}
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
      <table class="grid">
        <thead><tr>
          <th title={$i18n("sampling.plotTip")}>{$i18n("sampling.plot")}</th><th>{$i18n("common.name")}</th>
          <th>{$i18n("common.value")}</th><th>{$i18n("sampling.min")}</th><th>{$i18n("sampling.max")}</th>
          <th>{$i18n("sampling.mean")}</th><th>{$i18n("sampling.count")}</th><th>{$i18n("sampling.time")}</th>
          <th title={$i18n("sampling.axisTip")}>{$i18n("sampling.axis")}</th>
          <th title={$i18n("sampling.methodTip")}>{$i18n("sampling.method")}</th><th></th>
        </tr></thead>
        <tbody>
          {#each rows as { e, last, st }, i (e.expr)}
            {@const how = status?.watches?.find((w: any) => w.name === e.expr)}
            {@const chaseError = h.chaseErrors[e.expr]}
            {@const rejected = h.rejected[e.expr]}
            <tr class:sel={selected === e.expr} class:off={!running.includes(e.expr)} class:rejected={!!rejected}
                onclick={() => (selected = e.expr)} ondblclick={() => last && openAt(last.time_us)}>
              <td><input type="checkbox" aria-label={$i18n("sampling.plot")} checked={e.plot}
                         onchange={(ev) => (list[i] = { ...e, plot: ev.currentTarget.checked })} /></td>
              <td class="mono" title={rejected ? $i18n("sampling.rejectedTip", { error: rejected }) : ""}>
                {#if rejected}<span class="bad">✕</span> {/if}{e.expr}</td>
              <td class="mono v" title={chaseError ? $i18n("sampling.chaseError", { error: chaseError }) : ""}>
                {#if chaseError}<span class="bad">!</span> {/if}<EditValue text={displayValue(last?.value)}
                  initial={last ? inputText(last.value) : ""} via={last ? via : null}
                  onwrite={(input) => writeExpr(e.expr, input)} /></td>
              <td class="mono">{st.count ? shortNumber(st.min) : "—"}</td>
              <td class="mono">{st.count ? shortNumber(st.max) : "—"}</td>
              <td class="mono">{st.count ? shortNumber(st.mean) : "—"}</td>
              <td class="mono">{st.count}</td>
              <td>{#if last}<TimeLink to={{ source: "recording", us: last.time_us }} fallback={TIMEBASE_ZERO} />{/if}</td>
              <td><button class="small" title={$i18n("sampling.axisTip")}
                          onclick={(ev) => { ev.stopPropagation(); list[i] = { ...e, axis: e.axis === "left" ? "right" : "left" }; }}>
                {$i18n(e.axis === "left" ? "sampling.left" : "sampling.right")}</button></td>
              <td class="muted" title={rejected ? rejected : $i18n("sampling.methodTip")}>
                {#if rejected}<span class="bad">{$i18n("sampling.rejected")}</span>
                {:else}{how ? $i18n(how.method === "host" ? "sampling.method.host" : "sampling.method.sampler") : ""}{/if}</td>
              <td><button class="small" aria-label={$i18n("common.remove")} title={$i18n("common.remove")}
                          onclick={(ev) => { ev.stopPropagation(); void remove(e.expr); }}>×</button></td>
            </tr>
          {/each}
        </tbody>
      </table>
    {/if}
    </div>

    <div class="right">
    <div class="pane-bar sub">
      <TabBar tabs={MODES.map((m) => ({ id: m, label: $i18n(`sampling.mode.${m}`) }))} bind:value={mode}
              ariaLabel={$i18n("sampling.mode")} />
      {#if mode === "time"}
        <label>{$i18n("sampling.window")}
          <select bind:value={windowS}>
            {#each WINDOWS_S as w (w)}<option value={w}>{w ? `${w} s` : $i18n("common.all")}</option>{/each}
          </select>
        </label>
      {:else if mode === "xy"}
        <label>{$i18n("sampling.xAxis")}
          <select bind:value={xName}><option value=""></option>
            {#each list as e (e.expr)}<option value={e.expr}>{e.expr}</option>{/each}</select></label>
        <label>{$i18n("sampling.yAxis")}
          <select bind:value={yName}><option value=""></option>
            {#each list as e (e.expr)}<option value={e.expr}>{e.expr}</option>{/each}</select></label>
        <label title={$i18n("sampling.maxDtTip")}>{$i18n("sampling.maxDt")}
          <input class="mono num" inputmode="decimal" bind:value={maxDtUs} placeholder="—" /></label>
      {:else if mode === "array"}
        <form class="inline" onsubmit={readArray}>
          <input class="mono expr" aria-label={$i18n("sampling.arrayExpression")} placeholder={$i18n("sampling.arrayExpression")}
                 title={$i18n("sampling.arrayHint")} bind:value={arrayExpr} />
          <input class="mono member" aria-label={$i18n("sampling.member")} placeholder={$i18n("sampling.member")}
                 title={$i18n("sampling.memberTip")} bind:value={member} />
          <button disabled={!halted || !arrayExpr.trim()} title={halted ? "" : $i18n("sampling.haltToRead")}>{$i18n("sampling.read")}</button>
        </form>
      {:else}
        <form class="inline" onsubmit={arm}>
          <span>{$i18n("sampling.trigger")}</span>
          <select bind:value={trig.variable} aria-label={$i18n("common.name")}>
            <option value="">{$i18n("sampling.triggerNow")}</option>
            {#each running as name (name)}<option value={name}>{name}</option>{/each}
          </select>
          {#if trig.variable}
            <select bind:value={trig.op} aria-label={$i18n("sampling.trigger")}>{#each OPS as op (op)}<option value={op}>{op}</option>{/each}</select>
            <input class="mono num" aria-label={$i18n("common.value")} aria-required="true" bind:value={trig.value} /><span
              class="req" aria-hidden="true">*</span>
            <label title={$i18n("sampling.edgeTip")}><input type="checkbox" bind:checked={trig.edge} /> {$i18n("sampling.edge")}</label>
          {/if}
          <label>{$i18n("sampling.pre")}<span class="req" aria-hidden="true">*</span> <input class="mono num" inputmode="decimal"
                 aria-required="true" bind:value={trig.preMs} /></label>
          <label title={$i18n("sampling.postTip")}>{$i18n("sampling.post")}<span class="req" aria-hidden="true">*</span> <input
                 class="mono num" inputmode="decimal" aria-required="true" bind:value={trig.postMs} /></label>
          <button title={$i18n("sampling.armTip")} disabled={!connected}>{$i18n("sampling.arm")}</button>
          {#if trigError}<span class="bad">{trigError}</span>{/if}
          {#if capture}
            <span class="muted">{capture.state === "done"
              ? $i18n("sampling.capture.done", { n: capture.count, reason: capture.reason ?? "" })
              : $i18n(capture.state === "armed" ? "sampling.capture.armed" : "sampling.capture.capturing")}</span>
            {#if capture.t_start_us !== null}
              <TimeLink to={{ source: "recording", us: capture.t_start_us, usEnd: capture.t_end_us ?? undefined }}
                        fallback={TIMEBASE_ZERO} />
            {/if}
          {/if}
        </form>
      {/if}
    </div>

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
        {#if wave}
          {#if wave.values.length < wave.length}
            <p class="muted small">{$i18n("sampling.arrayPartial", { n: wave.values.length, length: wave.length })}</p>
          {/if}
          <Chart series={waveSeries} height={200} xFormat={(i) => String(Math.round(i))} label={$i18n("sampling.mode.array")} />
          <div class="elements">
            <table class="grid mono">
              <thead><tr><th>{$i18n("sampling.index")}</th><th>{$i18n("common.value")}</th></tr></thead>
              <tbody>
                {#each wave.values as v, k (k)}
                  <tr><td class="muted">{k}</td>
                    <td><EditValue text={Number.isFinite(v) ? String(v) : "—"} {via}
                                   onwrite={(input) => writeExpr(elementExpr(waveSource.expr, k, waveSource.member), input)} /></td></tr>
                {/each}
              </tbody>
            </table>
          </div>
        {:else}
          <p class="empty">{waveError ?? $i18n(halted ? "sampling.arrayHint" : "sampling.haltToRead")}</p>
        {/if}
      {:else}
        {#if capSeries.length}
          <Chart series={capSeries} {cursor} height={200} xFormat={fmtX} label={$i18n("sampling.mode.capture")}
                 onopen={(x) => openAt(x)} onpick={pick} marks={abMarks} />
        {/if}
      {/if}
    </div>
    {#if measuring}
      <div class="measure">
        {#if ab.a === null}
          <span class="muted">{$i18n("sampling.cursorHint")}</span>
        {:else}
          <div class="pane-bar sub">
            <span>{$i18n("sampling.cursorA")} <strong class="mono">{at(ab.a)}</strong></span>
            {#if ab.b !== null}
              <span>{$i18n("sampling.cursorB")} <strong class="mono">{at(ab.b)}</strong></span>
              <span>{$i18n("sampling.delta")} <strong class="mono">{formatDuration((ab.b - ab.a) * 1e-6)}</strong></span>
            {:else}
              <span class="muted">{$i18n("sampling.cursorHint")}</span>
            {/if}
            <span class="spacer"></span>
            <button type="button" class="small" onclick={() => (ab = { a: null, b: null })}>{$i18n("sampling.clearCursors")}</button>
          </div>
          {#if measureError}<div class="error">{measureError}</div>{/if}
          {#if measured}
            <table class="grid">
              <thead>
                <tr><th>{$i18n("common.name")}</th><th>{$i18n("sampling.cursorA")}</th><th>{$i18n("sampling.cursorB")}</th>
                  <th>{$i18n("sampling.delta")}</th>
                  <th title={$i18n("sampling.between")}>{$i18n("sampling.min")}</th>
                  <th title={$i18n("sampling.between")}>{$i18n("sampling.max")}</th>
                  <th title={$i18n("sampling.between")}>{$i18n("sampling.mean")}</th>
                  <th title={$i18n("sampling.between")}>{$i18n("sampling.count")}</th></tr>
              </thead>
              <tbody>
                {#each measured.rows as r (r.name)}
                  <tr><td class="mono">{r.name}</td>
                    {#each [r.a, r.b] as v, k (k)}
                      <td class="mono">{v ? displayValue(v.value) : "—"}{#if v}<span class="muted small"> @ {at(v.time_us)}</span>{/if}</td>
                    {/each}
                    <td class="mono">{r.delta === null ? "—" : shortNumber(r.delta)}</td>
                    <td class="mono">{num(r.stats.min)}</td><td class="mono">{num(r.stats.max)}</td>
                    <td class="mono">{num(r.stats.mean)}</td><td class="mono">{r.stats.count}</td></tr>
                  {#if r.first_us === null || r.first_us > Math.min(measured.a_us, measured.b_us)}
                    <tr><td></td><td colspan="7" class="muted small">{$i18n("sampling.measureFrom",
                      { t: r.first_us === null ? "—" : at(r.first_us) })}</td></tr>
                  {/if}
                {/each}
              </tbody>
            </table>
          {/if}
        {/if}
      </div>
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
    .right .sub { border-top: none; }
  }
  .expr { flex: 1; min-width: 100px; max-width: 280px; }
  .member { width: 90px; }
  .num { width: 70px; }
  .sub { border-top: 1px solid var(--line); flex-wrap: wrap; }
  .inline { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
  .plot { padding: 4px 8px; }
  .measure { padding: 0 8px 6px; font-size: 12px; }
  .measure .sub { border-top: none; padding-left: 0; }
  .note { padding: 3px 8px; font-size: 12px; }
  .warn { color: var(--warn); }
  .bad { color: var(--bad); }
  .small { font-size: 12px; margin: 0 0 4px; }
  tr.sel td { background: var(--select); }
  tr.off td { opacity: 0.6; }
  tr.rejected td { opacity: 1; }
  button.on { border-color: var(--accent); color: var(--accent); }
  td.v { max-width: 260px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .elements { max-height: 180px; overflow: auto; margin-top: 4px; }
  .elements table { width: auto; min-width: 200px; }
</style>
