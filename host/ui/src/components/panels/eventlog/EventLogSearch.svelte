<script lang="ts">
  // Event Log の「検索」: 条件の欄・範囲・結果の表（行の選択・移動・CSV）。表示だけ（検索・選択は親）
  import { fieldText, summary, type Condition, type RecEvent } from "../../../lib/eventlog";
  import { i18n } from "../../../lib/i18n";
  import ConditionFields from "../ConditionFields.svelte";
  import TimeLink from "../TimeLink.svelte";

  type Scope = "all" | "view" | "range";
  interface Props {
    cond: Condition;
    scope: Scope;
    streams: string[];
    streamsCut: { shown: number; limit: number } | null;
    viewRange: [number, number] | null;
    range: [number, number] | null;
    at: (t: number) => string;
    found: { count: number; events: RecEvent[] } | null;
    error: string | null;
    selected: number;
    box?: HTMLDivElement;
    onsearch: () => void;
    onclear: () => void;
    onstep: (dir: 1 | -1) => void;
    onchoose: (i: number, jump: boolean) => void;
    onkey: (e: KeyboardEvent) => void;
    onexport: () => void;
    onstart: () => void;
    onend: () => void;
  }
  let { cond = $bindable(), scope = $bindable(), streams, streamsCut, viewRange, range, at, found, error, selected,
        box = $bindable(), onsearch, onclear, onstep, onchoose, onkey, onexport, onstart, onend }: Props = $props();
</script>

<form class="pane-bar sub" onsubmit={(e) => { e.preventDefault(); onsearch(); }}>
  <ConditionFields bind:cond {streams} />
  {#if streamsCut}<span class="muted">{$i18n("eventLog.streamsTruncated", { n: streamsCut.shown, limit: streamsCut.limit })}</span>{/if}
  <select bind:value={scope} aria-label={$i18n("eventLog.scope")} title={$i18n("eventLog.scope")}>
    <option value="all">{$i18n("eventLog.scopeAll")}</option>
    <option value="view" disabled={!viewRange}>{$i18n("eventLog.scopeView")}</option>
    {#if range}<option value="range">{$i18n("eventLog.scopeRange", { from: at(range[0]), to: at(range[1]) })}</option>{/if}
  </select>
  <button type="submit" class="primary">{$i18n("eventLog.find")}</button>
  {#if found}<button type="button" class="small" onclick={onclear}>{$i18n("eventLog.clear")}</button>{/if}
</form>
{#if error}<div class="error">{error}</div>{/if}
{#if found}
  <div class="pane-bar sub">
    <span>{$i18n("eventLog.count", { n: found.count.toLocaleString() })}</span>
    {#if found.count > found.events.length}
      <span class="muted">{$i18n("eventLog.truncated", { n: found.events.length.toLocaleString() })}</span>
    {/if}
    <button class="small" disabled={!found.events.length} onclick={() => onstep(-1)} title={$i18n("eventLog.prevTip")}>
      {$i18n("eventLog.prev")}</button>
    <button class="small" disabled={!found.events.length} onclick={() => onstep(1)} title={$i18n("eventLog.nextTip")}>
      {$i18n("eventLog.next")}</button>
    {#if selected >= 0}<span class="muted">{selected + 1} / {found.events.length}</span>{/if}
    <span class="spacer"></span>
    <button class="small" onclick={onstart}>{$i18n("eventLog.useAsStart")}</button>
    <button class="small" onclick={onend}>{$i18n("eventLog.useAsEnd")}</button>
    <button class="small" disabled={!found.events.length} onclick={onexport}>CSV</button>
  </div>
  <div class="rows" bind:this={box} tabindex="0" onkeydown={onkey} role="grid" aria-label={$i18n("eventLog.search")}>
    <table class="grid">
      <thead><tr><th>{$i18n("eventLog.time")}</th><th>{$i18n("eventLog.stream")}</th><th>{$i18n("eventLog.kind")}</th><th>a</th><th>b</th>
        <th>{$i18n("eventLog.summary")}</th></tr></thead>
      <tbody>
        {#each found.events as e, i (i)}
          <tr data-row={i} class:selected={i === selected} onclick={() => onchoose(i, false)}
              ondblclick={() => onchoose(i, true)}>
            <td><TimeLink to={{ source: "recording", t: e.t, ...(e.t_end !== null && e.t_end > e.t ? { tEnd: e.t_end } : {}) }} /></td>
            <td class="mono">{e.stream}</td>
            <td class="mono">{e.kind}</td>
            <td class="mono num">{fieldText(e, "a")}</td>
            <td class="mono num">{fieldText(e, "b")}</td>
            <td class="sum" title={e.body ? JSON.stringify(e.body) : ""}>{summary(e)}</td>
          </tr>
        {:else}
          <tr><td colspan="6" class="muted">{$i18n("eventLog.noMatch")}</td></tr>
        {/each}
      </tbody>
    </table>
  </div>
{:else if !error}
  <p class="hint muted">{$i18n("eventLog.searchHint")}</p>
{/if}

<style>
  .sub { position: static; }
  .rows { flex: 1; overflow: auto; min-height: 60px; outline: none; }
  tr.selected td { background: var(--select); }
  tbody tr { cursor: default; }
  .num { text-align: right; }
  .sum { max-width: 40ch; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .error { color: var(--bad); padding: 4px 8px; font-size: 12.5px; }
  .hint { padding: 8px 12px; margin: 0; }
</style>
