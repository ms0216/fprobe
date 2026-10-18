<script lang="ts">
  // Dashboard（STM32CubeMonitor のダッシュボード・µC/Probe の部品を並べた操作パネル・MPLAB の DMCI）: ゲージ・数値・
  // 棒・グラフ・ボタン・スライダ・切り替え・ランプを並べて変数を読む・書く。配置はブラウザに覚え、JSON で書き出す・
  // 読み込む（読み込みは lib/dashboard の validateLayout で検証して直す）。
  // 読み: 走行中は watch.values（Live で部品の式を観測に足す。Data Sampling と同じ観測を分け合う）、止まっているときは
  // debug.evaluate（止まるたび・書いた後）。書き: 走行中は watch.write、止まっているときは debug.write_expression。
  // 遠隔（--lan）の画面でも同じ（デーモンの API だけを使う）。配置はデーモンの画面の状態（ui.state の dashboard）で別の窓・
  // 遠隔の画面・AI と分け合う（lib/dashboard の SharedDashboard）
  import { untrack } from "svelte";
  import Chart from "../charts/Chart.svelte";
  import { download } from "../../lib/chart";
  import { shared } from "../../lib/context";
  import {
    addWidget, exampleLayout, expressionsOf, formatNumber, fraction, gaugeArc, KINDS, layoutJson, moveWidget, parseLayout,
    readValues, removeWidget, SharedDashboard, sliderStep, truthOf, updateWidget, writeCall, writeValue, MAX_H, MAX_W,
    type Layout, type Problem, type Reading, type Widget, type WidgetKind,
  } from "../../lib/dashboard";
  import { allowed } from "../../lib/guard";
  import { i18n, type Key } from "../../lib/i18n";
  import { load, save } from "../../lib/prefs";
  import { displayValue, numberOf, SampleStore } from "../../lib/sampling";
  import { axisTime, usSeconds } from "../../lib/time";
  import { missing, readWatch, startWatch, stopWatch } from "../../lib/watchshare";
  import Empty from "./Empty.svelte";

  const OWNER = "dashboard";
  const POLL_MS = 300, STATUS_MS = 1000, LIVE_RATE = 100;

  const { app, conn, tl } = shared();
  // グラフの横軸（タイムベースの µs）は記録の始めからの秒（記録が無ければタイムベースの 0 から）
  const origins = tl.origins;
  const fmtX = $derived(axisTime(usSeconds($origins), 1e-6));
  const call = conn.call.bind(conn);
  const act = app.act.bind(app);

  let layout = $state<Layout>(parseLayout(load("dashboard.layout", '{"version":1,"widgets":[]}')).layout);
  let editing = $state(false);
  let newKind = $state<WidgetKind>("gauge");
  let problems = $state<Problem[]>([]);
  let readings = $state(new Map<string, Reading>());
  let running = $state<string[]>([]);          // 観測している式（全員の）
  let live = $state(false);                    // このビューで Live にした
  let busy = $state(false);
  let gen = $state(0);
  let sliding = $state<Record<string, number>>({});
  let fileInput: HTMLInputElement;
  const history = new SampleStore(400);       // グラフの部品の値の列（走行中の読み）

  const target = $derived($app.target);
  const connected = $derived(!!target?.state && target.state !== "DISCONNECTED");
  const runningTarget = $derived(target?.state === "RUNNING" || target?.state === "SLEEPING");
  const halted = $derived(target?.state === "HALTED");
  const key = $derived(halted ? `${target?.pc}/${target?.run}` : "");
  const exprs = $derived(expressionsOf(layout));
  // 部品の式がすべて観測の組にある（一部だけなら Live で足りない式を足す）
  const sampled = $derived(exprs.length > 0 && exprs.every((e) => running.includes(e)));
  const lost = $derived(live ? missing(running, exprs) : []);

  $effect(() => save("dashboard.layout", JSON.stringify(layout)));

  // 配置をデーモンの画面の状態で分け合う（別の窓・遠隔の画面・AI が同じ操作パネルを見る）
  let sharedReady = false;
  const sharing = new SharedDashboard(conn, (checked) => {
    layout = checked.layout;
    problems = checked.problems;
  });
  $effect(() => () => sharing.dispose());
  $effect(() => {
    if ($app.link !== "open") {
      sharedReady = false;
      return;
    }
    void untrack(() => sharing.load(layout)).then(() => (sharedReady = true));
  });
  $effect(() => {
    const snapshot = $state.snapshot(layout) as Layout;
    if (untrack(() => sharedReady)) void sharing.save(snapshot);
  });

  // 止まるたびに読む（走行中の読みは下の周期）
  $effect(() => {
    if (key) untrack(() => void read());
  });
  // 配置の式が変わったら読み直す
  $effect(() => {
    void exprs;
    untrack(() => connected && !runningTarget && void read());
  });
  // 走行中: 値を周期的に読む
  $effect(() => {
    if (!runningTarget || !connected) return;
    const timer = setInterval(() => void read(), POLL_MS);
    return () => clearInterval(timer);
  });
  // 観測の組（Live の取り合い）を周期的に読む
  $effect(() => {
    if (!connected) return;
    void readRunning();
    const timer = setInterval(() => void readRunning(), STATUS_MS);
    return () => clearInterval(timer);
  });

  async function readRunning() {
    try {
      running = (await readWatch(call)).names;
    } catch {
      running = [];
    }
  }

  let reading = false;
  async function read() {
    const list = untrack(() => exprs);
    if (reading || !list.length || !untrack(() => connected)) return;
    reading = true;
    try {
      const isRunning = untrack(() => runningTarget);
      readings = await readValues(call, list, isRunning);
      if (isRunning) {
        for (const [e, r] of readings) if (r.sampled && r.time_us !== null) history.merge(e, [{ time_us: r.time_us, value: r.value }]);
        gen++;
      }
    } finally {
      reading = false;
    }
  }

  async function toggleLive() {
    if (!(await allowed(app))) return;
    busy = true;
    try {
      if (sampled) {
        await stopWatch(call, act, OWNER, exprs);
        live = false;
      } else {
        const now = await readWatch(call);
        live = await startWatch(call, act, OWNER, exprs, now.rate || LIVE_RATE);
      }
      await readRunning();
    } finally {
      busy = false;
    }
  }

  async function write(w: Widget, input?: number) {
    const value = writeValue(w, valueOf(w.expr), input);
    if (value === null || !w.expr || !(await allowed(app))) return;
    const { method, params } = writeCall(runningTarget, w.expr, value);
    await app.act(method, params);
    await read();
  }

  // --- 編集 ---------------------------------------------------------------------------------------

  function patch(id: string, p: Partial<Omit<Widget, "id">>) {
    layout = updateWidget(layout, id, p);
  }

  function exportLayout() {
    download("fprobe-dashboard.json", layoutJson(layout), "application/json");
  }

  async function importLayout(e: Event) {
    const file = (e.currentTarget as HTMLInputElement).files?.[0];
    if (!file) return;
    const checked = parseLayout(await file.text());
    problems = checked.problems;
    if (checked.layout.widgets.length || !checked.problems.length) layout = checked.layout;
    fileInput.value = "";
  }

  function problemText(p: Problem): string {
    const text = $i18n(`dashboard.problem.${p.code}` as Key, { detail: p.detail });
    return p.index === null ? text : $i18n("dashboard.problemAt", { n: p.index, text });
  }

  // --- 表示 ---------------------------------------------------------------------------------------

  const valueOf = (expr: string) => numberOf(readings.get(expr)?.value);
  const titleOf = (w: Widget) => w.label || w.expr || $i18n(`dashboard.kind.${w.kind}` as Key);
  function chartOf(w: Widget) {
    void gen;
    const p = history.points(w.expr);
    return [{ name: titleOf(w), x: p.x.slice(), y: p.y.slice(), step: true }];
  }
</script>

<div class="pane dash">
  <div class="pane-bar">
    <button class:on={editing} title={$i18n("dashboard.editTip")} onclick={() => (editing = !editing)}>
      {$i18n(editing ? "dashboard.done" : "dashboard.edit")}</button>
    {#if editing}
      <select bind:value={newKind} aria-label={$i18n("dashboard.addWidget")}>
        {#each KINDS as k (k)}<option value={k}>{$i18n(`dashboard.kind.${k}` as Key)}</option>{/each}
      </select>
      <button onclick={() => (layout = addWidget(layout, newKind))}>{$i18n("dashboard.addWidget")}</button>
      <button title={$i18n("dashboard.exampleTip")} onclick={() => (layout = exampleLayout())}>{$i18n("dashboard.example")}</button>
    {/if}
    <button class:on={sampled} disabled={busy || !connected || !exprs.length} title={$i18n("dashboard.liveTip")}
            onclick={toggleLive}>{$i18n(sampled ? "dashboard.stopLive" : "dashboard.live")}</button>
    <span class="spacer"></span>
    <button onclick={exportLayout} disabled={!layout.widgets.length}>{$i18n("dashboard.export")}</button>
    <button onclick={() => fileInput.click()}>{$i18n("dashboard.import")}</button>
    <input class="hidden" type="file" accept=".json,application/json" bind:this={fileInput} onchange={importLayout} />
  </div>
  {#if problems.length}
    <div class="note warn">
      <span>{$i18n("dashboard.problems")}</span>
      {#each problems as p, i (i)}<span>{problemText(p)}</span>{/each}
      <button class="small" onclick={() => (problems = [])}>{$i18n("common.close")}</button>
    </div>
  {/if}
  {#if lost.length}
    <div class="note warn">{$i18n("dashboard.missing", { names: lost.join(", ") })}</div>
  {/if}
  {#if !connected}
    <Empty need="connected" />
  {/if}

  {#if !layout.widgets.length}
    <div class="empty">
      <strong>{$i18n("dashboard.empty")}</strong><span>{$i18n("dashboard.emptyHint")}</span>
      <div class="actions">
        <button onclick={() => (editing = true)}>{$i18n("dashboard.edit")}</button>
        <button title={$i18n("dashboard.exampleTip")} onclick={() => (layout = exampleLayout())}>{$i18n("dashboard.example")}</button>
      </div>
    </div>
  {:else}
    <div class="board">
      {#each layout.widgets as w, i (w.id)}
        {@const r = readings.get(w.expr)}
        {@const v = valueOf(w.expr)}
        {@const notSampled = runningTarget && r !== undefined && !r.sampled}
        <div class="card" style:grid-column="span {w.w}" style:grid-row="span {w.h}" class:editing>
          <div class="title" title={w.expr}>{titleOf(w)}</div>
          <div class="body">
            {#if notSampled}
              <span class="muted" title={$i18n("dashboard.notSampledTip")}>{$i18n("dashboard.notSampled")}</span>
            {:else if r?.error}
              <span class="bad small" title={r.error}>{r.error}</span>
            {:else if w.kind === "gauge"}
              <svg viewBox="0 0 100 82" class="gauge" role="img" aria-label={titleOf(w)}>
                <path d={gaugeArc(1)} class="track" />
                <path d={gaugeArc(fraction(v, w))} class="fill" />
                <text x="50" y="54" text-anchor="middle" class="gv">{formatNumber(v, w)}</text>
                <text x="16" y="80" text-anchor="middle" class="gr">{w.min}</text>
                <text x="84" y="80" text-anchor="middle" class="gr">{w.max}</text>
              </svg>
            {:else if w.kind === "number"}
              <span class="big mono">{Number.isFinite(v) ? formatNumber(v, w) : displayValue(r?.value)}</span>
            {:else if w.kind === "bar"}
              <div class="bar"><span style:width="{(fraction(v, w) || 0) * 100}%"></span></div>
              <span class="mono">{formatNumber(v, w)}</span>
            {:else if w.kind === "chart"}
              <Chart series={chartOf(w)} yDomain={[w.min, w.max]} height={Math.max(60, w.h * 78 - 34)} legend={false} xFormat={fmtX}
                     label={titleOf(w)} />
            {:else if w.kind === "button"}
              <button class="primary" disabled={!connected || !w.expr}
                      title={$i18n("dashboard.writeTip", { expr: w.expr, value: w.value })}
                      onclick={() => void write(w)}>{w.label || w.value}</button>
            {:else if w.kind === "slider"}
              <input type="range" min={w.min} max={w.max} step={sliderStep(w)} disabled={!connected || !w.expr}
                     value={sliding[w.id] ?? (Number.isFinite(v) ? v : w.min)} aria-label={titleOf(w)}
                     title={$i18n("dashboard.sliderTip", { expr: w.expr })}
                     oninput={(e) => (sliding = { ...sliding, [w.id]: Number(e.currentTarget.value) })}
                     onchange={async (e) => {
                       await write(w, Number(e.currentTarget.value));
                       const { [w.id]: _, ...rest } = sliding;
                       sliding = rest;
                     }} />
              <span class="mono">{formatNumber(sliding[w.id] ?? v, w)}</span>
            {:else if w.kind === "toggle"}
              <button class="switch" class:on={truthOf(v) === true} role="switch" aria-checked={truthOf(v) === true}
                      aria-label={titleOf(w)} disabled={!connected || !w.expr || truthOf(v) === null}
                      title={$i18n("dashboard.toggleTip", { expr: w.expr })} onclick={() => void write(w)}><i></i></button>
            {:else if w.kind === "lamp"}
              <span class="lamp" class:lit={truthOf(v) === true} class:unknown={truthOf(v) === null}></span>
            {/if}
          </div>
          {#if editing}
            <div class="edit">
              <select value={w.kind} aria-label={$i18n("dashboard.addWidget")}
                      onchange={(e) => patch(w.id, { kind: e.currentTarget.value as WidgetKind })}>
                {#each KINDS as k (k)}<option value={k}>{$i18n(`dashboard.kind.${k}` as Key)}</option>{/each}
              </select>
              <input class="mono" placeholder={$i18n("dashboard.expression")} aria-label={$i18n("dashboard.expression")}
                     value={w.expr} onchange={(e) => patch(w.id, { expr: e.currentTarget.value.trim() })} />
              <input placeholder={$i18n("dashboard.label")} aria-label={$i18n("dashboard.label")} value={w.label}
                     onchange={(e) => patch(w.id, { label: e.currentTarget.value })} />
              {#if w.kind !== "button" && w.kind !== "toggle" && w.kind !== "lamp"}
                <label>{$i18n("dashboard.min")}<input type="number" value={w.min}
                       onchange={(e) => patch(w.id, { min: Number(e.currentTarget.value) })} /></label>
                <label>{$i18n("dashboard.max")}<input type="number" value={w.max}
                       onchange={(e) => { const max = Number(e.currentTarget.value); if (max > w.min) patch(w.id, { max }); }} /></label>
                <label>{$i18n("dashboard.unit")}<input value={w.unit} onchange={(e) => patch(w.id, { unit: e.currentTarget.value })} /></label>
                <label>{$i18n("dashboard.digits")}<input type="number" min="0" max="6" value={w.digits}
                       onchange={(e) => patch(w.id, { digits: Number(e.currentTarget.value) })} /></label>
              {/if}
              {#if w.kind === "button"}
                <label title={$i18n("dashboard.valueTip")}>{$i18n("dashboard.value")}<input class="mono" value={w.value}
                       onchange={(e) => patch(w.id, { value: e.currentTarget.value })} /></label>
              {/if}
              <label>{$i18n("dashboard.width")}<select value={w.w} onchange={(e) => patch(w.id, { w: Number(e.currentTarget.value) })}>
                {#each Array.from({ length: MAX_W }, (_, k) => k + 1) as n (n)}<option value={n}>{n}</option>{/each}</select></label>
              <label>{$i18n("dashboard.height")}<select value={w.h} onchange={(e) => patch(w.id, { h: Number(e.currentTarget.value) })}>
                {#each Array.from({ length: MAX_H }, (_, k) => k + 1) as n (n)}<option value={n}>{n}</option>{/each}</select></label>
              <span class="ops">
                <button class="small" disabled={i === 0} title={$i18n("dashboard.moveUp")} aria-label={$i18n("dashboard.moveUp")}
                        onclick={() => (layout = moveWidget(layout, w.id, -1))}>‹</button>
                <button class="small" disabled={i === layout.widgets.length - 1} title={$i18n("dashboard.moveDown")}
                        aria-label={$i18n("dashboard.moveDown")} onclick={() => (layout = moveWidget(layout, w.id, 1))}>›</button>
                <button class="small" title={$i18n("common.remove")} aria-label={$i18n("common.remove")}
                        onclick={() => (layout = removeWidget(layout, w.id))}>×</button>
              </span>
            </div>
          {/if}
        </div>
      {/each}
    </div>
  {/if}
</div>

<style>
  .hidden { display: none; }
  .note { display: flex; flex-wrap: wrap; gap: 4px 12px; align-items: center; padding: 3px 8px; font-size: 12px; }
  .warn { color: var(--warn); }
  .bad { color: var(--bad); }
  .small { font-size: 12px; }
  button.on { border-color: var(--accent); color: var(--accent); }
  .board { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); grid-auto-rows: minmax(78px, auto);
           gap: 8px; padding: 8px; }
  .card { border: 1px solid var(--line); border-radius: 6px; padding: 6px 8px; display: flex; flex-direction: column;
          min-width: 0; background: var(--panel); }
  .card.editing { border-style: dashed; }
  .title { font-size: 12px; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .body { flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 4px; min-height: 0; }
  .big { font-size: 22px; font-weight: 600; }
  .gauge { width: 100%; max-width: 160px; }
  .gauge .track { fill: none; stroke: var(--line); stroke-width: 9; stroke-linecap: round; }
  .gauge .fill { fill: none; stroke: var(--accent); stroke-width: 9; stroke-linecap: round; }
  .gauge .gv { fill: var(--text); font: 600 13px ui-monospace, Consolas, monospace; }
  .gauge .gr { fill: var(--muted); font: 8px ui-monospace, Consolas, monospace; }
  .bar { width: 100%; height: 12px; background: var(--line); border-radius: 6px; overflow: hidden; }
  .bar span { display: block; height: 100%; background: var(--accent); }
  input[type="range"] { width: 100%; }
  .switch { width: 44px; height: 24px; border-radius: 12px; padding: 0; position: relative; background: var(--line); }
  .switch i { position: absolute; top: 3px; left: 3px; width: 16px; height: 16px; border-radius: 50%; background: var(--panel);
              transition: left 0.1s; }
  .switch.on { background: var(--ok); border-color: var(--ok); }
  .switch.on i { left: 23px; }
  .lamp { width: 26px; height: 26px; border-radius: 50%; background: var(--line); border: 1px solid var(--line); }
  .lamp.lit { background: var(--ok); box-shadow: 0 0 10px var(--ok); }
  .lamp.unknown { background: none; border-style: dashed; }
  .edit { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 6px; border-top: 1px dashed var(--line); padding-top: 6px;
          font-size: 12px; }
  .edit input, .edit select { font-size: 12px; max-width: 100%; }
  .edit input:not([type]) { flex: 1 1 100px; }
  .edit input[type="number"] { width: 64px; }
  .edit label { display: flex; gap: 3px; align-items: center; }
  .ops { margin-left: auto; display: flex; gap: 3px; }
</style>
