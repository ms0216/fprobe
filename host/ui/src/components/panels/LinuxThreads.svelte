<script lang="ts">
  // Threads の Linux のプロセス（linux.halt.debug.* に attach している間）: 全スレッドの tid・名前・いたコア・モード・
  // 場所（linux.halt.debug.threads の値のまま）。▶ は今のスレッド（target.state の linux.tid）。行を押すとそのスレッドを
  // 選ぶ（linux.halt.debug.select。AI の選択と同じ API）。レジスタ・呼び出し履歴・変数はそのスレッドに変わる
  import { onDestroy } from "svelte";
  import { shared } from "../../lib/context";
  import { i18n } from "../../lib/i18n";
  import { LinuxThreads } from "../../lib/linuxthreads";
  import { hex32 } from "./format";

  const { app, conn } = shared();
  const store = new LinuxThreads(conn, app);
  const off = app.subscribe((s) => store.onTarget(s.target));
  onDestroy(off);
  const lx = $derived($app.target?.linux);
  const FROM = { selected: "linuxThreads.from.selected", last_stop: "linuxThreads.from.last_stop", rule: "linuxThreads.from.rule" } as const;
  /** tid_from（API の値: 今のスレッドの決まり方）の言葉。知らない値は API の値のまま */
  const fromText = (f: string | null | undefined) => (f && f in FROM ? $i18n(FROM[f as keyof typeof FROM]) : (f ?? ""));
</script>

<div class="pane-bar">
  <span class="mono">Linux {lx?.comm} pid {lx?.pid}</span>
  {#if lx?.tid !== null && lx?.tid !== undefined}<span class="muted">{$i18n("linuxThreads.current", { tid: lx.tid, from: fromText(lx.tid_from) })}</span>{/if}
  {#if $store.stale}<span class="muted">{$i18n("linuxThreads.stale")}</span>{/if}
</div>
{#if $store.error}<div class="bad note">{$store.error}</div>{/if}
<table class="grid" class:stale={$store.stale}>
  <thead><tr><th>tid</th><th>{$i18n("common.name")}</th><th>{$i18n("linuxThreads.core")}</th>
    <th>{$i18n("linuxThreads.mode")}</th><th>{$i18n("linuxThreads.where")}</th></tr></thead>
  <tbody>
    {#if $store.rows}
      {#each $store.rows.threads as t (t.tid)}
        <tr class="task" class:current={t.tid === lx?.tid} title={$i18n("linuxThreads.rowTip")}
            onclick={() => void store.select(t.tid)}>
          <td class="mono">{t.tid === lx?.tid ? "▶ " : ""}{t.tid}</td>
          <td class="mono">{t.comm}</td>
          <td class="mono">{t.on_core ?? "—"}</td>
          <td class="muted">{t.mode ?? "—"}</td>
          <td class="mono">{#if t.error}<span class="bad">{t.error}</span>{:else}{t.function ?? (t.pc != null ? hex32(t.pc) : "—")}{t.file ? ` (${t.file}:${t.line})` : ""}{/if}</td>
        </tr>
      {/each}
    {:else}
      {#each lx?.threads ?? [] as t (t.tid)}
        <tr class="task" class:current={t.tid === lx?.tid}><td class="mono">{t.tid === lx?.tid ? "▶ " : ""}{t.tid}</td>
          <td class="mono">{t.comm}</td><td></td><td></td><td></td></tr>
      {/each}
    {/if}
  </tbody>
</table>

<style>
  .task { cursor: pointer; }
  .task:hover td { background: var(--select); }
  .current td { font-weight: 600; }
  .stale { opacity: 0.6; }
  .bad { color: var(--bad); }
  .note { font-size: 12px; padding: 2px 8px; }
</style>
