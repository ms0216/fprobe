<script lang="ts">
  // Linux の perf（linux.perf.stat / record）。欄の値は API の引数のまま。呼び出しは親の run
  import { perfRecordParams, perfStatParams, type PerfForm } from "../../../lib/linux";
  import { i18n } from "../../../lib/i18n";
  import type { LinuxRun } from "./run";

  interface Props {
    target: string;
    perf: PerfForm;
    perfResult: Record<string, any> | null;
    run: LinuxRun;
    fmt: (v: unknown) => string;
  }
  let { target, perf = $bindable(), perfResult = $bindable(), run, fmt }: Props = $props();
</script>

<details>
  <summary>perf</summary>
  <div class="bar">
    <input class="mono short" bind:value={perf.pid} placeholder="pid" aria-label="pid" title={$i18n("linux.perfPidHint")} />
    <input class="mono num" bind:value={perf.duration} placeholder="s" aria-label="duration_s" title="duration_s" />
    <input class="mono" bind:value={perf.events} placeholder="task-clock cycles" aria-label="events" />
    <button class="small" onclick={async () => { perfResult = null; perfResult = (await run("linux.perf.stat", perfStatParams(target, perf), ["linux.perf", perf])) ?? null; }}>{$i18n("linux.perfStat")}</button>
  </div>
  <div class="bar">
    <input class="mono num" bind:value={perf.frequency} placeholder="Hz" aria-label="frequency_hz" title="frequency_hz" />
    <input class="mono num" bind:value={perf.maxSamples} placeholder="max" aria-label="max_samples" title={$i18n("linux.perfMaxHint")} />
    <button class="small" onclick={async () => { perfResult = null; perfResult = (await run("linux.perf.record", perfRecordParams(target, perf), ["linux.perf", perf])) ?? null; }}>{$i18n("linux.perfRecord")}</button>
  </div>
  {#if perfResult?.rows}
    <table class="mono small"><tbody>
      {#each perfResult.rows as row, i (i)}<tr><td>{row.event}</td><td>{fmt(row.value ?? row.counted)}</td><td>{row.unit}</td><td>{fmt(row.run_pct)} %</td></tr>{/each}
    </tbody></table>
  {:else if perfResult}
    <pre class="mono small">{JSON.stringify({ ...perfResult, stderr: undefined }, null, 1)}</pre>
  {/if}
  {#if perfResult?.stderr}<pre class="mono small muted">{perfResult.stderr}</pre>{/if}
</details>

<style>
  .bar { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
  .short { width: auto; field-sizing: content; min-width: 7em; }
  .num { width: auto; field-sizing: content; min-width: 4em; }
  .small { font-size: 12px; }
  table td { padding: 0 8px 0 0; vertical-align: top; }
  pre { margin: 2px 0; white-space: pre-wrap; }
</style>
