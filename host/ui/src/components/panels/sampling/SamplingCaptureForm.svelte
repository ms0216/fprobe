<script lang="ts">
  // Data Sampling の「条件での取得」の入力（watch.trigger）: 条件（変数・比較・値・エッジ）・前後の時間・状態
  import Req from "../Req.svelte";
  import { i18n } from "../../../lib/i18n";
  import { OPS, type TriggerForm } from "../../../lib/sampling";
  import { TIMEBASE_ZERO } from "../../../lib/time";
  import TimeLink from "../TimeLink.svelte";

  let { trig = $bindable(), trigError, capture, running, connected, onarm }: {
    trig: TriggerForm; trigError: string | null; capture: any; running: string[]; connected: boolean; onarm: (e: Event) => void;
  } = $props();
</script>

<form class="inline" onsubmit={onarm}>
  <span>{$i18n("sampling.trigger")}</span>
  <select bind:value={trig.variable} aria-label={$i18n("common.name")}>
    <option value="">{$i18n("sampling.triggerNow")}</option>
    {#each running as name (name)}<option value={name}>{name}</option>{/each}
  </select>
  {#if trig.variable}
    <select bind:value={trig.op} aria-label={$i18n("sampling.trigger")}>{#each OPS as op (op)}<option value={op}>{op}</option>{/each}</select>
    <input class="mono num" aria-label={$i18n("common.value")} aria-required="true" bind:value={trig.value} /><span
      class="req" aria-hidden="true">*</span>
    <label title={$i18n("sampling.edgeTip")}><input type="checkbox" bind:checked={trig.edge} /> {$i18n("sampling.edge")}</label>
  {/if}
  <label>{$i18n("sampling.pre")}<Req /> <input class="mono num" inputmode="decimal"
         aria-required="true" bind:value={trig.preMs} /></label>
  <label title={$i18n("sampling.postTip")}>{$i18n("sampling.post")}<Req /> <input
         class="mono num" inputmode="decimal" aria-required="true" bind:value={trig.postMs} /></label>
  <button title={$i18n("sampling.armTip")} disabled={!connected}>{$i18n("sampling.arm")}</button>
  {#if trigError}<span class="bad">{trigError}</span>{/if}
  {#if capture}
    <span class="muted">{capture.state === "done"
      ? $i18n("sampling.capture.done", { n: capture.count, reason: capture.reason ?? "" })
      : $i18n(capture.state === "armed" ? "sampling.capture.armed" : "sampling.capture.capturing")}</span>
    {#if capture.t_start_us !== null}
      <TimeLink to={{ source: "recording", us: capture.t_start_us, usEnd: capture.t_end_us ?? undefined }}
                fallback={TIMEBASE_ZERO} />
    {/if}
  {/if}
</form>

<style>
  .inline { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
  .num { width: 70px; }
  .bad { color: var(--bad); }
</style>
