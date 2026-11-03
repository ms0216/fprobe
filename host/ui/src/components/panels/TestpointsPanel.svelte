<script lang="ts">
  // Testpoints（docs/superpowers/specs/2026-11-02-ui-default-panels-design.md §4.1）: テストポイントの受け取りを始め・止め
  // （testpoint.start / stop）、受け取りの様子（testpoint.status。受けている間は読み続ける）と、関数ごとの時間・異常
  // （testpoint.report）・呼び出しの並び（testpoint.calls）・カバレッジ（testpoint.coverage）をタブで描く。
  // 値・並び・切った数・欠けは API のまま。異常・呼び出しの時刻は、API が記録のタイムベース（t）を返したときだけ時間軸へ飛ぶ
  import { onDestroy, untrack } from "svelte";
  import type { FieldError, FieldValues } from "../../lib/apiform";
  import { shared } from "../../lib/context";
  import { methodFields } from "../../lib/crosstrigger";
  import { seconds } from "../../lib/format";
  import { allowed } from "../../lib/guard";
  import { i18n } from "../../lib/i18n";
  import { onCalls } from "../../lib/notify";
  import { Poller } from "../../lib/poll";
  import { load, save } from "../../lib/prefs";
  import { percent } from "../../lib/rtos";
  import {
    callsCall, coverageCall, cut, pairText, readQuiet, revealOf, startCall, startFields, statusItems, testpointMethods,
  } from "../../lib/testpoint";
  import { buildParams } from "../../lib/apiform";
  import ApiFields from "./ApiFields.svelte";
  import LoadState from "./LoadState.svelte";
  import TabBar from "./TabBar.svelte";
  import TimeLink from "./TimeLink.svelte";

  const { app, conn } = shared();
  type Tab = "functions" | "anomalies" | "calls" | "coverage";
  const blank = (fields: { name: string }[]): FieldValues => Object.fromEntries(fields.map((f) => [f.name, ""]));

  const methods = testpointMethods();
  let method = $state(load("testpoints.method", ""));
  let values = $state<FieldValues>({});
  let startErrors = $state<Record<string, FieldError>>({});
  const fields = $derived(method ? startFields(method) : []);
  $effect(() => {
    void method;
    untrack(() => {
      save("testpoints.method", method);
      values = blank(fields);
      startErrors = {};
    });
  });

  let status = $state<any>(null);
  let statusError = $state<string | null>(null);
  const running = $derived(!!status?.running);
  const items = $derived(statusItems(status));
  const poller = new Poller(readStatus);
  onDestroy(() => poller.dispose());
  $effect(() => poller.poll(running));
  // だれが（AI も）始め・止めても読み直す
  $effect(() => onCalls(conn, ["testpoint.start", "testpoint.stop"], () => {
    void poller.refresh();
    void readTab();
  }));
  $effect(() => {
    if ($app.link === "open") untrack(() => void poller.refresh());
  });

  async function readStatus() {
    const r = await readQuiet(conn, "testpoint.status");
    status = r.result;
    statusError = r.error;
    // 前の受け取りの結果があり、まだ何も読んでいなければ今のタブを読む（AI が始めた分も開いたときに見える）
    if (statusItems(r.result) && !report && !calls && !coverage) void readTab();
  }

  async function start() {
    const call = startCall(method, values);
    if ("errors" in call) {
      startErrors = call.errors;
      return;
    }
    startErrors = {};
    if (!(await allowed(app))) return;
    if ((await app.act("testpoint.start", call.params as never)) !== undefined) await poller.refresh();
  }

  async function stop() {
    if (!(await allowed(app))) return;
    const r = await app.act("testpoint.stop", {} as never);
    if (r === undefined) return;
    status = r;
    await readTab();
  }

  // --- 読むタブ（押したとき・始め / 止めの通知で読み直す） ---
  let tab = $state<Tab>((load("testpoints.tab", "functions") as Tab) || "functions");
  $effect(() => save("testpoints.tab", tab));
  const reportFields = methodFields("testpoint.report");
  const callsFields = methodFields("testpoint.calls");
  const coverageFields = methodFields("testpoint.coverage");
  let reportValues = $state<FieldValues>(blank(reportFields));
  let callsValues = $state<FieldValues>(blank(callsFields));
  let coverageValues = $state<FieldValues>(blank(coverageFields));
  let tabErrors = $state<Record<string, FieldError>>({});
  let report = $state<any>(null);
  let calls = $state<any>(null);
  let coverage = $state<any>(null);
  let readError = $state<string | null>(null);

  async function readTab() {
    const built = tab === "calls" ? callsCall(callsValues) : tab === "coverage" ? coverageCall(coverageValues)
      : buildParams(reportFields, reportValues);
    if ("errors" in built) {
      tabErrors = built.errors;
      return;
    }
    tabErrors = {};
    const m = tab === "calls" ? "testpoint.calls" : tab === "coverage" ? "testpoint.coverage" : "testpoint.report";
    const r = await readQuiet(conn, m, built.params);
    readError = r.error;
    if (r.error) return;
    if (tab === "calls") calls = r.result;
    else if (tab === "coverage") coverage = r.result;
    else report = r.result;
  }

  const fnCut = $derived(report ? cut(report.functions?.length ?? 0, report.functions_total, report.limits?.top) : null);
  const anCut = $derived(report ? cut(report.anomalies?.length ?? 0, report.anomalies_total, report.limits?.anomalies) : null);
  const unCut = $derived(coverage ? cut(coverage.uncovered?.length ?? 0, coverage.uncovered_total, coverage.limit) : null);
  const deCut = $derived(coverage ? cut(coverage.decisions?.length ?? 0, coverage.decisions_total, coverage.limit) : null);
  const us = (v: number | undefined) => (v === undefined ? "—" : seconds(v / 1e6));
  const ts = (v: number | undefined) => (typeof v === "number" ? v.toFixed(6) : "—");      // 最初の印からの秒（API の t_s）
</script>

<div class="pane testpoints">
  <form class="pane-bar" onsubmit={(e) => { e.preventDefault(); void start(); }}>
    <label title={$i18n("testpoints.methodTip")}><span class="mono">method<span class="ast" aria-hidden="true">*</span></span>
      <select bind:value={method} aria-required="true" aria-invalid={!!startErrors.method} disabled={running}>
        <option value=""></option>
        {#each methods as m (m)}<option value={m}>{m}</option>{/each}
      </select></label>
    {#if method}<ApiFields {fields} bind:values errors={startErrors} disabled={running} />{/if}
    <button class="primary small" disabled={running || !method}>{$i18n("testpoints.start")}</button>
    <button type="button" class="small" disabled={!running} onclick={() => void stop()}>{$i18n("testpoints.stop")}</button>
  </form>

  <LoadState state={$poller} onretry={() => void poller.refresh()} />
  {#if statusError}<div class="bad pad">{statusError}</div>{/if}
  {#if items}
    <div class="status mono" aria-live="polite">
      {#if running}<span class="live">● {$i18n("testpoints.running")}</span>{:else}<span class="muted">{$i18n("testpoints.stopped")}</span>{/if}
      {#each items as [k, v] (k)}<span><span class="muted">{k}</span> {v}</span>{/each}
    </div>
    {#if status.error}<div class="bad pad">{status.error}</div>{/if}
    {#each status.warnings ?? [] as w, i (i)}<div class="warn pad">{w}</div>{/each}
  {:else if !statusError}
    <div class="muted pad">{$i18n("testpoints.none")}</div>
  {/if}

  <div class="tabs-line">
    <TabBar tabs={[{ id: "functions", label: $i18n("testpoints.functions") }, { id: "anomalies", label: $i18n("testpoints.anomalies") },
                   { id: "calls", label: $i18n("testpoints.calls") }, { id: "coverage", label: $i18n("testpoints.coverage") }]}
            bind:value={tab} ariaLabel={$i18n("panel.testpoints")} onselect={() => (readError = null)} />
  </div>
  <form class="pane-bar" onsubmit={(e) => { e.preventDefault(); void readTab(); }}>
    {#if tab === "calls"}
      <ApiFields fields={callsFields} bind:values={callsValues} errors={tabErrors} />
    {:else if tab === "coverage"}
      <ApiFields fields={coverageFields} bind:values={coverageValues} errors={tabErrors} />
    {:else}
      <ApiFields fields={reportFields} bind:values={reportValues} errors={tabErrors} />
    {/if}
    <button class="small">{$i18n("testpoints.read")}</button>
    <span class="muted small mono">{tab === "calls" ? "testpoint.calls" : tab === "coverage" ? "testpoint.coverage" : "testpoint.report"}</span>
  </form>
  {#if readError}<div class="bad pad">{readError}</div>{/if}

  <div class="body">
    {#if (tab === "functions" || tab === "anomalies") && report}
      <div class="summary mono">
        <span><span class="muted">hits</span> {report.hits}</span><span><span class="muted">losses</span> {report.losses}</span>
        <span><span class="muted">span_s</span> {report.span_s ?? "—"}</span>
        {#if report.tree_cut}<span class="warn">{$i18n("testpoints.treeCut", { n: report.tree_cut, depth: report.tree_max_depth })}</span>{/if}
      </div>
      {#if tab === "functions"}
        <table class="grid">
          <thead><tr><th>{$i18n("testpoints.function")}</th><th>{$i18n("testpoints.count")}</th>
            <th>{$i18n("testpoints.total")}</th><th>{$i18n("testpoints.min")}</th><th>{$i18n("testpoints.mean")}</th>
            <th>{$i18n("testpoints.max")}</th><th>σ</th><th>{$i18n("testpoints.selfMean")}</th></tr></thead>
          <tbody>
            {#each report.functions ?? [] as f (f.name)}
              <tr><td class="mono">{f.name}</td><td class="mono">{f.calls}</td>
                <td class="mono">{seconds(f.inclusive?.total_s)}</td><td class="mono">{seconds(f.inclusive?.min_s)}</td>
                <td class="mono">{seconds(f.inclusive?.mean_s)}</td><td class="mono">{seconds(f.inclusive?.max_s)}</td>
                <td class="mono">{seconds(f.inclusive?.stdev_s)}</td><td class="mono">{seconds(f.exclusive?.mean_s)}</td></tr>
            {/each}
          </tbody>
        </table>
        {#if fnCut}<div class="muted small pad">{$i18n("common.truncated", { ...fnCut })}</div>{/if}
      {:else}
        <div class="summary mono">
          {#each Object.entries(report.anomaly_counts ?? {}) as [k, n] (k)}<span class="warn">{k} {n}</span>{:else}
            <span class="muted">{$i18n("testpoints.noAnomalies")}</span>{/each}
        </div>
        <table class="grid">
          <thead><tr><th>{$i18n("testpoints.time")}</th><th>{$i18n("testpoints.kind")}</th><th>{$i18n("testpoints.function")}</th>
            <th>id</th><th>{$i18n("testpoints.detail")}</th></tr></thead>
          <tbody>
            {#each report.anomalies ?? [] as a, i (i)}
              <tr><td><TimeLink to={revealOf(a)}>{ts(a.t_s)}</TimeLink></td><td class="mono">{a.type}</td>
                <td class="mono">{a.function ?? ""}</td><td class="mono">{a.id ?? ""}</td><td>{a.detail ?? ""}</td></tr>
            {/each}
            {#each report.open ?? [] as o, i (`open${i}`)}
              <tr><td><TimeLink to={revealOf(o)}>{ts(o.t_s)}</TimeLink></td><td class="mono">open</td>
                <td class="mono">{o.function}</td><td></td><td>{$i18n("testpoints.openFrame")}</td></tr>
            {/each}
          </tbody>
        </table>
        {#if anCut}<div class="muted small pad">{$i18n("common.truncated", { ...anCut })}</div>{/if}
      {/if}
    {:else if tab === "calls" && calls}
      <div class="summary mono">
        <span><span class="muted">total</span> {calls.total}</span>
        {#if calls.truncated}<span class="warn">{$i18n("common.truncated", { shown: calls.calls.length, total: calls.total, limit: calls.limit })}</span>{/if}
        {#if calls.dropped}<span class="warn">{$i18n("testpoints.dropped", { n: calls.dropped })}</span>{/if}
      </div>
      <table class="grid">
        <thead><tr><th>{$i18n("testpoints.time")}</th><th>{$i18n("testpoints.function")}</th><th>{$i18n("testpoints.duration")}</th>
          <th>{$i18n("testpoints.self")}</th><th>{$i18n("testpoints.depth")}</th><th>{$i18n("testpoints.complete")}</th></tr></thead>
        <tbody>
          {#each calls.calls ?? [] as c, i (i)}
            <tr class:warn={!c.complete}><td><TimeLink to={revealOf(c)}>{ts(c.t_s)}</TimeLink></td>
              <td class="mono" style:padding-left="{4 + 12 * (c.depth ?? 0)}px">{c.function}</td>
              <td class="mono">{us(c.duration_us)}</td><td class="mono">{us(c.self_us)}</td>
              <td class="mono">{c.depth}</td><td class="mono">{String(c.complete)}</td></tr>
          {/each}
        </tbody>
      </table>
    {:else if tab === "coverage" && coverage}
      <dl class="kv mono">
        <dt>{$i18n("testpoints.covFunctions")}</dt><dd>{pairText(coverage.functions)} ({percent(coverage.function_ratio)})</dd>
        <dt>C0</dt><dd>{pairText(coverage.c0)} ({percent(coverage.c0_ratio)})</dd>
        <dt>C1</dt><dd>{pairText(coverage.c1)} ({percent(coverage.c1_ratio)})</dd>
      </dl>
      <h4>{$i18n("testpoints.uncovered")} <span class="muted">({coverage.uncovered_total})</span></h4>
      <table class="grid">
        <thead><tr><th>where</th><th>{$i18n("testpoints.function")}</th><th>{$i18n("testpoints.kind")}</th><th>label</th></tr></thead>
        <tbody>
          {#each coverage.uncovered ?? [] as u (u.id)}
            <tr><td class="mono">{u.where}</td><td class="mono">{u.function}</td>
              <td class="mono">{u.kind}{u.implicit ? " (implicit)" : ""}</td><td class="mono">{u.label ?? ""}</td></tr>
          {/each}
        </tbody>
      </table>
      {#if unCut}<div class="muted small pad">{$i18n("common.truncated", { ...unCut })}</div>{/if}
      {#if coverage.decisions?.length}
        <h4>{$i18n("testpoints.decisions")} <span class="muted">({coverage.decisions_total})</span></h4>
        <pre class="mono small">{JSON.stringify(coverage.decisions, null, 1)}</pre>
        {#if deCut}<div class="muted small pad">{$i18n("common.truncated", { ...deCut })}</div>{/if}
      {/if}
    {:else}
      <div class="muted pad">{$i18n("testpoints.readHint")}</div>
    {/if}
  </div>
</div>

<style>
  .testpoints { display: flex; flex-direction: column; overflow: auto; }
  label { display: inline-flex; gap: 3px; align-items: center; font-size: 12.5px; }
  .ast { color: var(--bad); margin-left: 1px; }
  .status, .summary { display: flex; gap: 12px; flex-wrap: wrap; padding: 4px 8px; font-size: 12px; }
  .live { color: var(--bad); }
  .tabs-line { padding: 0 8px; border-bottom: 1px solid var(--line); }
  .body { padding: 2px 4px; }
  .kv { display: grid; grid-template-columns: max-content 1fr; gap: 2px 12px; margin: 4px 8px; font-size: 12px; }
  .kv dt { color: var(--muted); }
  .kv dd { margin: 0; }
  h4 { font-size: 12px; font-weight: 600; color: var(--muted); margin: 8px 8px 2px; }
  pre { margin: 2px 8px; white-space: pre-wrap; }
  .bad { color: var(--bad); }
  .warn { color: var(--warn); }
  .small { font-size: 12px; }
  .pad { padding: 4px 8px; font-size: 12px; }
</style>
