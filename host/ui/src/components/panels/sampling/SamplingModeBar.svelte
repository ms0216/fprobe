<script lang="ts">
  // Data Sampling の右側の見出しの行: グラフの種類の切り替えと、種類ごとの欄（窓・XY の軸・配列の式・条件での取得）。
  // 値は親が持つ。表示だけ
  import { i18n } from "../../../lib/i18n";
  import { WINDOWS_S, type SampledExpr, type TriggerForm } from "../../../lib/sampling";
  import SamplingCaptureForm from "./SamplingCaptureForm.svelte";
  import TabBar from "../TabBar.svelte";

  interface Props {
    modes: readonly string[];
    mode: string;
    windowS: number;
    list: SampledExpr[];
    xName: string;
    yName: string;
    maxDtUs: string;
    arrayExpr: string;
    member: string;
    halted: boolean;
    trig: TriggerForm;
    trigError: string | null;
    capture: any;
    running: string[];
    connected: boolean;
    onread: (e: Event) => void;
    onarm: (e: Event) => void;
  }
  let { modes, mode = $bindable(), windowS = $bindable(), list, xName = $bindable(), yName = $bindable(), maxDtUs = $bindable(),
        arrayExpr = $bindable(), member = $bindable(), halted, trig = $bindable(), trigError, capture, running, connected,
        onread, onarm }: Props = $props();
</script>

<div class="pane-bar sub">
  <TabBar tabs={modes.map((m) => ({ id: m, label: $i18n(`sampling.mode.${m}` as never) }))} bind:value={mode}
          ariaLabel={$i18n("sampling.mode")} />
  {#if mode === "time"}
    <label>{$i18n("sampling.window")}
      <select bind:value={windowS}>
        {#each WINDOWS_S as w (w)}<option value={w}>{w ? `${w} s` : $i18n("common.all")}</option>{/each}
      </select>
    </label>
  {:else if mode === "xy"}
    <label>{$i18n("sampling.xAxis")}
      <select bind:value={xName}><option value=""></option>
        {#each list as e (e.expr)}<option value={e.expr}>{e.expr}</option>{/each}</select></label>
    <label>{$i18n("sampling.yAxis")}
      <select bind:value={yName}><option value=""></option>
        {#each list as e (e.expr)}<option value={e.expr}>{e.expr}</option>{/each}</select></label>
    <label title={$i18n("sampling.maxDtTip")}>{$i18n("sampling.maxDt")}
      <input class="mono num" inputmode="decimal" bind:value={maxDtUs} placeholder="—" /></label>
  {:else if mode === "array"}
    <form class="inline" onsubmit={onread}>
      <input class="mono expr" aria-label={$i18n("sampling.arrayExpression")} placeholder={$i18n("sampling.arrayExpression")}
             title={$i18n("sampling.arrayHint")} bind:value={arrayExpr} />
      <input class="mono member" aria-label={$i18n("sampling.member")} placeholder={$i18n("sampling.member")}
             title={$i18n("sampling.memberTip")} bind:value={member} />
      <button disabled={!halted || !arrayExpr.trim()} title={halted ? "" : $i18n("sampling.haltToRead")}>{$i18n("sampling.read")}</button>
    </form>
  {:else}
    <SamplingCaptureForm bind:trig {trigError} {capture} {running} {connected} {onarm} />
  {/if}
</div>

<style>
  .expr { flex: 1; min-width: 100px; max-width: 280px; }
  .member { width: 90px; }
  .num { width: 70px; }
  .sub { border-top: 1px solid var(--line); flex-wrap: wrap; }
  .inline { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
  @container (min-width: 900px) {
    .sub { border-top: none; }
  }
</style>
