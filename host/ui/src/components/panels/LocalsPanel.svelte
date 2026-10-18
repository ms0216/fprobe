<script lang="ts">
  // Locals: いまのフレームの引数とローカル変数（止まったときに読む）。構造体・配列・ポインタは木で開き、
  // 値のダブルクリックで書く
  import { shared } from "../../lib/context";
  import { i18n } from "../../lib/i18n";
  import type { VarNode } from "../../lib/vartree";
  import Empty from "./Empty.svelte";
  import VarTree from "./VarTree.svelte";

  const { dbg } = shared();
  const roots = $derived.by<VarNode[]>(() => {
    const l = $dbg.locals;
    if (!l) return [];
    return [...l.arguments.map((v) => ({ key: `a:${v.name}`, name: v.name, expr: v.name, type: v.type, value: v.value,
                                          tag: "locals.argument" as const })),
            ...l.locals.map((v) => ({ key: `l:${v.name}`, name: v.name, expr: v.name, type: v.type, value: v.value }))];
  });
</script>

<div class="pane" class:stale={$dbg.stale}>
  {#if $dbg.locals}
    {#if roots.length}
      <VarTree {roots} {dbg} gen={$dbg.gen} stops={$dbg.stops} writable={!!$dbg.stop && !$dbg.stale} />
    {:else}
      <p class="empty">{$i18n("locals.none")}{$dbg.locals.function ? ` — ${$dbg.locals.function}()` : ""}</p>
    {/if}
  {:else}
    <Empty need="halted" />
  {/if}
</div>
