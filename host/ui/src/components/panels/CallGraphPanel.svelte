<script lang="ts">
  // Call Graph（IAR の Call Graph / Stack Usage・Keil の Call Graph の型）: ELF の静的な呼び出しの木（根 = リセット・例外・
  // 呼ばれない関数・関数ポインタの呼び先）、関数ごとの枠と呼び先を含む最大のスタック、最深の経路、再帰・関数ポインタ・
  // 可変長の確保の警告。Code Profile があれば自分の時間の割合も並べる。関数のクリックで Source・Disassembly。
  // ELF だけで動く（機器に触らない）
  import { errorText } from "../../lib/api";
  import { deepestKeys, rootsOf, treeRows, type StackFunction, type StackRoot } from "../../lib/callgraph";
  import { shared } from "../../lib/context";
  import { onCalls } from "../../lib/notify";
  import { i18n, type Key } from "../../lib/i18n";
  import { SymbolAddresses } from "../../lib/profile";
  import { percent } from "../../lib/rtos";
  import Empty from "./Empty.svelte";

  const { app, conn, dbg, showAddress } = shared();
  const quiet = { quiet: true };
  const symbols = new SymbolAddresses(conn);

  let report = $state<any>(null);
  let error = $state<string | null>(null);
  let open = $state<Set<string>>(new Set());
  let load = $state<Map<string, number>>(new Map());

  const online = $derived($app.link === "open");
  const functions = $derived<StackFunction[]>(report?.functions ?? []);
  const roots = $derived<StackRoot[]>(report ? rootsOf(report) : []);
  const rows = $derived(treeRows(functions, roots, open));

  // ELF が変わるたびに読む
  $effect(() => {
    if (online && $dbg.elf) void refresh();
  });
  $effect(() => onCalls(conn, ["profile.start", "profile.stop"], () => void readProfile()));

  async function refresh() {
    try {
      report = await conn.call("debug.stack.static", { top: 100000 }, quiet);
      error = null;
      const reset = rootsOf(report)[0];
      open = new Set(reset ? deepestKeys(reset).slice(0, -1) : []);
    } catch (e) {
      report = null;
      error = errorText(e);
    }
    await readProfile();
  }

  async function readProfile() {
    try {
      const r = await conn.call("profile.result", { by: "function", top: 10000 }, quiet);
      load = new Map(r.rows.map((x: any) => [x.function, x.fraction]));
    } catch {
      load = new Map();
    }
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
  {#if report}
    <div class="pane-bar">
      <button class="small" onclick={expandDeepest} title={$i18n("callGraph.deepestTip")}>{$i18n("callGraph.openDeepest")}</button>
      {#if report.msp}
        <span class="muted" title={$i18n("callGraph.mspTip")}>{$i18n("callGraph.msp", { one: report.msp.one_level, all: report.msp.all_nested })}
          {#if !report.msp.bounded}<span class="warn">⚠</span>{/if}</span>
      {/if}
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
  .note { font-size: 12px; max-width: 50%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
</style>
