<script lang="ts">
  // Call Graph（IAR の Call Graph / Stack Usage・Keil の Call Graph の型）: ELF の静的な呼び出しの木（根 = リセット・例外・
  // 呼ばれない関数・関数ポインタの呼び先）、関数ごとの枠と呼び先を含む最大のスタック、最深の経路、再帰・関数ポインタ・
  // 可変長の確保の警告。Code Profile があれば自分の時間の割合も並べる。関数のクリックで Source・Disassembly。
  // ELF だけで動く（機器に触らない）
  import { errorText } from "../../lib/api";
  import {
    VTOR, deepestKeys, rootsOf, treeRows, vectorTableForm, vectorTableParams, vtorValue, type StackFunction, type StackRoot,
    type VectorTableForm,
  } from "../../lib/callgraph";
  import { shared } from "../../lib/context";
  import { onCalls } from "../../lib/notify";
  import { topCut } from "../../lib/cut";
  import { i18n, type Key } from "../../lib/i18n";
  import { SymbolAddresses } from "../../lib/profile";
  import { percent } from "../../lib/rtos";
  import Empty from "./Empty.svelte";
  import { hex32 } from "./format";

  const { app, conn, dbg, showAddress } = shared();
  const quiet = { quiet: true };
  // 読む上限（API の top）。切れたら切る前の数と上限を出す
  const STACK_TOP = 100000;        // debug.stack.static の関数
  const LOAD_TOP = 10000;          // profile.result の関数（割合の列）
  const symbols = new SymbolAddresses(conn);

  let report = $state<any>(null);
  let error = $state<string | null>(null);
  let open = $state<Set<string>>(new Set());
  let load = $state<Map<string, number>>(new Map());
  let loadCut = $state<ReturnType<typeof topCut>>(null);
  // ベクタ表: 欄（編集中）と、debug.stack.static に渡している値（適用したもの）。番地は利用者が明示する（VTOR は欄に
  // 入れる手助けだけ）
  let vtForm = $state<VectorTableForm>({ address: "", size: "" });
  let vtApplied = $state<VectorTableForm>({ address: "", size: "" });
  let vtError = $state<string | null>(null);

  const online = $derived($app.link === "open");
  const connected = $derived(!!$app.target?.state && $app.target.state !== "DISCONNECTED");
  const functions = $derived<StackFunction[]>(report?.functions ?? []);
  const stackCut = $derived(topCut(functions.length, report?.functions_total, STACK_TOP));
  const roots = $derived<StackRoot[]>(report ? rootsOf(report) : []);
  const rows = $derived(treeRows(functions, roots, open));

  // ELF が変わるたびに読む
  $effect(() => {
    if (online && $dbg.elf) void refresh();
  });
  $effect(() => onCalls(conn, ["profile.start", "profile.stop"], () => void readProfile()));

  async function refresh() {
    const vt = vectorTableParams(vtApplied);
    try {
      report = await conn.call("debug.stack.static", { top: STACK_TOP, ...("params" in vt ? vt.params : {}) }, quiet);
      error = null;
      vtForm = vectorTableForm(report.vector_table);   // 使った表（大きさは ELF から決まったものも）を欄に描く
      vtApplied = { ...vtForm };
      const reset = rootsOf(report)[0];                // API の並びの先頭の根（API はリセットを先頭に返す）の最深の経路を開く
      open = new Set(reset ? deepestKeys(reset).slice(0, -1) : []);
    } catch (e) {
      report = null;
      error = errorText(e);
    }
    await readProfile();
  }

  async function readProfile() {
    try {
      const r = await conn.call("profile.result", { by: "function", top: LOAD_TOP }, quiet);
      load = new Map(r.rows.map((x: any) => [x.function, x.fraction]));
      loadCut = topCut(r.rows.length, r.rows_total, LOAD_TOP);
    } catch {
      load = new Map();
      loadCut = null;
    }
  }

  /** 欄の値で解析し直す */
  async function applyVectorTable() {
    const vt = vectorTableParams(vtForm);
    if ("error" in vt) {
      vtError = $i18n(vt.error === "address" ? "callGraph.badAddress" : "callGraph.badSize");
      return;
    }
    vtError = null;
    vtApplied = { ...vtForm };
    await refresh();
  }

  /** VTOR を読んで番地の欄に入れる（使うのは適用してから） */
  async function readVtor() {
    const hex = await app.act("target.memory.read", { address: VTOR, length: 4 });
    if (hex === undefined) return;                     // 誤りは app.act が出した
    const value = vtorValue(hex);
    if (value === null) {
      vtError = $i18n("callGraph.badVtor");
      return;
    }
    vtError = null;
    vtForm = { address: hex32(value), size: "" };
  }

  function toggle(key: string) {
    const next = new Set(open);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    open = next;
  }

  function expandDeepest() {
    open = new Set([...open, ...roots.flatMap((r) => deepestKeys(r).slice(0, -1))]);
  }

  async function go(fn: StackFunction | null, name: string) {
    const address = fn?.address ?? (await symbols.of(name));
    if (address !== null && address !== undefined) showAddress(address);
  }

  const kindLabel = (k: string) => {
    const key = `callGraph.kind.${k}` as Key;
    const s = $i18n(key);
    return s === key ? k : s;
  };
</script>

<div class="pane graph">
  {#if online && $dbg.elf}
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div class="pane-bar vt" onkeydown={(e) => e.key === "Enter" && void applyVectorTable()}>
      <span title={$i18n("callGraph.vectorTableTip")}>{$i18n("callGraph.vectorTable")}</span>
      <input class="mono addr" aria-label={$i18n("callGraph.vectorAddress")} placeholder={$i18n("callGraph.vectorAddress")}
             bind:value={vtForm.address} spellcheck="false" />
      <input class="mono size" aria-label={$i18n("callGraph.vectorSize")}
             placeholder={`${$i18n("callGraph.vectorSize")}: ${$i18n("callGraph.vectorSizeHint")}`}
             title={$i18n("callGraph.vectorSize")} bind:value={vtForm.size} spellcheck="false" />
      <button class="small" disabled={!connected} title={$i18n("callGraph.readVtorTip")} onclick={readVtor}>
        {$i18n("callGraph.readVtor")}</button>
      <button class="small" onclick={applyVectorTable}>{$i18n("cond.apply")}</button>
      {#if vtError}<span class="bad">{vtError}</span>
      {:else if report?.vector_table}
        <span class="muted">{$i18n("callGraph.vectorUsed", { address: hex32(report.vector_table.address),
                                                            size: report.vector_table.size })}</span>
      {:else}<span class="muted">{$i18n("callGraph.vectorNone")}</span>{/if}
    </div>
  {/if}
  {#if report}
    <div class="pane-bar">
      <button class="small" onclick={expandDeepest} title={$i18n("callGraph.deepestTip")}>{$i18n("callGraph.openDeepest")}</button>
      {#if report.msp}
        <span class="muted" title={$i18n("callGraph.mspTip")}>{$i18n("callGraph.msp", { one: report.msp.one_level, all: report.msp.all_nested })}
          {#if !report.msp.bounded}<span class="warn">⚠</span>{/if}</span>
      {/if}
      {#if stackCut}<span class="muted">{$i18n("common.truncated", stackCut)}</span>{/if}
      {#if loadCut}<span class="muted">{$i18n("callGraph.loadTruncated", loadCut)}</span>{/if}
      <span class="spacer"></span>
      {#if report.notes?.length}<span class="muted note" title={report.notes.join("\n")}>ⓘ {report.notes[0]}</span>{/if}
      <button class="small" onclick={refresh}>{$i18n("common.refresh")}</button>
    </div>
    <table class="grid">
      <thead><tr><th>{$i18n("callGraph.function")}</th>
        <th title={$i18n("callGraph.frameTip")}>{$i18n("callGraph.frame")}</th>
        <th title={$i18n("callGraph.maxStackTip")}>{$i18n("callGraph.maxStack")}</th>
        <th title={$i18n("callGraph.loadTip")}>{$i18n("callGraph.load")}</th><th></th></tr></thead>
      <tbody>
        {#each rows as r (r.key)}
          <tr class:deepest={r.deepest}>
            <td class="mono fn" style:padding-left="{8 + r.depth * 16}px">
              {#if r.hasChildren}
                <button class="caret" aria-expanded={r.open} onclick={() => toggle(r.key)}>{r.open ? "▾" : "▸"}</button>
              {:else}<span class="caret"></span>{/if}
              <button class="link" onclick={() => go(r.fn, r.name)}>{r.name}</button>
              {#if r.root}<span class="tag">{kindLabel(r.root.kind)}{r.root.exceptions ? ` ${r.root.exceptions.join(",")}` : ""}</span>{/if}
              {#if r.deepest}<span class="muted" title={$i18n("callGraph.deepestTip")}>★</span>{/if}
            </td>
            <td class="mono">{r.fn ? `${r.fn.frame}` : "—"}<span class="muted">{r.fn?.frame_source === "su" ? " .su" : ""}</span></td>
            <td class="mono">{r.fn ? `${r.fn.max}${r.fn.bounded ? "" : "+"}` : "—"}
              {#if r.root?.total !== undefined}<span class="muted"> ({r.root.total})</span>{/if}</td>
            <td class="mono">{load.has(r.name) ? percent(load.get(r.name)) : ""}</td>
            <td>
              {#if r.recursive}<span class="warn" title={$i18n("callGraph.recursion")}>↻ {$i18n("callGraph.recursion")}</span>
              {:else if !r.fn}<span class="muted">{$i18n("callGraph.notInElf")}</span>
              {:else if !r.fn.bounded}
                <span class="warn" title={$i18n("callGraph.unbounded", { reasons: [...r.fn.reasons, ...r.fn.unknown_from].join(", ") })}>
                  ⚠ {r.fn.reasons[0] ?? r.fn.unknown_from.join(", ")}</span>
              {/if}
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
  {:else if error && $dbg.elf}
    <div class="empty"><strong>{$i18n("callGraph.none")}</strong><span>{$i18n("callGraph.noneHint", { error })}</span></div>
  {:else}
    <Empty need="elf" />
  {/if}
</div>

<style>
  .fn { white-space: nowrap; }
  .caret { display: inline-block; width: 14px; border: none; background: none; padding: 0; color: var(--muted); }
  .tag { font-size: 11px; color: var(--muted); border: 1px solid var(--line); border-radius: 4px; padding: 0 4px; margin-left: 6px; }
  .deepest td { background: color-mix(in srgb, var(--warn) 8%, transparent); }
  .warn { color: var(--warn); }
  .bad { color: var(--bad); }
  .vt input.addr { width: 11ch; }
  .vt input.size { width: 16ch; }
  .note { font-size: 12px; max-width: 50%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
</style>
