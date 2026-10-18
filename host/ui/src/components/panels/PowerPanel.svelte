<script lang="ts">
  // Power（J-Link の Power・Ozone の Power Graph に当たる）: いまのターゲットの電圧（probe.voltage を周期的に読む）の
  // 窓の推移と最小・最大・平均（止める・消す・CSV）、記録の電圧（stream aux・kind mv）のグラフと電圧の低下の一覧
  // （ダブルクリック・最低の点で時間軸へ、低下を時間軸の印に、時間軸の選択をグラフの縦線に）。
  // 電流は追加のハード（INA226）が要るので、理由を出すだけ（作らない）
  import { errorText } from "../../lib/api";
  import { untrack } from "svelte";
  import Chart from "../charts/Chart.svelte";
  import { download, type Series } from "../../lib/chart";
  import { shared } from "../../lib/context";
  import { i18n } from "../../lib/i18n";
  import { onRecording } from "../../lib/notify";
  import { load, save } from "../../lib/prefs";
  import { findDips, formatVolts, measuredVolts, minIndex, recordingVolts, SPANS_S, VoltageWindow, type VoltageReading }
    from "../../lib/power";
  import { axisTime, formatDuration, formatTick, secondsOf } from "../../lib/time";
  import { onTimeline } from "../../lib/timeline/events";
  import TimeLink from "./TimeLink.svelte";

  const OWNER = "power";
  const POLL_MS = 500, PAGE_LIMIT = 20;         // 記録の電圧は 1000 件ずつ、20 回まで
  const THRESHOLDS_MV = [20, 50, 100, 200];

  const { app, conn, tl, reveal } = shared();

  let spanS = $state(Number(load("power.span", "60")) || 60);
  let thresholdMv = $state(Number(load("power.threshold", "50")) || 50);
  let paused = $state(false);
  let latest = $state<VoltageReading | null>(null);
  let readError = $state<string | null>(null);
  let gen = $state(0);
  let origin = 0;
  const win = new VoltageWindow();              // 長さは下の $effect で spanS に合わせる

  let rec = $state<{ name: string; hz: number } | null>(null);
  let recVolts = $state<{ x: number[]; y: number[] } | null>(null);
  let recTotal = $state(0);
  let cursor = $state<number | null>(null);

  const linkOpen = $derived($app.link === "open");
  const aux = $derived($app.probe?.capabilities ? Boolean($app.probe.capabilities.aux) : true);

  $effect(() => {
    save("power.span", String(spanS));
    save("power.threshold", String(thresholdMv));
    win.setSpan(spanS);
    untrack(() => gen++);
  });

  // いまの電圧を周期的に読む（止めている間は読まない）
  $effect(() => {
    if (!linkOpen || !aux || paused) return;
    void readVoltage();
    const timer = setInterval(() => void readVoltage(), POLL_MS);
    return () => clearInterval(timer);
  });

  async function readVoltage() {
    try {
      const r: VoltageReading = await conn.call("probe.voltage", {}, { quiet: true });
      const now = Date.now();
      if (!win.length) origin = now;
      win.add(now, r);
      latest = r;
      readError = null;
      gen++;
    } catch (e) {
      readError = errorText(e);
    }
  }

  function clear() {
    win.clear();
    latest = null;
    gen++;
  }

  const stats = $derived.by(() => {
    void gen;
    return win.stats();
  });
  const liveSeries = $derived.by((): Series[] => {
    void gen;
    return win.length ? [win.series($i18n("power.voltage"), origin)] : [];
  });

  // 記録の電圧（つながったら・だれかが記録を開いた・始めた・止めたら読み直す）
  $effect(() => {
    if (!linkOpen) return;
    untrack(() => void readRecording());
    return onRecording(conn, () => void readRecording());
  });

  async function readRecording() {
    try {
      const s = await conn.call("recording.status", {}, { quiet: true });
      if (!s?.name) {
        rec = null;
        recVolts = null;
        return;
      }
      rec = { name: s.name, hz: Number(s.timebase_hz) || 0 };
      const events: { t: number; b: number; kind: string }[] = [];
      let t0: number | undefined;
      for (let page = 0; page < PAGE_LIMIT; page++) {
        const r = await conn.call("recording.find", { query: { stream: "aux", kind: "mv", ...(t0 !== undefined ? { t0 } : {}) } },
                                  { quiet: true });
        if (page === 0) recTotal = r?.count ?? 0;
        const got = r?.events ?? [];
        events.push(...got);
        if (got.length < 1000) break;
        t0 = got[got.length - 1].t + 1;
      }
      recVolts = recordingVolts(events);
    } catch {
      rec = null;
      recVolts = null;
    }
  }

  const dips = $derived(recVolts ? findDips(recVolts.x, recVolts.y, thresholdMv / 1000) : []);
  const lowest = $derived(recVolts ? minIndex(recVolts.y) : -1);
  const recSeries = $derived<Series[]>(recVolts?.x.length ? [{ name: $i18n("timeline.row.aux.voltage"), x: recVolts.x, y: recVolts.y, unit: "V" }] : []);
  // 横軸はタイムベース。表示は記録の始めからの秒（lib/time.ts）
  const origins = tl.origins;
  const tickS = $derived(rec?.hz ? 1 / rec.hz : 0);
  const recFormat = $derived(axisTime(secondsOf($origins, "recording", { t0: recVolts?.x[0] ?? 0, tickS }), tickS));

  // 低下を時間軸の印に
  $effect(() => {
    tl.setMarks(OWNER, "recording", dips.map((d) => ({ t: d.t, tEnd: d.tEnd, label: `-${Math.round(d.drop * 1000)} mV`, tone: 3 as const })));
  });
  $effect(() => () => tl.setMarks(OWNER, "recording", []));

  // 時間軸の 1 回クリック: 記録のグラフの縦線
  $effect(() => onTimeline({ select: (d) => {
    if (d.source === "recording") cursor = d.t;
  } }));

  const openAt = (t: number, tEnd?: number) => reveal({ source: "recording", t, tEnd });
</script>

<div class="pane power">
  <div class="pane-bar">
    <strong>{$i18n("power.voltage")}</strong>
    <span class="big mono">{measuredVolts(latest) !== null ? formatVolts(measuredVolts(latest)!) : "—"}</span>
    {#if latest}
      {#if latest.over_range}<span class="badge bad" title={$i18n("power.overRangeTip")}>{$i18n("power.overRange")}</span>
      {:else if !latest.live}<span class="badge warn" title={$i18n("power.notLiveTip")}>{$i18n("power.notLive")}</span>
      {:else}<span class="badge ok">{$i18n("power.live")}</span>{/if}
    {/if}
    <span class="spacer"></span>
    <label>{$i18n("sampling.window")}
      <select bind:value={spanS}>
        {#each SPANS_S as s (s)}<option value={s}>{s >= 60 ? `${s / 60} min` : `${s} s`}</option>{/each}
      </select>
    </label>
    <button class:on={paused} onclick={() => (paused = !paused)}>{$i18n(paused ? "sampling.resume" : "sampling.pause")}</button>
    <button onclick={clear}>{$i18n("sampling.clear")}</button>
    <button disabled={!win.length} onclick={() => download("fprobe-voltage.csv", win.csv(), "text/csv")}>{$i18n("sampling.exportCsv")}</button>
  </div>

  {#if !aux}
    <div class="empty"><strong>{$i18n("power.noAux")}</strong><span>{$i18n("power.noAuxHint")}</span></div>
  {:else}
    {#if readError}<div class="note bad">{$i18n("power.readError", { error: readError })}</div>{/if}
    <div class="stats mono">
      <span>{$i18n("sampling.min")} {formatVolts(stats.min)}</span>
      <span>{$i18n("sampling.max")} {formatVolts(stats.max)}</span>
      <span>{$i18n("sampling.mean")} {formatVolts(stats.mean)}</span>
      <span class="muted">{$i18n("power.readings", { n: stats.count })}</span>
      {#if stats.overRange}<span class="bad">{$i18n("power.overRange")} × {stats.overRange}</span>{/if}
      {#if stats.notLive}<span class="warn">{$i18n("power.notLive")} × {stats.notLive}</span>{/if}
    </div>
    <div class="plot">
      <Chart series={liveSeries} height={140} xFormat={formatTick} yFormat={(v) => v.toFixed(3)}
             label={$i18n("power.voltage")} />
    </div>
  {/if}
  <div class="note muted" title={$i18n("power.currentWhy")}>
    <strong>{$i18n("power.current")}</strong> — {$i18n("power.currentWhy")}
  </div>

  <div class="pane-bar sub">
    {#if rec}
      <strong>{$i18n("power.recording", { name: rec.name })}</strong>
      {#if recVolts && recVolts.x.length < recTotal}
        <span class="muted">{$i18n("power.truncated", { n: recVolts.x.length, total: recTotal })}</span>
      {/if}
    {:else}
      <strong>{$i18n("power.noRecording")}</strong>
    {/if}
    <span class="spacer"></span>
    {#if recVolts?.x.length}
      <label title={$i18n("power.dipThresholdTip")}>{$i18n("power.dipThreshold")}
        <select bind:value={thresholdMv}>{#each THRESHOLDS_MV as m (m)}<option value={m}>{m} mV</option>{/each}</select>
      </label>
      <button disabled={lowest < 0} title={$i18n("power.revealLowestTip")}
              onclick={() => recVolts && openAt(recVolts.x[lowest])}>{$i18n("power.revealLowest")}</button>
    {/if}
    <button onclick={() => void readRecording()}>{$i18n("common.refresh")}</button>
  </div>
  {#if !rec}
    <p class="empty">{$i18n("power.noRecordingHint")}</p>
  {:else if !recVolts?.x.length}
    <p class="empty">{$i18n("power.noRecordingVolts")}</p>
  {:else}
    <div class="plot" title={$i18n("sampling.chartTip")}>
      <Chart series={recSeries} height={160} {cursor} xFormat={recFormat} yFormat={(v) => v.toFixed(3)}
             label={$i18n("power.recording", { name: rec.name })} onopen={(x) => openAt(Math.round(x))} />
    </div>
    <table class="grid">
      <thead><tr><th>{$i18n("power.dips")}</th><th>{$i18n("power.lowest")}</th><th>{$i18n("power.drop")}</th>
        <th>{$i18n("power.duration")}</th></tr></thead>
      <tbody>
        {#each dips as d (d.t)}
          <tr ondblclick={() => openAt(d.t, d.tEnd)}>
            <td><TimeLink to={{ source: "recording", t: d.at }} /></td>
            <td class="mono">{formatVolts(d.min)}</td>
            <td class="mono">{Math.round(d.drop * 1000)} mV</td>
            <td class="mono">{tickS ? formatDuration((d.tEnd - d.t) * tickS) : d.tEnd - d.t}</td>
          </tr>
        {:else}
          <tr><td colspan="4" class="muted">{$i18n("power.noDips")}</td></tr>
        {/each}
      </tbody>
    </table>
  {/if}
</div>

<style>
  .big { font-size: 16px; font-weight: 600; }
  .badge { font-size: 11px; border: 1px solid currentColor; border-radius: 8px; padding: 0 6px; }
  .ok { color: var(--ok); }
  .warn { color: var(--warn); }
  .bad { color: var(--bad); }
  .stats { display: flex; flex-wrap: wrap; gap: 4px 16px; padding: 4px 8px; font-size: 12.5px; }
  .plot { padding: 4px 8px; }
  .note { padding: 3px 8px; font-size: 12px; }
  .sub { border-top: 1px solid var(--line); }
  button.on { border-color: var(--accent); color: var(--accent); }
</style>
