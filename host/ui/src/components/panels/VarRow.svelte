<script lang="ts">
  // 変数の木の 1 行と、開いていればその子（自分を入れ子に使う）。構造体・配列の子は値の中から、ポインタの先は
  // 開いたときに `*式` を読む。値をダブルクリックすると書ける（基本型・列挙・ポインタ）。
  import type { Snippet } from "svelte";
  import type { Debugger } from "../../lib/debugger";
  import { i18n } from "../../lib/i18n";
  import { childrenOf, derefExpr, display, editable, expandable, inputText, kindOf, parseInput, pointerChildren, structType,
           pointeeType, type VarNode } from "../../lib/vartree";
  import type { Seen } from "./VarTree.svelte";
  import VarRow from "./VarRow.svelte";

  let { node, depth = 0, dbg, open, seen, gen, stops, writable, ops }:
    { node: VarNode; depth?: number; dbg: Debugger; open: Set<string>; seen: Map<string, Seen>; gen: number; stops: number;
      writable: boolean; ops?: Snippet<[VarNode]> } = $props();

  const MORE_LIMIT = 4096;               // debug.evaluate の max_elements の上限
  const isOpen = $derived(open.has(node.key));
  let editing = $state<string | null>(null);
  let members = $state<Record<string, string>>({});
  let target = $state<{ type?: string; value: unknown } | null>(null);     // ポインタの先
  let targetMembers = $state<Record<string, string>>({});
  let more = $state<unknown>(undefined);                                    // 多い配列の続きを読んだもの
  let loadError = $state<string | null>(null);
  let changed = $state(false);

  const value = $derived(more ?? node.value);
  const shown = $derived({ ...node, value });
  const kind = $derived(kindOf(value));
  const pending = $derived(value === undefined && !node.error);           // まだ読んでいない（止まる前の Watch）
  const text = $derived(node.error ? $i18n("watch.error") : pending ? "—" : display(value, 64, node.type));
  const canOpen = $derived(!node.error && expandable(value));
  const children = $derived(!isOpen ? [] : kind === "pointer"
    ? (target ? pointerChildren(shown, target, targetMembers) : [])
    : childrenOf(shown, members));

  // 前に止まったときの値と違えば赤く（ステップで変わった値。VS Code と同じ）。止まっている間の読み直し（書き込み）で
  // 変わった値にも印を付け、付けた印は次に止まるまで保つ
  $effect(() => {
    if (pending) return;
    const prev = seen.get(node.key);
    if (prev && prev.stops !== stops) changed = prev.text !== text;
    else if (prev && prev.text !== text) changed = true;
    seen.set(node.key, { text, stops });
  });
  // 値が読み直されたら（gen）多い配列の続きは捨てる
  $effect(() => {
    void gen;
    more = undefined;
  });
  // 構造体のメンバーの型
  $effect(() => {
    const type = isOpen && kind === "struct" ? structType(node.type) : null;
    if (type) void dbg.typeMembers(type).then((m) => (members = m));
  });
  // ポインタの先（開いている間、止まり直すたび）
  $effect(() => {
    void gen;
    if (!isOpen || kind !== "pointer" || !node.expr) return;
    dbg.evaluate(derefExpr(node.expr)).then(async (r) => {
      target = r;
      loadError = null;
      const type = structType(r.type ?? pointeeType(node.type));
      targetMembers = type && kindOf(r.value) === "struct" ? await dbg.typeMembers(type) : {};
    }).catch((e) => {
      target = null;
      loadError = String((e as Error)?.message ?? e);
    });
  });

  function toggle() {
    if (isOpen) open.delete(node.key);
    else open.add(node.key);
  }

  function startEdit() {
    if (writable && editable(shown)) editing = inputText(value);
  }

  async function commit() {
    if (editing === null || !node.expr) return;
    const input = editing;
    editing = null;
    if (input.trim() !== "" && input !== inputText(value)) await dbg.writeExpression(node.expr, parseInput(input));
  }

  function onKey(e: KeyboardEvent) {
    if (e.key === "Enter") {
      e.preventDefault();
      void commit();
    } else if (e.key === "Escape") {
      e.preventDefault();
      editing = null;
    }
  }

  async function loadMore() {
    if (!node.expr) return;
    const length = (value as { length: number }).length;
    try {
      more = (await dbg.evaluate(node.expr, Math.min(length, MORE_LIMIT))).value;
    } catch (e) {
      loadError = String((e as Error)?.message ?? e);
    }
  }

  const focus = (el: HTMLInputElement) => {
    el.focus();
    el.select();
  };
</script>

<tr class:changed>
  <td class="name" style:padding-left="{8 + depth * 14}px" title={node.expr ?? node.name}>
    {#if canOpen}
      <button class="caret" aria-expanded={isOpen} aria-label={isOpen ? $i18n("vars.collapse") : $i18n("vars.expand")}
              onclick={toggle}>{isOpen ? "▾" : "▸"}</button>
    {:else}<span class="caret"></span>{/if}<span>{node.name}</span>{#if node.tag}<span class="muted tag">{$i18n(node.tag)}</span>{/if}
  </td>
  <!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
  <td class="v" class:bad={!!node.error} class:muted={kind === "unavailable"} class:can-edit={writable && editable(shown)}
      title={node.error ?? (writable && editable(shown) ? $i18n("vars.editTip") : text)} ondblclick={startEdit}>
    {#if editing !== null}
      <input class="mono edit" aria-label={$i18n("vars.edit")} bind:value={editing} use:focus onkeydown={onKey}
             onblur={() => (editing = null)} />
    {:else}{text}{/if}
  </td>
  <td class="type"><span class="muted">{node.type ?? ""}</span>
    {#if ops && depth === 0}<span class="ops">{@render ops(node)}</span>{/if}</td>
</tr>
{#if isOpen}
  {#each children as child (child.key)}
    <VarRow node={child} depth={depth + 1} {dbg} {open} {seen} {gen} {stops} {writable} {ops} />
  {/each}
  {#if kind === "truncated" && more === undefined}
    <tr><td style:padding-left="{22 + (depth + 1) * 14}px" colspan="3">
      <button class="link" onclick={loadMore}>{$i18n("vars.more", { n: (value as { length: number }).length })}</button>
    </td></tr>
  {/if}
  {#if loadError}
    <tr><td class="bad" style:padding-left="{22 + (depth + 1) * 14}px" colspan="3">{loadError}</td></tr>
  {/if}
{/if}

<style>
  .name { white-space: nowrap; }
  .tag { margin-left: 6px; font-size: 11.5px; }
  .caret { display: inline-block; width: 14px; border: none; background: none; padding: 0; color: var(--muted);
           font-size: 11px; cursor: pointer; }
  td { overflow: hidden; text-overflow: ellipsis; }
  .v { white-space: nowrap; }
  .can-edit { cursor: text; }
  .type { white-space: nowrap; font-size: 12px; position: relative; overflow: visible; }
  .type > .muted { display: block; overflow: hidden; text-overflow: ellipsis; }
  .bad { color: var(--bad); }
  .changed .v { color: var(--bad); }
  .edit { width: 100%; padding: 0 4px; }
  /* Watch の行の操作: 行に乗ったときだけ、型の列の右に重ねる */
  .ops { position: absolute; right: 4px; top: 50%; transform: translateY(-50%); display: none; gap: 4px;
         background: var(--panel); padding-left: 6px; }
  tr:hover .ops, tr:focus-within .ops { display: flex; }
</style>
