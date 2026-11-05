<script lang="ts">
  // Event Log の「統計」: 区間の始め・終わりの条件、分布図、その棒の中の区間、長い順の上位。表示だけ（測りは親）
  import { describeCondition, type Condition, type Histogram, type Longest } from "../../../lib/eventlog";
  import type { Reveal } from "../../../lib/timeline/types";
  import { formatDuration } from "../../../lib/time";
  import { i18n } from "../../../lib/i18n";
  import Chart from "../../charts/Chart.svelte";
  import ConditionFields from "../ConditionFields.svelte";
  import Req from "../Req.svelte";
  import TimeLink from "../TimeLink.svelte";

  interface Props {
    startCond: Condition;
    endCond: Condition;
    binsText: string;
    longestText: string;
    picked: number | null;
    streams: string[];
    stats: any;
    error: string | null;
    canMeasure: boolean;
    hz: number;
    hist: Histogram[];
    longest: Longest[];
    bars: any[];
    inBin: [number, number][];
    inBinCut: { shown: number; total: number; limit: number } | null;
    dur: (ticks: number) => string;
    reveal: (r: Reveal) => void;
    onmeasure: () => void;
    onevents: (t: number, d: number) => void;
  }
  let { startCond = $bindable(), endCond = $bindable(), binsText = $bindable(), longestText = $bindable(), picked = $bindable(),
        streams, stats, error, canMeasure, hz, hist, longest, bars, inBin, inBinCut, dur, reveal, onmeasure, onevents }: Props = $props();
</script>

<form class="stats-form" onsubmit={(e) => { e.preventDefault(); onmeasure(); }}>
  <div><span class="label">{$i18n("eventLog.start")}</span><ConditionFields bind:cond={startCond} {streams} /></div>
  <div><span class="label">{$i18n("eventLog.end")}</span><ConditionFields bind:cond={endCond} {streams} /></div>
  <div>
    <label>{$i18n("eventLog.bins")}<input class="num" bind:value={binsText} inputmode="numeric" size="4"
      aria-required="true" /><Req /></label>
    <label>{$i18n("eventLog.longestN")}<input class="num" bind:value={longestText} inputmode="numeric" size="4"
      aria-required="true" /><Req /></label>
  </div>
  <div>
    <button type="submit" class="primary" disabled={!canMeasure}
            title={canMeasure ? "" : $i18n("eventLog.needBins")}>{$i18n("eventLog.measure")}</button>
    <span class="muted">{$i18n("eventLog.measureHint")}</span>
  </div>
</form>
{#if error}<div class="error">{error}</div>{/if}
{#if stats}
  {#if !stats.count}
    <p class="hint muted">{$i18n("eventLog.noIntervals")}</p>
  {:else}
    <div class="summary">
      <span class="muted">{describeCondition(startCond)} → {describeCondition(endCond)}</span>
      <table class="grid figures">
        <thead><tr><th>{$i18n("eventLog.countCol")}</th><th>{$i18n("eventLog.min")}</th><th>{$i18n("eventLog.max")}</th>
          <th>{$i18n("eventLog.mean")}</th><th>σ</th><th>{$i18n("eventLog.median")}</th><th>p90</th><th>p99</th></tr></thead>
        <tbody><tr>
          <td class="mono">{stats.count}</td>
          <td><TimeLink to={{ source: "recording", t: stats.min_at, tEnd: stats.min_at + stats.min }}>{dur(stats.min)}</TimeLink></td>
          <td><TimeLink to={{ source: "recording", t: stats.max_at, tEnd: stats.max_at + stats.max }}>{dur(stats.max)}</TimeLink></td>
          <td class="mono">{dur(stats.mean)}</td>
          <td class="mono">{dur(stats.stdev)}</td>
          <td class="mono">{dur(stats.median)}</td>
          <td class="mono">{dur(stats.p90)}</td>
          <td class="mono">{dur(stats.p99)}</td>
        </tr></tbody>
      </table>
    </div>
    <div class="chart">
      <Chart bars={bars} height={140} xFormat={formatDuration} label={$i18n("eventLog.histogram")}
             onpick={(_, bar) => (picked = bar)}
             onopen={(_, bar) => { const first = bar === null ? undefined : hist[bar]?.intervals[0];
                                   if (first) reveal({ source: "recording", t: first[0], tEnd: first[0] + first[1] }); }} />
    </div>
    <div class="lists">
      <div>
        <div class="muted head">{picked === null ? $i18n("eventLog.pickBin") : $i18n("eventLog.inBin", {
          from: formatDuration(hist[picked].lo / hz), to: formatDuration(hist[picked].hi / hz), n: hist[picked].count })}</div>
        <table class="grid">
          <tbody>
            {#each inBin as [t, d], i (i)}
              <tr ondblclick={() => reveal({ source: "recording", t, tEnd: t + d })}>
                <td><TimeLink to={{ source: "recording", t, tEnd: t + d }} /></td>
                <td class="mono">{dur(d)}</td>
                <td><button class="small" onclick={() => onevents(t, d)} title={$i18n("eventLog.eventsTip")}>
                  {$i18n("eventLog.events")}</button></td>
              </tr>
            {/each}
          </tbody>
        </table>
        {#if inBinCut}<div class="muted">{$i18n("common.truncated", inBinCut)}</div>{/if}
        {#if stats.intervals_total !== undefined}
          <div class="muted">{$i18n("eventLog.intervalsTruncated", { shown: stats.intervals.length, total: stats.intervals_total })}</div>
        {/if}
      </div>
      <div>
        <div class="muted head">{$i18n("eventLog.longest", { n: longest.length })}</div>
        <table class="grid">
          <tbody>
            {#each longest as o, i (i)}
              <tr ondblclick={() => reveal({ source: "recording", t: o.t, tEnd: o.t_end })}>
                <td><TimeLink to={{ source: "recording", t: o.t, tEnd: o.t_end }} /></td>
                <td class="mono">{dur(o.duration)}</td>
                <td><button class="small" onclick={() => onevents(o.t, o.duration)} title={$i18n("eventLog.eventsTip")}>
                  {$i18n("eventLog.events")}</button></td>
              </tr>
            {:else}
              <tr><td class="muted">{$i18n("eventLog.noLongest")}</td></tr>
            {/each}
          </tbody>
        </table>
      </div>
    </div>
  {/if}
{:else if !error}
  <p class="hint muted">{$i18n("eventLog.statsHint")}</p>
{/if}

<style>
  tbody tr { cursor: default; }
  .error { color: var(--bad); padding: 4px 8px; font-size: 12.5px; }
  .hint { padding: 8px 12px; margin: 0; }
  .stats-form { display: flex; flex-direction: column; gap: 4px; padding: 6px 8px; border-bottom: 1px solid var(--line); }
  .stats-form .label { display: inline-block; width: 4.5em; color: var(--muted); }
  .stats-form label { display: inline-flex; gap: 4px; align-items: center; color: var(--muted); margin-right: 8px; }
  .stats-form input.num { width: 5em; }
  .summary { padding: 4px 8px; }
  .figures { width: auto; }
  .chart { padding: 0 8px; }
  .lists { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; padding: 4px 8px; min-height: 0; }
  .lists .head { padding: 2px 0; }
</style>
