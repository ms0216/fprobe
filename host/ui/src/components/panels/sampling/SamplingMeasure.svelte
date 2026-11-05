<script lang="ts">
  // Data Sampling のカーソル A・B の測定: 2 点の値・差・間の統計。値はデーモンの watch.measure の応答をそのまま描く
  import { shortNumber } from "../../../lib/chart";
  import { i18n } from "../../../lib/i18n";
  import { displayValue, type ChartCursors } from "../../../lib/sampling";
  import { formatDuration } from "../../../lib/time";

  let { ab = $bindable(), measured, measureError, at }: {
    ab: ChartCursors; measured: any; measureError: string | null; at: (us: number) => string;
  } = $props();

  const num = (v: number | null) => (v !== null && Number.isFinite(v) ? shortNumber(v) : "—");
</script>

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
            {#if !r.covers_range_start}
              <tr><td></td><td colspan="7" class="muted small">{$i18n("sampling.measureFrom",
                { t: r.first_us === null ? "—" : at(r.first_us) })}</td></tr>
            {/if}
          {/each}
        </tbody>
      </table>
    {/if}
  {/if}
</div>

<style>
  .sub { border-top: 1px solid var(--line); flex-wrap: wrap; }
  .measure { padding: 0 8px 6px; font-size: 12px; }
  .measure .sub { border-top: none; padding-left: 0; }
  .small { font-size: 12px; margin: 0 0 4px; }
</style>
