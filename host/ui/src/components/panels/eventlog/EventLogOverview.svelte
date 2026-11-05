<script lang="ts">
  // Event Log の「概要」: 記録の stream・kind ごとの件数・割合・1 秒あたり・最初・最後（行から検索へ）。表示だけ（読みは親）
  import { shortNumber } from "../../../lib/chart";
  import type { KindStat } from "../../../lib/eventlog";
  import { i18n } from "../../../lib/i18n";
  import TimeLink from "../TimeLink.svelte";

  interface Props {
    overview: { rows: KindStat[]; total: number; max: number } | null;
    error: string | null;
    scope: "all" | "view";
    viewRange: [number, number] | null;
    oncount: () => void;
    onsearch: (k: KindStat) => void;
  }
  let { overview, error, scope = $bindable(), viewRange, oncount, onsearch }: Props = $props();
</script>

<div class="pane-bar sub">
  <select bind:value={scope} aria-label={$i18n("eventLog.scope")} title={$i18n("eventLog.scope")}>
    <option value="all">{$i18n("eventLog.scopeAll")}</option>
    <option value="view" disabled={!viewRange}>{$i18n("eventLog.scopeView")}</option>
  </select>
  <button class="small" onclick={oncount}>{$i18n("common.refresh")}</button>
  {#if overview}
    <span class="muted">{$i18n("eventLog.overviewTotal", { n: overview.total.toLocaleString(), kinds: overview.rows.length })}</span>
  {/if}
</div>
{#if error}<div class="error">{error}</div>{/if}
{#if overview}
  <div class="rows">
    <table class="grid">
      <thead><tr><th>{$i18n("eventLog.stream")}</th><th>{$i18n("eventLog.kind")}</th><th>{$i18n("eventLog.countCol")}</th>
        <th>{$i18n("eventLog.share")}</th><th>{$i18n("eventLog.perSecond")}</th><th>{$i18n("eventLog.first")}</th>
        <th>{$i18n("eventLog.last")}</th><th></th></tr></thead>
      <tbody>
        {#each overview.rows as k (`${k.stream}.${k.kind}`)}
          <tr ondblclick={() => onsearch(k)}>
            <td class="mono">{k.stream}</td>
            <td class="mono">{k.kind}</td>
            <td class="mono num bar" style:--w="{overview.max ? (k.count / overview.max) * 100 : 0}%">{k.count.toLocaleString()}</td>
            <td class="mono num">{(k.share * 100).toFixed(k.share < 0.01 ? 2 : 1)} %</td>
            <td class="mono num">{k.perSecond === null ? "—" : shortNumber(k.perSecond)}</td>
            <td><TimeLink to={{ source: "recording", t: k.first }} /></td>
            <td><TimeLink to={{ source: "recording", t: k.last }} /></td>
            <td><button class="small" title={$i18n("eventLog.searchKindTip")} onclick={() => onsearch(k)}>
              {$i18n("eventLog.searchKind")}</button></td>
          </tr>
        {:else}
          <tr><td colspan="8" class="muted">{$i18n("eventLog.noEvents")}</td></tr>
        {/each}
      </tbody>
    </table>
  </div>
{/if}

<style>
  .sub { position: static; }
  .rows { flex: 1; overflow: auto; min-height: 60px; outline: none; }
  tbody tr { cursor: default; }
  .num { text-align: right; }
  /* 件数の列: 最大に対する割合の棒を背景に */
  .bar { background: linear-gradient(to right, color-mix(in srgb, var(--accent) 22%, transparent) var(--w), transparent var(--w)); }
  .error { color: var(--bad); padding: 4px 8px; font-size: 12.5px; }
</style>
