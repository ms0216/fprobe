<script lang="ts">
  // Watch: C の式を足して、止まるたびに読む（構造体・配列・ポインタは木で開く。値のダブルクリックで書く）。
  // 走っている間は、このパネルが開いていれば 1 秒ごとに走らせたまま読む（ライブ ウォッチ。Ozone・CubeIDE の Live
  // Expressions。大域・静的の式）。つないでいなければ ELF だけで式と型を調べ、誤りをその行に出す。書き込みで中断（ウォッチポイント）
  import { shared } from "../../lib/context";
  import { allowed } from "../../lib/guard";
  import { i18n } from "../../lib/i18n";
  import type { VarNode } from "../../lib/vartree";
  import VarTree from "./VarTree.svelte";

  const { app, dbg } = shared();
  let name = $state("");
  const connected = $derived($app.target?.state !== undefined && $app.target?.state !== "DISCONNECTED");
  const roots = $derived<VarNode[]>($dbg.watches.map((w) => ({
    key: `w:${w.name}`, name: w.name, expr: w.name, type: w.type, value: w.value, error: w.error,
  })));
  const live = $derived(!$dbg.stop && $dbg.watchMode === "live");

  $effect(() => dbg.startLive());               // 開いている間だけ走らせたまま読む

  function add(e: Event) {
    e.preventDefault();
    void dbg.addWatch(name.trim());
    name = "";
  }
</script>

<div class="pane" class:stale={$dbg.stale && !live}>
  <form class="pane-bar" onsubmit={add}>
    <input aria-label={$i18n("watch.placeholder")} placeholder={$i18n("watch.placeholder")} bind:value={name} />
    <button disabled={!name.trim()}>{$i18n("watch.add")}</button>
    {#if live}
      <span class="mode live" title={$i18n("watch.liveTip")}>● {$i18n("watch.live")}</span>
    {:else if !$dbg.stop && $dbg.watchMode === "elf" && $dbg.watches.length}
      <span class="mode" title={$i18n("watch.elfOnlyTip")}>{$i18n("watch.elfOnly")}</span>
    {/if}
  </form>
  <VarTree {roots} {dbg} gen={$dbg.gen} stops={$dbg.stops} writable={!!$dbg.stop && !$dbg.stale}>
    {#snippet ops(node: VarNode)}
      <button class="small" title={$i18n("watch.watchpointTip")} disabled={!connected}
              onclick={async () => (await allowed(app)) && dbg.setWatchpoint(node.name)}>{$i18n("watch.watchpoint")}</button>
      <button class="small" aria-label={$i18n("common.remove")} title={$i18n("common.remove")}
              onclick={() => dbg.removeWatch(node.name)}>×</button>
    {/snippet}
  </VarTree>
</div>

<style>
  input { flex: 1; min-width: 80px; }
  .mode { font-size: 12px; color: var(--muted); white-space: nowrap; }
  .mode.live { color: var(--ok); }
</style>
