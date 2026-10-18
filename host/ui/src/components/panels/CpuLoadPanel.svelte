<script lang="ts">
  // CPU Load（Tracealyzer の CPU Load Graph・SystemView の CPU Load / Runtime の型）: タスクの時間軸を取り始め・止め、
  // タスクごとの表（使用率・回数・合計・最長・最短・平均）と負荷の推移（窓ごと。タスク別は積み上げ = 上端が負荷）。
  // 取っていないが記録があれば、記録の rtos の区間から同じ表とグラフを作る。窓のダブルクリック・最大の窓で時間軸へ
  import { onDestroy } from "svelte";
  import { isNoData } from "../../lib/api";
  import type { Series } from "../../lib/chart";
  import { shared } from "../../lib/context";
  import SamplerNotice from "./SamplerNotice.svelte";
  import { allowed } from "../../lib/guard";
  import { i18n } from "../../lib/i18n";
  import { load, save } from "../../lib/prefs";
  import { onCalls, onRecording } from "../../lib/notify";
  import { Poller } from "../../lib/poll";
  import {
    loadFromTimeline, loadStack, loadWindows, percent, recordedRuns, recordedTable, tableFromTimeline, windowAt,
    type LoadSeries, type RecordedRuns, type TaskTable,
  } from "../../lib/rtos";
  import { axisTime, formatDuration, originFor, type Origin } from "../../lib/time";
  import Chart from "../charts/Chart.svelte";
  import LoadState from "./LoadState.svelte";
  import TimeLink from "./TimeLink.svelte";

  const { app, conn, tl, reveal } = shared();
  const quiet = { quiet: true };
  const WINDOWS = [1000, 10000, 100000];                    // µs

  let mode = $state<"timeline" | "recording" | null>(null);
  let method = $state(load("cpuLoad.method", "sampled"));
  let rate = $state(Number(load("cpuLoad.rate", "20000")));
  let exceptions = $state(load("cpuLoad.exceptions", "1") === "1");
  let windowUs = $state(Number(load("cpuLoad.window", "10000")));
  let byTask = $state(load("cpuLoad.byTask", "1") === "1");
  let table = $state<TaskTable | null>(null);
  let series = $state<LoadSeries | null>(null);
  let running = $state(false);
  let takenWith = $state("");                               // 取った方法（表示）
  let inRecording = $state(false);                          // 開いている記録のデータ（時刻の 0 を記録の始めに）
  let recording = $state<string | null>(null);
  let truncated = $state(0);
  let focus = $state<string | null>(null);                  // 表で選んだタスク（グラフで強調）
  let cached: { key: string; runs: RecordedRuns } | null = null;
  const poller = new Poller(refresh);
  onDestroy(() => poller.dispose());

  $effect(() => save("cpuLoad.method", method));
  $effect(() => save("cpuLoad.rate", String(rate)));
  $effect(() => save("cpuLoad.exceptions", exceptions ? "1" : "0"));
  $effect(() => save("cpuLoad.window", String(windowUs)));
  $effect(() => save("cpuLoad.byTask", byTask ? "1" : "0"));

  const online = $derived($app.link === "open");
  const connected = $derived(!!$app.target?.state && $app.target.state !== "DISCONNECTED");

  $effect(() => {
    void windowUs;
    if (online) void poller.refresh();
  });
  // 取っている間は読み続ける（前の読みが終わってから。重いほど間をあける）
  $effect(() => poller.poll(running));
  // ほかの利用者（AI も）がタスクの時間軸を始め・止めた、記録を開いた・始めた・止めたら読み直す
  $effect(() => onCalls(conn, ["rtos.timeline.start", "rtos.timeline.stop"], () => void poller.refresh()));
  $effect(() => onRecording(conn, () => void poller.refresh()));

  /** 時間軸（取っている・取った）か、無ければ開いている記録から。まだ無いのは空の状態、読めないのは誤り（Poller が出す） */
  async function refresh() {
    let g: any = null, l: any = null;
    try {
      g = await conn.call("rtos.timeline.get", { limit: 0 }, quiet);
      l = await conn.call("rtos.timeline.load", { window_us: windowUs }, quiet);
    } catch (e) {
      if (!isNoData(e)) throw e;
      g = null;
    }
    if (g) {
      table = tableFromTimeline(g);
      series = loadFromTimeline(l);
      running = !!g.running;
      takenWith = g.method === "itm" ? $i18n("cpuLoad.itm") : $i18n("cpuLoad.sampled");
      inRecording = g.window?.source === "recording";
      mode = "timeline";
      truncated = 0;
      return;
    }
    running = false;
    const st = await conn.call("recording.status", {}, quiet);
    const tab = st.name ? await recordedTable(conn) : null;
    if (tab) {
      const key = `${st.name}/${st.events}`;
      if (cached?.key !== key) cached = { key, runs: await recordedRuns(conn) };
      table = tab;
      series = loadWindows(cached.runs, Math.max(1, Math.round((windowUs * tab.hz) / 1e6)), [tab.t0, tab.t1]);
      truncated = cached.runs.truncated ? cached.runs.runs.length : 0;
      recording = st.name;
      inRecording = true;
      mode = "recording";
      return;
    }
    mode = null;
    table = null;
    series = null;
  }

  async function start() {
    if (!(await allowed(app))) return;
    const done = await app.act("rtos.timeline.start", { method: method as "sampled" | "itm", rate_hz: rate,
                                                        exceptions: method === "itm" && exceptions });
    if (done !== undefined) await poller.refresh();
  }

  async function stop() {
    if (!(await allowed(app))) return;
    const done = await app.act("rtos.timeline.stop", {});
    if (done !== undefined) await poller.refresh();
  }

  const hz = $derived(table?.hz ?? 1);
  // 時刻はタイムベース（グラフの横軸も）。表示は記録の始めからの秒（記録の外で取った時間軸なら、その始めから）
  const origins = tl.origins;
  const base = $derived<Origin>(originFor($origins, "recording", { t0: table?.t0 ?? 0, tickS: 1 / hz }, inRecording));
  const xFormat = $derived(axisTime((t) => (t - base.t0) * base.tickS, 1 / hz));
  // タスク別 = 積み上げ（タスク・割り込み・その他。上端が負荷）。そうでなければ負荷と割り込みの線
  const chartSeries = $derived.by((): Series[] => {
    const ws = series?.windows ?? [];
    if (!ws.length || !series) return [];
    if (byTask) return loadStack(series, (t) => t, { isr: $i18n("cpuLoad.isr"), other: $i18n("cpuLoad.other") }, focus);
    const x = ws.map((w) => w.t0);
    const pct = (v: number | null | undefined) => (v === null || v === undefined ? NaN : v * 100);
    const out: Series[] = [{ name: $i18n("cpuLoad.load"), x, y: ws.map((w) => pct(w.load)), step: true, unit: "%" }];
    if (ws.some((w) => w.isr)) out.push({ name: $i18n("cpuLoad.isr"), x, y: ws.map((w) => pct(w.isr)), step: true, unit: "%" });
    return out;
  });
  const cursor = $derived($tl.source === "recording" && $tl.selection !== null && table ? $tl.selection : null);
  const peak = $derived(series?.peak ?? null);

  // 最大の窓を時間軸に印として置く（閉じたら消す）
  $effect(() => {
    tl.setMarks("cpuLoad", "recording", peak ? [{ t: peak.t0, tEnd: peak.t1, tone: 3,
                                                 label: $i18n("cpuLoad.peak", { load: percent(peak.load) }) }] : []);
  });
  onDestroy(() => tl.setMarks("cpuLoad", "recording", []));

  function openAt(x: number) {
    if (!series) return;
    const w = windowAt(series, x);
    if (w) reveal({ source: "recording", t: w.t0, tEnd: w.t1 });
  }

  const dur = (ticks: number) => formatDuration(ticks / hz);
</script>

<div class="pane cpu">
  <SamplerNotice mine="rtos" />
  <div class="pane-bar">
    {#if running}
      <span class="live">● {$i18n("cpuLoad.live")}</span>
      <button disabled={!connected} onclick={stop}>{$i18n("cpuLoad.stop")}</button>
    {:else}
      <label title={$i18n("cpuLoad.methodTip")}>{$i18n("cpuLoad.method")}
        <select bind:value={method}>
          <option value="sampled">{$i18n("cpuLoad.sampled")}</option>
          <option value="itm">{$i18n("cpuLoad.itm")}</option>
        </select></label>
      {#if method === "sampled"}
        <label>{$i18n("cpuLoad.rate")}
          <select bind:value={rate}>
            {#each [5000, 10000, 20000, 50000] as r (r)}<option value={r}>{r / 1000} kHz</option>{/each}
          </select></label>
      {:else}
        <label title={$i18n("cpuLoad.exceptionsTip")}><input type="checkbox" bind:checked={exceptions} />
          {$i18n("cpuLoad.exceptions")}</label>
      {/if}
      <button class="primary" disabled={!connected || !$poller.loaded} title={connected ? $i18n("cpuLoad.startTip") : $i18n("hint.connectFirst")}
              onclick={start}>{$i18n("cpuLoad.start")}</button>
    {/if}
    {#if mode}
      <span class="muted">{mode === "timeline" ? $i18n("cpuLoad.fromTimeline", { method: takenWith })
                                              : $i18n("cpuLoad.fromRecording", { name: recording ?? "" })}</span>
    {/if}
    <span class="spacer"></span>
    <label>{$i18n("cpuLoad.window")}
      <select bind:value={windowUs}>
        {#each WINDOWS as w (w)}<option value={w}>{formatDuration(w / 1e6)}</option>{/each}
      </select></label>
    <label><input type="checkbox" bind:checked={byTask} /> {$i18n("cpuLoad.byTask")}</label>
  </div>

  <LoadState state={$poller} onretry={() => void poller.refresh()} />
  {#if mode && table && series}
    <div class="summary">
      {#if peak}
        <span>{$i18n("cpuLoad.peak", { load: percent(peak.load) })}</span>
        <TimeLink to={{ source: "recording", t: peak.t0, tEnd: peak.t1 }} origin={base} />
      {/if}
      <span class="muted">{$i18n("cpuLoad.mean", { load: percent(series.meanLoad) })}</span>
      {#if table.lowerBound}<span class="warn" title={$i18n("cpuLoad.lowerBound")}>⚠</span>{/if}
      {#if truncated}<span class="warn">{$i18n("cpuLoad.truncated", { n: truncated })}</span>{/if}
    </div>
    <div title={$i18n("cpuLoad.chartTip")}>
      <Chart type={byTask ? "stacked" : "line"} series={chartSeries} yDomain={[0, 100]} height={150} {cursor} {xFormat}
             yFormat={(v) => `${v}%`} onopen={openAt} label={$i18n("cpuLoad.load")} />
    </div>
    <table class="grid">
      <thead><tr><th>{$i18n("cpuLoad.task")}</th><th>{$i18n("cpuLoad.usage")}</th><th>{$i18n("cpuLoad.count")}</th>
        <th>{$i18n("cpuLoad.total")}</th><th>{$i18n("cpuLoad.max")}</th><th>{$i18n("cpuLoad.min")}</th>
        <th>{$i18n("cpuLoad.avg")}</th></tr></thead>
      <tbody>
        {#each table.tasks as row (row.handle)}
          <tr class="row" class:focus={focus === row.task} title={$i18n("cpuLoad.rowTip")}
              onclick={() => (focus = focus === row.task ? null : row.task)}>
            <td class="mono">{row.task}</td>
            <td class="mono usage"><span class="bar"><span style:width="{Math.min(100, row.cpu * 100)}%"></span></span>
              {percent(row.cpu)}</td>
            <td class="mono">{row.runs}</td>
            <td class="mono">{dur(row.total)}</td>
            <td class="mono">{dur(row.max)}</td>
            <td class="mono">{dur(row.min)}</td>
            <td class="mono">{dur(row.mean)}</td>
          </tr>
        {/each}
      </tbody>
    </table>
  {:else if $poller.loaded}
    <div class="empty">
      <strong>{$i18n("cpuLoad.none")}</strong>
      <span>{$i18n("cpuLoad.noneHint")}</span>
      {#if !connected}<span>{$i18n("hint.connectFirst")}</span>{/if}
    </div>
  {/if}
</div>

<style>
  .cpu label { display: inline-flex; gap: 4px; align-items: center; font-size: 12px; color: var(--muted); }
  .cpu select { font-size: 12px; padding: 1px 4px; }
  .live { color: var(--bad); font-size: 12px; }
  .summary { display: flex; gap: 10px; align-items: center; padding: 4px 8px; font-size: 12px; flex-wrap: wrap; }
  .warn { color: var(--warn); }
  .row { cursor: pointer; }
  .row:hover td, .row.focus td { background: var(--select); }
  .usage { white-space: nowrap; }
  .bar { display: inline-block; width: 70px; height: 6px; background: var(--line); border-radius: 3px; margin-right: 6px;
         vertical-align: middle; overflow: hidden; }
  .bar span { display: block; height: 100%; background: var(--accent); }
</style>
