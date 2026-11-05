<script lang="ts">
  // Flash の領域と外付けのアルゴリズム（.FLM）。開いたときに領域を読み、アルゴリズムを足す・外す
  import { shared } from "../../../lib/context";
  import { algoParams, baseName, emptyAlgo, sectorsText, size, type AlgoForm, type FlashRun } from "../../../lib/flash";
  import type { Result } from "../../../lib/api";
  import { i18n, t } from "../../../lib/i18n";
  import { hex32 } from "../format";

  let { online, busy, run, onInvalid, onNote }: {
    online: boolean; busy: string | null; run: FlashRun; onInvalid: (message: string) => void; onNote: (message: string) => void;
  } = $props();

  const { conn } = shared();

  let regions = $state<Result<"target.flash.regions">["regions"] | null>(null);
  let algos = $state<NonNullable<Result<"target.flash.regions">["algorithms"]>>([]);  // target.flash.algo.add で足したアルゴリズム
  let algo = $state<AlgoForm>(emptyAlgo());

  async function loadRegions() {
    try {
      const r = await conn.call("target.flash.regions", {}, { quiet: true });
      regions = r.regions;
      algos = r.algorithms ?? [];
    } catch {
      regions = null;
    }
  }

  /** 外付けのアルゴリズム（.FLM）で Flash の領域を足す（未接続なら覚えておき、接続後の最初の操作で足す） */
  async function addAlgo() {
    let params;
    try {
      params = algoParams(algo);
    } catch (e) {
      onInvalid((e as Error).message === "flm" ? t("flash.needFlm") : t("flash.numberHint"));
      return;
    }
    const r = await run("algo", "target.flash.algo.add", params);
    if (r) {
      onNote("pending" in r && r.pending ? `${t("flash.algoAdded", { name: r.name })} · ${t("flash.algoPending")}`
                       : t("flash.algoAdded", { name: r.name }));
      algo = emptyAlgo();
      await loadRegions();
    }
  }

  async function removeAlgo(name: string) {
    if (await run("algo", "target.flash.algo.remove", { name })) await loadRegions();
  }
</script>

<details ontoggle={(e) => (e.currentTarget as HTMLDetailsElement).open && loadRegions()}>
  <summary>{$i18n("flash.regions")}</summary>
  {#if regions}
    <table class="grid">
      <thead><tr><th>{$i18n("common.name")}</th><th>{$i18n("flash.start")}</th><th>{$i18n("flash.size")}</th>
        <th>{$i18n("flash.sectors")}</th><th>{$i18n("flash.algorithm")}</th><th>{$i18n("flash.boot")}</th></tr></thead>
      <tbody>
        {#each regions as r (r.name)}
          <tr class:muted={!r.usable}>
            <td class="mono">{r.name}</td><td class="mono">{hex32(r.start)}</td><td class="mono">{size(r.size)}</td>
            <td class="mono">{sectorsText(r.sectors)}</td><td class="mono">{r.algorithm}</td><td>{r.boot ? "✓" : ""}</td>
          </tr>
        {/each}
      </tbody>
    </table>
  {/if}
  <div class="sub" title={$i18n("flash.algosTip")}>{$i18n("flash.algos")}</div>
  {#if algos.length}
    <table class="grid">
      <thead><tr><th>{$i18n("common.name")}</th><th>FLM</th><th>{$i18n("flash.start")}</th><th>{$i18n("flash.size")}</th>
        <th></th></tr></thead>
      <tbody>
        {#each algos as a (a.name)}
          <tr>
            <td class="mono">{a.name}</td><td class="mono">{baseName(a.flm)}</td>
            <td class="mono">{a.address !== null && a.address !== undefined ? hex32(a.address) : "-"}</td>
            <td class="mono">{size(a.size)}</td>
            <td><button class="small" disabled={!online || !!busy} onclick={() => removeAlgo(a.name)}>
              {$i18n("common.remove")}</button></td>
          </tr>
        {/each}
      </tbody>
    </table>
  {/if}
  <form class="row pad" onsubmit={(e) => { e.preventDefault(); void addAlgo(); }}>
    <input class="mono grow" bind:value={algo.flm} placeholder={$i18n("flash.algoFlm")} aria-label={$i18n("flash.algoFlm")}
           spellcheck="false" />
    <input class="mono num" bind:value={algo.address} placeholder={$i18n("flash.address")} aria-label={$i18n("flash.address")} />
    <input class="mono len" bind:value={algo.size} placeholder={$i18n("flash.size")} aria-label={$i18n("flash.size")} />
    <input class="mono num" bind:value={algo.ramAddress} placeholder={$i18n("flash.ramAddress")}
           aria-label={$i18n("flash.ramAddress")} />
    <input class="mono len" bind:value={algo.ramSize} placeholder={$i18n("flash.ramSize")} aria-label={$i18n("flash.ramSize")} />
    <input class="len" bind:value={algo.name} placeholder={$i18n("common.name")} aria-label={$i18n("common.name")} />
    <button class="small" disabled={!online || !algo.flm.trim() || !!busy} title={$i18n("flash.algosTip")}>
      {$i18n("common.add")}</button>
  </form>
</details>

<style>
  .row { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; min-width: 0; }
  .row.pad { padding: 4px 10px; }
  .grow { flex: 1; min-width: 120px; }
  .num { width: 110px; }
  .len { width: 70px; }
  details { border-bottom: 1px solid var(--line); }
  summary { padding: 4px 10px; cursor: pointer; color: var(--muted); font-size: 12px; font-weight: 600; }
  .sub { padding: 6px 10px 2px; color: var(--muted); font-size: 12px; }
</style>
