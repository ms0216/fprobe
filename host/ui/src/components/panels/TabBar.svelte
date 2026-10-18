<script lang="ts" generics="T extends string">
  // パネルの中のタブ（全パネル共通。VS Code のパネルのタブと同じ下線の型）: Event Log の Search / Statistics / Notes、
  // Logic Analyzer の Decode / Trigger / Measure、Terminal の源、RTOS Objects の種類。
  // - label は表示する文字（訳した後。源の名前のように訳さないものもある）。count があれば薄く添える（件数）。
  // - title はツールチップ。ariaLabel はタブの列の名前。onselect は選んだとき（value の bind と別に動作が要るとき）
  // ←/→ で隣のタブへ（WAI-ARIA のタブの作法）
  let { tabs, value = $bindable(), ariaLabel, onselect }: {
    tabs: { id: T; label: string; count?: number; title?: string }[]; value: T | undefined; ariaLabel?: string;
    onselect?: (id: T) => void;
  } = $props();

  function choose(id: T) {
    value = id;
    onselect?.(id);
  }

  function onKey(e: KeyboardEvent, i: number) {
    const step = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
    if (!step || !tabs.length) return;
    e.preventDefault();
    const next = tabs[(i + step + tabs.length) % tabs.length];
    choose(next.id);
    ((e.currentTarget as HTMLElement).parentElement?.children[tabs.indexOf(next)] as HTMLElement | undefined)?.focus();
  }
</script>

<div class="tabs" role="tablist" aria-label={ariaLabel}>
  {#each tabs as t, i (t.id)}
    <button type="button" role="tab" class="tab" class:on={value === t.id} aria-selected={value === t.id}
            tabindex={value === t.id ? 0 : -1} title={t.title} onclick={() => choose(t.id)}
            onkeydown={(e) => onKey(e, i)}>{t.label}{#if t.count !== undefined}<span class="count">{t.count}</span>{/if}</button>
  {/each}
</div>

<style>
  .tabs { display: flex; gap: 2px; flex-wrap: wrap; min-width: 0; }
  .tab { border: none; border-bottom: 2px solid transparent; border-radius: 0; padding: 2px 8px; background: none;
         color: var(--muted); font-size: 12.5px; white-space: nowrap; }
  .tab.on { color: var(--text); border-bottom-color: var(--accent); }
  .tab:hover:not(.on) { color: var(--text); }
  .count { margin-left: 5px; color: var(--muted); font-size: 11px; }
</style>
