<script lang="ts">
  // Data Sampling のツールバー右の取得の状態（周期・巡回・取りこぼし・誤り）。値はデーモンの watch.status の事実をそのまま
  import { shortNumber } from "../../../lib/chart";
  import { i18n } from "../../../lib/i18n";
  import type { Health } from "../../../lib/sampling";

  let { h, status, connected }: { h: Health; status: any; connected: boolean } = $props();

  const pct = (r: number) => (r * 100).toFixed(r < 0.01 ? 2 : 1);
</script>

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

<style>
  .warn { color: var(--warn); }
  .bad { color: var(--bad); }
</style>
