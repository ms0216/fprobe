<script lang="ts" module>
  export interface Seen { text: string; stops: number }
</script>

<script lang="ts">
  // 変数の木（Watch・Locals が使う）: 名前・値・型の表。構造体・配列・ポインタは ▸ で開く（開いた行は覚える）。
  // 止まっているとき、値のダブルクリックで書ける。
  import type { Snippet } from "svelte";
  import { SvelteSet } from "svelte/reactivity";
  import type { Debugger } from "../../lib/debugger";
  import { i18n } from "../../lib/i18n";
  import type { VarNode } from "../../lib/vartree";
  import VarRow from "./VarRow.svelte";

  let { roots, dbg, gen, stops, writable, ops }:
    { roots: VarNode[]; dbg: Debugger; gen: number; stops: number; writable: boolean; ops?: Snippet<[VarNode]> } = $props();

  const open = new SvelteSet<string>();       // 開いている行（木を読み直しても保つ）
  const seen = new Map<string, Seen>();       // 行ごとの前回の値の文字（変わった値に印）
</script>

<table class="grid mono vars">
  <colgroup><col class="c-name" /><col class="c-value" /><col class="c-type" /></colgroup>
  <thead><tr><th>{$i18n("common.name")}</th><th>{$i18n("common.value")}</th><th>{$i18n("common.type")}</th></tr></thead>
  <tbody>
    {#each roots as node (node.key)}
      <VarRow {node} {dbg} {open} {seen} {gen} {stops} {writable} {ops} />
    {/each}
  </tbody>
</table>

<style>
  .vars { table-layout: fixed; }
  .c-name { width: 34%; }
  .c-value { width: 42%; }
  .c-type { width: 24%; }
</style>
