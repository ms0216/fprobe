<script lang="ts">
  // RTOS Timing（docs/superpowers/specs/2026-11-02-ui-default-panels-design.md §4.2）: タスクの時間軸（CPU Load のパネルか
  // AI が rtos.timeline.start で取った）から、ジョブの応答時間・実行時間・周期の分布と最悪の回（rtos.timeline.jobs）、
  // キュー・セマフォの使用量と満杯・空・待ち（rtos.timeline.queues）、タスクごとのスタックの最大・あふれの印とヒープの最小の
  // 空き（rtos.timeline.memory）をタブで描く。値・並びは API のまま。最悪の回・最大の時刻は時間軸へ飛ぶ
  import { untrack } from "svelte";
  import type { FieldError, FieldValues } from "../../lib/apiform";
  import { buildParams } from "../../lib/apiform";
  import { shared } from "../../lib/context";
  import { methodFields } from "../../lib/crosstrigger";
  import { i18n } from "../../lib/i18n";
  import { onCalls } from "../../lib/notify";
  import { load, save } from "../../lib/prefs";
  import { percent } from "../../lib/rtos";
  import {
    DIST_KEYS, TAB_METHOD, pointReveal, readAnalysis, rowsOf, spanReveal, type Analysis, type TimingTab,
  } from "../../lib/rtostiming";
  import { formatDuration, originFor, type Origin } from "../../lib/time";
  import ApiFields from "./ApiFields.svelte";
  import TabBar from "./TabBar.svelte";
  import TimeLink from "./TimeLink.svelte";

  const { app, conn, tl } = shared();
  const origins = tl.origins;
  const blank = (m: string): FieldValues => Object.fromEntries(methodFields(m).map((f) => [f.name, ""]));

  let tab = $state<TimingTab>((load("rtosTiming.tab", "jobs") as TimingTab) || "jobs");
  $effect(() => save("rtosTiming.tab", tab));
  let values = $state<Record<TimingTab, FieldValues>>({ jobs: blank(TAB_METHOD.jobs), queues: blank(TAB_METHOD.queues),
                                                         stacks: blank(TAB_METHOD.stacks) });
  let errors = $state<Record<string, FieldError>>({});
  let results = $state<Record<TimingTab, Analysis | null>>({ jobs: null, queues: null, stacks: null });
  let error = $state<string | null>(null);

  async function read() {
    const built = buildParams(methodFields(TAB_METHOD[tab]), values[tab]);
    if ("errors" in built) {
      errors = built.errors;
      return;
    }
    errors = {};
    const r = await readAnalysis(conn, tab, built.params);
    error = r.error;
    results[tab] = r.analysis;
  }

  // だれが（AI も）時間軸を始め・止めても、いまのタブを読み直す（開いたときも 1 回）
  $effect(() => {
    if ($app.link !== "open") return;
    untrack(() => void read());
    return onCalls(conn, ["rtos.timeline.start", "rtos.timeline.stop"], () => void read());
  });

  const now = $derived(results[tab]);
  const r = $derived(now?.result ?? null);
  const base = $derived<Origin>(originFor($origins, "recording", { t0: now?.t0 ?? 0, tickS: now?.tickS ?? 1e-6 },
                                          now?.inRecording ?? false));
  /** タイムベースの刻み → 時間の長さ（単位の書き換えだけ） */
  const dur = (ticks: number | null | undefined) =>
    typeof ticks === "number" && now?.tickS ? formatDuration(ticks * now.tickS, 3) : "—";
  const distCell = (d: any, k: string) => (d && d.count ? (k === "count" ? String(d.count) : dur(d[k])) : "—");
</script>

{#snippet dist(label: string, d: any)}
  <tr><td class="muted">{label}</td>{#each DIST_KEYS as k (k)}<td class="mono">{distCell(d, k)}</td>{/each}</tr>
{/snippet}

<div class="pane rtos-timing">
  <div class="tabs-line">
    <TabBar tabs={[{ id: "jobs", label: $i18n("rtosTiming.jobs"), title: TAB_METHOD.jobs },
                   { id: "queues", label: $i18n("rtosTiming.queues"), title: TAB_METHOD.queues },
                   { id: "stacks", label: $i18n("rtosTiming.stacks"), title: TAB_METHOD.stacks }]}
            bind:value={tab} ariaLabel={$i18n("panel.rtosTiming")} onselect={() => { error = null; if (!results[tab]) void read(); }} />
  </div>
  <form class="pane-bar" onsubmit={(e) => { e.preventDefault(); void read(); }}>
    <ApiFields fields={methodFields(TAB_METHOD[tab])} bind:values={values[tab]} {errors} />
    <button class="small">{$i18n("rtosTiming.read")}</button>
    <span class="muted small mono">{TAB_METHOD[tab]}</span>
    {#if r}<span class="muted small">{r.running ? $i18n("rtosTiming.live") : ""} {r.method ?? ""}
      {#if typeof r.resolution_us === "number"}· {$i18n("rtosTiming.resolution", { us: r.resolution_us.toFixed(1) })}{/if}</span>{/if}
  </form>
  {#if error}<div class="bad pad">{error}</div>{/if}

  {#if r}
    {#each r.notes ?? [] as n, i (i)}<div class="muted small pad">{n}</div>{/each}
    {#if tab === "jobs" && "tasks" in r}
      {#each rowsOf(r.tasks) as row (row.name)}
        <div class="task">
          <div><strong class="mono">{row.name}</strong>
            <span class="muted small mono">jobs {row.jobs} · preemptions {row.preemptions}{row.truncated ? ` · truncated ${row.truncated}` : ""}</span></div>
          <table class="grid">
            <thead><tr><th></th>{#each DIST_KEYS as k (k)}<th>{k}</th>{/each}</tr></thead>
            <tbody>
              {@render dist($i18n("rtosTiming.exec"), row.exec)}
              {@render dist($i18n("rtosTiming.wall"), row.wall)}
              {@render dist($i18n("rtosTiming.period"), row.period)}
              {#if row.response}{@render dist($i18n("rtosTiming.response"), row.response)}{/if}
            </tbody>
          </table>
          <div class="worst small">
            {#if row.worst_exec}<span>{$i18n("rtosTiming.worstExec", { d: dur(row.worst_exec.exec) })}
              <TimeLink to={spanReveal(row.worst_exec)} origin={base} /></span>{/if}
            {#if row.worst_wall}<span>{$i18n("rtosTiming.worstWall", { d: dur(row.worst_wall.wall) })}
              <TimeLink to={spanReveal(row.worst_wall)} origin={base} /></span>{/if}
            {#if row.worst_response}<span>{$i18n("rtosTiming.worstResponse", { d: dur(row.worst_response.response) })}
              <TimeLink to={spanReveal(row.worst_response)} origin={base} /></span>{/if}
          </div>
        </div>
      {:else}
        <div class="muted pad">{$i18n("rtosTiming.noJobs")}</div>
      {/each}
      <h4>{$i18n("rtosTiming.runs")}</h4>
      <table class="grid">
        <thead><tr><th>{$i18n("rtosTiming.task")}</th><th>{$i18n("rtosTiming.count")}</th>
          {#each ["min", "median", "max"] as k (k)}<th>{$i18n("rtosTiming.exec")} {k}</th>{/each}
          <th>{$i18n("rtosTiming.period")} median</th><th>{$i18n("rtosTiming.longest")}</th><th>followed_by</th></tr></thead>
        <tbody>
          {#each rowsOf(r.runs) as row (row.name)}
            <tr><td class="mono">{row.name}</td><td class="mono">{row.runs}</td>
              {#each ["min", "median", "max"] as k (k)}<td class="mono">{distCell(row.exec, k)}</td>{/each}
              <td class="mono">{distCell(row.period, "median")}</td>
              <td>{#if row.worst_exec}<TimeLink to={spanReveal(row.worst_exec)} origin={base} />{/if}</td>
              <td class="mono small">{Object.entries(row.followed_by ?? {}).map(([k, n]) => `${k} ${n}`).join(", ")}</td></tr>
          {/each}
        </tbody>
      </table>
      {#if r.jobs}
        <h4>{$i18n("rtosTiming.jobList")} <span class="muted">({r.jobs.length} / {r.jobs_total})</span></h4>
        <table class="grid">
          <thead><tr><th>{$i18n("rtosTiming.time")}</th><th>{$i18n("rtosTiming.exec")}</th><th>{$i18n("rtosTiming.wall")}</th>
            <th>{$i18n("rtosTiming.response")}</th><th>preemptions</th><th>end_reason</th></tr></thead>
          <tbody>
            {#each r.jobs as j, i (i)}
              <tr><td><TimeLink to={spanReveal(j)} origin={base} /></td><td class="mono">{dur(j.exec)}</td>
                <td class="mono">{dur(j.wall)}</td><td class="mono">{dur(j.response)}</td>
                <td class="mono">{j.preemptions}</td><td class="mono">{j.end_reason}</td></tr>
            {/each}
          </tbody>
        </table>
      {/if}
    {:else if tab === "queues" && "objects" in r}
      {#if r.kernel}<div class="muted small pad mono">events {r.kernel.events} · lost_records {r.kernel.lost_records} · gaps {r.kernel.gaps}</div>{/if}
      <table class="grid">
        <thead><tr><th>{$i18n("rtosTiming.object")}</th><th>kind</th><th>length</th><th>max</th><th>mean</th>
          <th>max_usage</th><th>mean_usage</th><th>sends</th><th>receives</th><th>send_failed</th><th>receive_failed</th>
          <th>waits_send</th><th>waits_receive</th></tr></thead>
        <tbody>
          {#each rowsOf(r.objects) as q (q.name)}
            <tr><td class="mono">{q.name}{#if q.saturated}<span class="warn" title={$i18n("rtosTiming.saturatedTip")}> saturated</span>{/if}</td>
              <td class="mono">{q.kind}</td><td class="mono">{q.length ?? "—"}</td><td class="mono">{q.max ?? "—"}</td>
              <td class="mono">{typeof q.mean === "number" ? q.mean.toFixed(2) : "—"}</td>
              <td class="mono">{percent(q.max_usage)}</td><td class="mono">{percent(q.mean_usage)}</td>
              <td class="mono">{q.sends}</td><td class="mono">{q.receives}</td>
              <td class="mono" class:bad={q.send_failed > 0}>{q.send_failed}</td>
              <td class="mono" class:bad={q.receive_failed > 0}>{q.receive_failed}</td>
              <td class="mono">{q.waits_send}</td><td class="mono">{q.waits_receive}</td></tr>
          {:else}
            <tr><td colspan="13" class="muted">{$i18n("rtosTiming.noObjects")}</td></tr>
          {/each}
        </tbody>
      </table>
      {#if r.lost}<div class="warn small pad">{$i18n("rtosTiming.lost", { n: r.lost })}</div>{/if}
    {:else if "stacks" in r}
      <table class="grid">
        <thead><tr><th>{$i18n("rtosTiming.task")}</th><th>size</th><th>max_used</th><th>min_used</th><th>last_used</th>
          <th>{$i18n("rtosTiming.maxAt")}</th><th>overflow</th></tr></thead>
        <tbody>
          {#each rowsOf(r.stacks) as s (s.name)}
            <tr class:bad={s.overflow}><td class="mono">{s.name}</td><td class="mono">{s.size ?? "—"}</td>
              <td class="mono">{s.max_used}</td><td class="mono">{s.min_used}</td><td class="mono">{s.last_used}</td>
              <td><TimeLink to={pointReveal(s.max_at)} origin={base} /></td><td class="mono">{String(s.overflow)}</td></tr>
          {:else}
            <tr><td colspan="7" class="muted">{$i18n("rtosTiming.noStacks")}</td></tr>
          {/each}
        </tbody>
      </table>
      {#if r.heap}
        <dl class="kv mono">
          <dt>{$i18n("rtosTiming.heapTotal")}</dt><dd>{r.heap.total ?? "—"}</dd>
          <dt>{$i18n("rtosTiming.heapMinFree")}</dt><dd>{r.heap.min_free} <TimeLink to={pointReveal(r.heap.min_at)} origin={base} /></dd>
          <dt>{$i18n("rtosTiming.heapLastFree")}</dt><dd>{r.heap.last_free}</dd>
        </dl>
      {/if}
    {/if}
  {:else if !error}
    <div class="muted pad">{$i18n("rtosTiming.hint")}</div>
  {/if}
</div>

<style>
  .rtos-timing { overflow: auto; }
  .tabs-line { padding: 0 8px; border-bottom: 1px solid var(--line); }
  .task { margin: 6px 8px 10px; }
  .worst { display: flex; gap: 14px; flex-wrap: wrap; margin-top: 2px; }
  .kv { display: grid; grid-template-columns: max-content 1fr; gap: 2px 12px; margin: 6px 8px; font-size: 12px; }
  .kv dt { color: var(--muted); }
  .kv dd { margin: 0; }
  h4 { font-size: 12px; font-weight: 600; color: var(--muted); margin: 8px 8px 2px; }
  .bad { color: var(--bad); }
  .warn { color: var(--warn); }
  .small { font-size: 12px; }
  .pad { padding: 4px 8px; font-size: 12px; }
</style>
