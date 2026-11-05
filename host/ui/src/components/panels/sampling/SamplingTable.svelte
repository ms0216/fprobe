<script lang="ts">
  // Data Sampling の式の表: 式ごとの最新の値・最小・最大・平均・件数・時刻（時間軸へのリンク）・方式。
  // 統計はデーモンが watch.set からの値すべてで数えたもの（watch.status の stats）をそのまま描く。
  // 最新の値は、ダブルクリックで書ける（debug.write_expression。道はデーモンが選ぶ）
  import { shortNumber } from "../../../lib/chart";
  import { i18n } from "../../../lib/i18n";
  import { displayValue, type Health, type Sample, type SampledExpr } from "../../../lib/sampling";
  import { TIMEBASE_ZERO } from "../../../lib/time";
  import { inputText } from "../../../lib/vartree";
  import EditValue from "../EditValue.svelte";
  import TimeLink from "../TimeLink.svelte";

  type Row = { e: SampledExpr; last: Sample | null; st: { count: number; min?: number; max?: number; mean?: number } };

  let { rows, status, h, running, via, list = $bindable(), selected = $bindable(), onopen, onremove, onwrite }: {
    rows: Row[]; status: any; h: Health; running: string[]; via: string | null;
    list: SampledExpr[]; selected: string | null;
    onopen: (us: number) => void; onremove: (expr: string) => void; onwrite: (expr: string, input: string) => Promise<string | null>;
  } = $props();
</script>

<table class="grid">
  <thead><tr>
    <th title={$i18n("sampling.plotTip")}>{$i18n("sampling.plot")}</th><th>{$i18n("common.name")}</th>
    <th>{$i18n("common.value")}</th><th>{$i18n("sampling.min")}</th><th>{$i18n("sampling.max")}</th>
    <th>{$i18n("sampling.mean")}</th><th>{$i18n("sampling.count")}</th><th>{$i18n("sampling.time")}</th>
    <th title={$i18n("sampling.axisTip")}>{$i18n("sampling.axis")}</th>
    <th title={$i18n("sampling.methodTip")}>{$i18n("sampling.method")}</th><th></th>
  </tr></thead>
  <tbody>
    {#each rows as { e, last, st }, i (e.expr)}
      {@const how = status?.watches?.find((w: any) => w.name === e.expr)}
      {@const chaseError = h.chaseErrors[e.expr]}
      {@const rejected = h.rejected[e.expr]}
      <tr class:sel={selected === e.expr} class:off={!running.includes(e.expr)} class:rejected={!!rejected}
          onclick={() => (selected = e.expr)} ondblclick={() => last && onopen(last.time_us)}>
        <td><input type="checkbox" aria-label={$i18n("sampling.plot")} checked={e.plot}
                   onchange={(ev) => (list[i] = { ...e, plot: ev.currentTarget.checked })} /></td>
        <td class="mono" title={rejected ? $i18n("sampling.rejectedTip", { error: rejected }) : ""}>
          {#if rejected}<span class="bad">✕</span> {/if}{e.expr}</td>
        <td class="mono v" title={chaseError ? $i18n("sampling.chaseError", { error: chaseError }) : ""}>
          {#if chaseError}<span class="bad">!</span> {/if}<EditValue text={displayValue(last?.value)}
            initial={last ? inputText(last.value) : ""} via={last ? via : null}
            onwrite={(input) => onwrite(e.expr, input)} /></td>
        <td class="mono">{st.count ? shortNumber(st.min!) : "—"}</td>
        <td class="mono">{st.count ? shortNumber(st.max!) : "—"}</td>
        <td class="mono">{st.count ? shortNumber(st.mean!) : "—"}</td>
        <td class="mono">{st.count}</td>
        <td>{#if last}<TimeLink to={{ source: "recording", us: last.time_us }} fallback={TIMEBASE_ZERO} />{/if}</td>
        <td><button class="small" title={$i18n("sampling.axisTip")}
                    onclick={(ev) => { ev.stopPropagation(); list[i] = { ...e, axis: e.axis === "left" ? "right" : "left" }; }}>
          {$i18n(e.axis === "left" ? "sampling.left" : "sampling.right")}</button></td>
        <td class="muted" title={rejected ? rejected : $i18n("sampling.methodTip")}>
          {#if rejected}<span class="bad">{$i18n("sampling.rejected")}</span>
          {:else}{how ? $i18n(how.method === "host" ? "sampling.method.host" : "sampling.method.sampler") : ""}{/if}</td>
        <td><button class="small" aria-label={$i18n("common.remove")} title={$i18n("common.remove")}
                    onclick={(ev) => { ev.stopPropagation(); onremove(e.expr); }}>×</button></td>
      </tr>
    {/each}
  </tbody>
</table>

<style>
  .bad { color: var(--bad); }
  tr.sel td { background: var(--select); }
  tr.off td { opacity: 0.6; }
  tr.rejected td { opacity: 1; }
  td.v { max-width: 260px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
</style>
