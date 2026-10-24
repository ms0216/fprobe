<script lang="ts">
  // 右クリックのメニュー（lib/contextmenu.ts の contextMenu を描く）。項目に呼ぶ API の名前を出し、使えない項目は
  // 理由をツールチップにして灰色に。外をクリック・Escape・スクロールで閉じる。
  import { closeMenu, contextMenu, type MenuItem } from "../../lib/contextmenu";
  import { i18n } from "../../lib/i18n";

  let box = $state<HTMLDivElement | null>(null);
  // 窓の端からはみ出さないように寄せる（見せ方）
  const pos = $derived.by(() => {
    const m = $contextMenu;
    if (!m) return { left: 0, top: 0 };
    const w = box?.offsetWidth ?? 0, h = box?.offsetHeight ?? 0;
    return { left: Math.max(0, Math.min(m.x, window.innerWidth - w - 4)), top: Math.max(0, Math.min(m.y, window.innerHeight - h - 4)) };
  });

  function pick(item: MenuItem) {
    if (item.blocked) return;
    closeMenu();
    void item.run();
  }
</script>

<svelte:window onkeydown={(e) => e.key === "Escape" && $contextMenu && closeMenu()}
               onmousedown={(e) => $contextMenu && !box?.contains(e.target as Node) && closeMenu()}
               onblur={() => closeMenu()} onresize={() => closeMenu()} />

{#if $contextMenu}
  <div class="ctx" role="menu" tabindex="-1" bind:this={box} style:left="{pos.left}px" style:top="{pos.top}px"
       oncontextmenu={(e) => e.preventDefault()}>
    {#each $contextMenu.entries as item, k (k)}
      {#if item === "sep"}
        <hr />
      {:else}
        <button role="menuitem" disabled={!!item.blocked} title={item.blocked ? $i18n(item.blocked) : item.api ?? ""}
                onclick={() => pick(item)}>
          <span>{$i18n(item.label, item.params)}</span>
          <span class="side">{#if item.api}<span class="api mono">{item.api}</span>{/if}{#if item.shortcut}<span class="key">{item.shortcut}</span>{/if}</span>
        </button>
      {/if}
    {/each}
  </div>
{/if}

<style>
  .ctx { position: fixed; z-index: 60; min-width: 260px; background: var(--panel); border: 1px solid var(--line);
         border-radius: 6px; padding: 4px; box-shadow: 0 6px 20px rgba(0, 0, 0, 0.18); display: flex; flex-direction: column;
         font-size: 13px; }
  button { display: flex; justify-content: space-between; gap: 24px; border: none; background: none; padding: 4px 10px;
           border-radius: 4px; text-align: left; }
  button:hover:not(:disabled) { background: var(--select); }
  .side { display: flex; gap: 10px; }
  .api { color: var(--muted); font-size: 11px; }
  .key { color: var(--muted); font-size: 12px; }
  hr { border: none; border-top: 1px solid var(--line); margin: 4px 2px; width: auto; }
</style>
