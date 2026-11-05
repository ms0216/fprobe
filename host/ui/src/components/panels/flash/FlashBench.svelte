<script lang="ts">
  // Flash の速度の計測（RAM・Flash の読み書き・消去・書き込みの速さ）
  import type { Result } from "../../../lib/api";
  import { frequency } from "../../../lib/format";
  import { parseNumber, seconds, size, speed, type FlashRun } from "../../../lib/flash";
  import { i18n, t, type Key } from "../../../lib/i18n";

  let { online, busy, run, onInvalid }: {
    online: boolean; busy: string | null; run: FlashRun; onInvalid: (message: string) => void;
  } = $props();

  let benchSector = $state("");
  let bench = $state<Result<"target.flash.benchmark"> | null>(null);

  async function benchmark() {
    const sector = benchSector.trim() ? parseNumber(benchSector) : null;
    if (benchSector.trim() && sector === null) {
      onInvalid(t("flash.addressHint"));
      return;
    }
    const r = await run("benchmark", "target.flash.benchmark", sector !== null ? { program_address: sector } : {});
    if (r) bench = r;
  }

  const benchRows = (b: Result<"target.flash.benchmark">): [Key, any][] => [
    ["flash.bench.ramWrite", b.ram?.write], ["flash.bench.ramRead", b.ram?.read], ["flash.bench.flashRead", b.flash_read],
    ["flash.bench.flashErase", b.flash_program?.erase], ["flash.bench.flashProgram", b.flash_program?.program],
    ["flash.bench.flashTotal", b.flash_program?.total],
  ].filter(([, r]) => r) as [Key, any][];
</script>

<details>
  <summary title={$i18n("flash.benchmarkTip")}>{$i18n("flash.benchmark")}</summary>
  <div class="row pad">
    <label>{$i18n("flash.benchSector")} <input class="mono num" bind:value={benchSector} /></label>
    <button class="small" disabled={!online || !!busy} title={$i18n("flash.benchmarkTip")} onclick={benchmark}>
      {$i18n("flash.benchRun")}</button>
    {#if bench?.frequency_hz}<span class="muted mono">{$i18n("flash.frequency", { f: frequency(bench.frequency_hz) })}</span>{/if}
  </div>
  {#if bench}
    <table class="grid">
      <thead><tr><th>{$i18n("flash.stage")}</th><th>{$i18n("flash.size")}</th><th>{$i18n("flash.time")}</th>
        <th>{$i18n("flash.speed")}</th></tr></thead>
      <tbody>
        {#each benchRows(bench) as [k, r] (k)}
          <tr><td>{$i18n(k)}</td><td class="mono">{size(r.bytes)}</td><td class="mono">{seconds(r.seconds)}</td>
            <td class="mono">{speed(r.bytes_per_s)}</td></tr>
        {/each}
      </tbody>
    </table>
  {/if}
</details>

<style>
  .row { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; min-width: 0; }
  .row.pad { padding: 4px 10px; }
  .num { width: 110px; }
  details { border-bottom: 1px solid var(--line); }
  summary { padding: 4px 10px; cursor: pointer; color: var(--muted); font-size: 12px; font-weight: 600; }
</style>
