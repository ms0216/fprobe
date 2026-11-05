<script lang="ts">
  // Linux の「道 B」: 物理メモリのリング（linux.ring.*）と、止めずに読む口 memap（XSDB / JTAG）。
  // 欄の値は API の引数のまま。呼び出しは親の run（欄の誤りの表示・覚える・読み直し）
  import { memapParams, ringOpenParams, ringReadParams, ringWatchParams, type MemapForm, type RingForm } from "../../../lib/linux";
  import { memapJtagParams, type MemapJtagForm } from "../../../lib/linuxhalt";
  import { i18n } from "../../../lib/i18n";
  import Req from "../Req.svelte";
  import type { LinuxRun } from "./run";

  interface Props {
    target: string;
    ring: RingForm;
    memap: MemapForm;
    memapJtag: MemapJtagForm;
    ringResult: Record<string, any> | null;
    memapInfo: Record<string, any> | null;
    run: LinuxRun;
  }
  let { target, ring = $bindable(), memap = $bindable(), memapJtag = $bindable(), ringResult = $bindable(),
        memapInfo = $bindable(), run }: Props = $props();
</script>

<details>
  <summary>{$i18n("linux.ring")}</summary>
  <div class="bar">
    <input class="mono short" bind:value={ring.phys} placeholder="phys" aria-label="phys" title={$i18n("linux.ringPhys")} />
    <input class="mono short" bind:value={ring.size} placeholder="size" aria-label="size" />
    <label><input type="checkbox" bind:checked={ring.cached} /> cached</label>
    <label title={$i18n("linux.lingerHint")}><input type="checkbox" bind:checked={ring.linger} /> linger</label>
    <button class="small" onclick={() => void run("linux.ring.open", ringOpenParams(target, ring), ["linux.ring", ring])}>{$i18n("linux.ringOpen")}</button>
  </div>
  <div class="bar">
    <select bind:value={ring.access} aria-label="access"><option value="">access</option><option value="halt">halt</option><option value="memap">memap</option></select>
    <input class="mono short" bind:value={ring.address} placeholder="address" aria-label="address" title={$i18n("linux.ringAddress")} />
    <input class="mono num" bind:value={ring.maxRecords} placeholder="max" aria-label="max_records" title="max_records" />
    <button class="small" onclick={async () => { ringResult = null; ringResult = (await run("linux.ring.read", ringReadParams(target, ring), ["linux.ring", ring])) ?? null; }}>{$i18n("linux.ringRead")}</button>
    <input class="mono num" bind:value={ring.interval} placeholder="s" aria-label="interval_s" title="interval_s" />
    <button class="small" onclick={() => void run("linux.ring.watch", ringWatchParams(target, ring), ["linux.ring", ring])}>{$i18n("linux.ringWatch")}</button>
    <button class="small" onclick={async () => { ringResult = null; ringResult = (await run("linux.ring.unwatch", { params: { target } })) ?? null; }}>{$i18n("linux.ringUnwatch")}</button>
  </div>
  {#if ringResult}<pre class="mono small">{JSON.stringify({ ...ringResult, records: ringResult.records?.length }, null, 1)}</pre>{/if}
  <div class="bar">
    <strong>memap</strong>
    <input class="mono" bind:value={memap.xsdb} placeholder="xsdb" aria-label="xsdb" title={$i18n("linux.xsdb")} />
    <input class="mono short" bind:value={memap.cableSerial} placeholder="cable_serial" aria-label="cable_serial" />
    <input class="mono short" bind:value={memap.chainIdcode} placeholder="chain_idcode" aria-label="chain_idcode" />
    <input class="mono num" bind:value={memap.timeout} placeholder="s" aria-label="timeout_s" title="timeout_s" />
    <button class="small" onclick={async () => { memapInfo = null; memapInfo = (await run("linux.memap.connect", memapParams(target, memap), ["linux.memap", memap])) ?? null; }}>{$i18n("linux.connect")}</button>
    <button class="small" onclick={async () => { if ((await run("linux.memap.disconnect", { params: { target } })) !== undefined) memapInfo = null; }}>{$i18n("linux.disconnect")}</button>
  </div>
  <div class="bar">
    <strong>memap JTAG</strong>
    <input class="mono short" bind:value={memapJtag.chainIdcode} placeholder="chain_idcode" aria-label="chain_idcode" title={$i18n("linux.memapJtagIdcode")} aria-required="true" /><Req />
    <input class="mono num" bind:value={memapJtag.ap} placeholder="ap" aria-label="ap" title={$i18n("linux.memapJtagAp")} aria-required="true" /><Req />
    <input class="mono short" bind:value={memapJtag.frequencyHz} placeholder="frequency_hz" aria-label="frequency_hz" aria-required="true" /><Req />
    <button class="small" title={$i18n("linux.memapJtagHint")} onclick={async () => { memapInfo = null; memapInfo = (await run("linux.memap.connect_jtag", memapJtagParams(target, memapJtag), ["linux.memapJtag", memapJtag])) ?? null; }}>{$i18n("linux.connect")}</button>
  </div>
  {#if memapInfo}<div class="mono small">{JSON.stringify(memapInfo)}</div>{/if}
</details>

<style>
  .bar { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
  .short { width: auto; field-sizing: content; min-width: 7em; }
  .num { width: auto; field-sizing: content; min-width: 4em; }
  .small { font-size: 12px; }
  pre { margin: 2px 0; white-space: pre-wrap; }
</style>
