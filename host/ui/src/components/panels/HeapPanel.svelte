<script lang="ts">
  // Heap（段階 B §3 の 6）: ヒープを辿る（heap.scan: 使用量・空き・最大の空き・断片化・ブロック・壊れ）と、malloc / free
  // の記録（heap.trace.*: 二重の free 等の問題・呼び出し元ごとの生きている確保）。値は API の事実のまま並べる
  import { confirmer } from "../../lib/confirm";
  import { shared } from "../../lib/context";
  import { allowed } from "../../lib/guard";
  import { issueText, readReport, scanParams, type ScanForm } from "../../lib/heap";
  import { i18n } from "../../lib/i18n";
  import { isRunning } from "../../lib/commands";
  import { hex32 } from "./format";

  const { app, conn } = shared();
  let form = $state<ScanForm>({ allocator: "", live: false, compare: false });
  let scan = $state<any>(null);
  let report = $state<any>(null);
  let reportError = $state<string | null>(null);

  const running = $derived(isRunning($app.target?.state));
  const pct = (part: number, whole: number) => (whole ? Math.round((1000 * part) / whole) / 10 : 0);

  async function runScan() {
    if (!(await allowed(app))) return;
    const out = await app.act("heap.scan", { ...scanParams({ ...form, live: form.live || running }) } as never);
    if (out !== undefined) scan = out;
  }

  /** 記録はブレークポイント: 確保・解放のたびにコアを少し止めるので確かめる */
  async function startTrace() {
    if (!(await allowed(app))) return;
    if (!(await confirmer.ask($i18n("heap.confirmTrace"), { ok: $i18n("heap.traceStart") }))) return;
    await app.act("heap.trace.start", {});
  }

  async function stopTrace() {
    if (!(await allowed(app))) return;
    await app.act("heap.trace.stop", {});
    await readTrace();
  }

  async function readTrace() {
    const r = await readReport(conn);
    report = r.report;
    reportError = r.error;
  }
</script>

<div class="pane heap">
  <section>
    <h3>{$i18n("heap.scan")}</h3>
    <form class="bar" onsubmit={(e) => { e.preventDefault(); void runScan(); }}>
      <select bind:value={form.allocator} aria-label={$i18n("heap.allocator")}>
        <option value="">{$i18n("heap.allocatorAll")}</option>
        <option value="freertos">FreeRTOS (heap_4 / heap_5)</option>
        <option value="newlib_nano">newlib-nano</option>
        <option value="newlib">newlib (dlmalloc)</option>
      </select>
      <label title={$i18n("heap.liveTip")}><input type="checkbox" bind:checked={form.live} /> {$i18n("heap.live")}</label>
      <label title={$i18n("heap.compareTip")}><input type="checkbox" bind:checked={form.compare} /> {$i18n("heap.compare")}</label>
      <button class="primary small">{$i18n("heap.scanRun")}</button>
    </form>
    {#if scan}
      {#if !scan.consistent}<div class="warn small">{$i18n("heap.notConsistent")}</div>{/if}
      {#each scan.heaps as h, k (k)}
        <div class="heap-one">
          <div><strong class="mono">{h.allocator}</strong> <span class="muted">{h.scheme ?? ""}</span>
            {#if !h.complete}<span class="warn small">{$i18n("heap.incomplete")}</span>{/if}</div>
          <div class="bar-line">
            <span class="bar"><span style:width="{pct(h.used, h.total)}%"></span></span>
            <span class="mono">{h.used} / {h.total} ({pct(h.used, h.total)}%)</span>
          </div>
          <dl class="kv mono">
            <dt>{$i18n("heap.free")}</dt><dd>{h.free}</dd>
            <dt>{$i18n("heap.largestFree")}</dt><dd>{h.largest_free}</dd>
            <dt>{$i18n("heap.fragmentation")}</dt><dd>{h.fragmentation}</dd>
            <dt>{$i18n("heap.blocks")}</dt><dd>{h.used_blocks} / {h.free_blocks}</dd>
            <dt>{$i18n("heap.overhead")}</dt><dd>{h.overhead}</dd>
          </dl>
          {#each h.issues ?? [] as issue, i (i)}<div class="bad small mono">{issueText(issue)}</div>{/each}
          {#if h.changes && !h.changes.first}
            <div class="small mono">{$i18n("heap.changes", { new: h.changes.new_count, newBytes: h.changes.new_bytes,
              gone: h.changes.gone_count, goneBytes: h.changes.gone_bytes })}</div>
            {#each h.changes.new ?? [] as c, i (i)}<div class="small mono muted">+ {hex32(c.pointer)} {c.size}</div>{/each}
          {:else if h.changes?.first}
            <div class="small muted">{$i18n("heap.changesFirst")}</div>
          {/if}
          {#if h.callers?.length}
            <table class="grid">
              <thead><tr><th>{$i18n("heap.caller")}</th><th>{$i18n("heap.count")}</th><th>{$i18n("heap.bytes")}</th></tr></thead>
              <tbody>{#each h.callers as c, i (i)}<tr><td class="mono">{c.caller}</td><td class="mono">{c.count}</td><td class="mono">{c.bytes}</td></tr>{/each}</tbody>
            </table>
          {/if}
          {#each h.notes ?? [] as n, i (i)}<div class="muted small">{n}</div>{/each}
        </div>
      {/each}
    {/if}
  </section>

  <section>
    <h3>{$i18n("heap.trace")}</h3>
    <div class="bar">
      <button class="small" title={$i18n("heap.traceTip")} onclick={() => void startTrace()}>{$i18n("heap.traceStart")}</button>
      <button class="small" onclick={() => void stopTrace()}>{$i18n("heap.traceStop")}</button>
      <button class="small" onclick={() => void readTrace()}>{$i18n("heap.traceReport")}</button>
    </div>
    {#if reportError}<div class="bad small">{reportError}</div>{/if}
    {#if report}
      <dl class="kv mono">
        <dt>{$i18n("heap.allocations")}</dt><dd>{report.allocations}</dd>
        <dt>{$i18n("heap.frees")}</dt><dd>{report.frees}</dd>
        <dt>{$i18n("heap.untrackedFrees")}</dt><dd>{report.untracked_frees ?? 0}</dd>
        <dt>{$i18n("heap.liveNow")}</dt><dd>{report.live?.count ?? 0} / {report.live?.bytes ?? 0} B</dd>
        <dt>{$i18n("heap.peak")}</dt><dd>{report.peak_bytes ?? 0} B</dd>
      </dl>
      {#each report.issues ?? [] as issue, i (i)}<div class="bad small mono">{issueText(issue)}</div>{/each}
      {#if report.live_by_caller?.length}
        <table class="grid">
          <thead><tr><th>{$i18n("heap.caller")}</th><th>{$i18n("heap.count")}</th><th>{$i18n("heap.bytes")}</th>
            <th>{$i18n("heap.oldest")}</th></tr></thead>
          <tbody>
            {#each report.live_by_caller as c, i (i)}
              <tr><td class="mono">{c.caller}</td><td class="mono">{c.count}</td><td class="mono">{c.bytes}</td>
                <td class="mono">{c.oldest_age_s ?? ""}</td></tr>
            {/each}
          </tbody>
        </table>
        {#if report.truncated}
          <div class="muted small">{$i18n("common.truncated", { shown: report.live_by_caller.length, total: report.callers_total, limit: report.top })}</div>
        {/if}
      {/if}
      {#each report.notes ?? [] as n, i (i)}<div class="muted small">{n}</div>{/each}
    {/if}
  </section>
</div>

<style>
  .heap { overflow: auto; }
  section { padding: 6px 10px 8px; border-bottom: 1px solid var(--line); }
  h3 { font-size: 12px; font-weight: 600; color: var(--muted); margin: 0 0 4px; }
  .bar { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin: 4px 0; }
  .heap-one { margin: 6px 0; }
  .bar-line { display: flex; gap: 8px; align-items: center; }
  .bar-line .bar { display: inline-block; width: 200px; height: 8px; background: var(--line); border-radius: 4px; overflow: hidden; margin: 0; }
  .bar-line .bar span { display: block; height: 100%; background: var(--accent); }
  .kv { display: grid; grid-template-columns: max-content 1fr; gap: 2px 12px; margin: 4px 0; font-size: 12px; }
  .kv dt { color: var(--muted); }
  .kv dd { margin: 0; }
  .bad { color: var(--bad); }
  .warn { color: var(--warn); }
  .small { font-size: 12px; }
</style>
