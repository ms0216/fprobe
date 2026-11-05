<script lang="ts">
  // Data Sampling の配列のグラフ（止まっているときに debug.evaluate で読んだ配列）と、要素の表（値のダブルクリックで書ける）
  import Chart from "../../charts/Chart.svelte";
  import type { Series } from "../../../lib/chart";
  import { i18n } from "../../../lib/i18n";
  import EditValue from "../EditValue.svelte";

  let { wave, waveSeries, waveError, halted, via, onwrite }: {
    wave: { values: number[]; length: number } | null; waveSeries: Series[]; waveError: string | null; halted: boolean;
    via: string | null; onwrite: (k: number, input: string) => Promise<string | null>;
  } = $props();
</script>

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
                           onwrite={(input) => onwrite(k, input)} /></td></tr>
        {/each}
      </tbody>
    </table>
  </div>
{:else}
  <p class="empty">{waveError ?? $i18n(halted ? "sampling.arrayHint" : "sampling.haltToRead")}</p>
{/if}

<style>
  .small { font-size: 12px; margin: 0 0 4px; }
  .elements { max-height: 180px; overflow: auto; margin-top: 4px; }
  .elements table { width: auto; min-width: 200px; }
</style>
