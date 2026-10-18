<script lang="ts">
  // Event Log（Tracealyzer / SystemView の Events・TRACE32 のトレースリスト。G07）: 記録の出来事の構造化された検索
  // （recording.find）と結果の表・時間軸の印、区間の統計（timing.measure: 分布図・外れ値 ↔ 生データ）、調査の印とメモ
  // （annotation.mark・recording.mark）、記録の概要（stream・kind ごとの件数・割合・1 秒あたり・最初・最後 → 検索へ）。
  // 行のダブルクリック・時刻のリンクで時間軸へ、時間軸の選択で近い行を選ぶ。
  // 計算は lib/eventlog.ts
  import { errorText } from "../../lib/api";
  import { tick, untrack } from "svelte";
  import { download, shortNumber } from "../../lib/chart";
  import { shared } from "../../lib/context";
  import {
    describeCondition, emptyCondition, eventsCsv, fieldText, histogramBars, intervalsInBin, kindQuery, kindStats, notesOf,
    outlierMarks, searchMarks, summary, toQuery, type Condition, type Histogram, type KindStat, type Note, type Outlier,
    type RecEvent,
  } from "../../lib/eventlog";
  import { allowed } from "../../lib/guard";
  import { i18n } from "../../lib/i18n";
  import { onCalls, onRecording } from "../../lib/notify";
  import { load, save } from "../../lib/prefs";
  import { formatDuration, timeText } from "../../lib/time";
  import { nearestAtOrBefore, onTimeline } from "../../lib/timeline/events";
  import Chart from "../charts/Chart.svelte";
  import ConditionFields from "./ConditionFields.svelte";
  import TabBar from "./TabBar.svelte";
  import TimeLink from "./TimeLink.svelte";

  type Tab = "overview" | "search" | "stats" | "notes";
  type Scope = "all" | "view" | "range";
  const SEARCH_OWNER = "eventLog", OUTLIER_OWNER = "eventLog.outliers";
  // 印を置く操作（ほかの利用者のものも）: 印を読み直す（記録を開いた・始めた・止めたは通知 recording）
  const MARKING = ["recording.mark"];

  const { app, conn, tl, dock, reveal } = shared();
  const restore = (key: string): Condition => {
    try {
      return { ...emptyCondition(), ...JSON.parse(load(key, "{}")) };
    } catch {
      return emptyCondition();
    }
  };

  let tab = $state<Tab>(load("eventLog.tab", "search") as Tab);
  let status = $state<any>(null);                        // recording.status
  let streams = $state<string[]>([]);

  // 検索
  let cond = $state(restore("eventLog.query"));
  let scope = $state<Scope>(load("eventLog.scope", "all") as Scope);
  let range = $state<[number, number] | null>(null);     // scope = range（統計の 1 回の区間から）
  let found = $state<{ count: number; events: RecEvent[] } | null>(null);
  let selected = $state(-1);
  let searchError = $state<string | null>(null);
  let box = $state<HTMLDivElement>();

  // 区間の統計
  let startCond = $state(restore("eventLog.start"));
  let endCond = $state(restore("eventLog.end"));
  let stats = $state<any>(null);
  let picked = $state<number | null>(null);
  let statsError = $state<string | null>(null);

  // 概要（stream・kind ごとの件数）
  let overview = $state<{ rows: KindStat[]; total: number; max: number; partial: boolean } | null>(null);
  let overviewScope = $state<"all" | "view">("all");
  let overviewError = $state<string | null>(null);

  // 印とメモ
  let notes = $state<Note[]>([]);
  let noteText = $state("");
  let noteAt = $state<"selection" | "now">("selection");

  const linked = $derived($app.link === "open");          // つながってから読む（つなぎ直したら読み直す）
  const origin = $derived(status?.span?.[0] ?? 0);
  const hz = $derived<number>(status?.timebase_hz ?? 0);
  const tickS = $derived(hz ? 1 / hz : 0);
  const has = $derived(!!status?.name);
  const viewing = $derived($tl.source === "recording" && $tl.recording === status?.name && !!$tl.extent);
  const viewRange = $derived<[number, number] | null>(viewing ? [$tl.view.start, $tl.view.end] : null);
  const selection = $derived(viewing ? $tl.selection : null);
  // 時刻の文字（記録の始めからの秒。時刻の欄・範囲の選択肢。lib/time.ts）
  const origins = tl.origins;
  const at = (t: number) => timeText($origins, { source: "recording", t }) ?? String(t);

  $effect(() => save("eventLog.tab", tab));
  $effect(() => {
    if (!linked) return;
    void readStatus();
    const reread = () => void readStatus().then(() => {
      if (tab === "notes") void readNotes();
      else if (tab === "overview" && status?.name) void countKinds();
    });
    const stops = [onRecording(conn, reread), onCalls(conn, MARKING, reread)];
    return () => stops.forEach((stop) => stop());
  });
  $effect(() => {
    if (tab === "notes" && has && linked) void readNotes();
  });
  $effect(() => {
    void overviewScope;
    if (tab === "overview" && has && linked) void untrack(() => countKinds());
  });
  // 閉じたら印を消す
  $effect(() => () => {
    tl.setMarks(SEARCH_OWNER, "recording", []);
    tl.setMarks(OUTLIER_OWNER, "recording", []);
  });
  // 時間軸で選んだ時刻の行を選ぶ（記録の時間軸のとき）
  $effect(() => onTimeline({
    select: (d) => {
      if (d.source !== "recording" || !found?.events.length) return;
      const i = nearestAtOrBefore(found.events.map((e) => e.t), d.t);
      if (i >= 0) void choose(i, false);
    },
  }));

  async function readStatus() {
    try {
      status = await conn.call("recording.status", {}, { quiet: true });
      if (status?.name && !streams.length) {
        const out = await conn.call("recording.query", { sql: "SELECT DISTINCT stream FROM events", limit: 100 },
                                    { quiet: true });
        streams = (out.rows ?? []).map((r: unknown[]) => String(r[0]));
      }
    } catch {
      status = status && status.name ? status : null;
    }
  }

  async function startRecording() {
    if (!(await allowed(app))) return;
    if (await app.act("recording.start", { name: "" }) !== undefined) await readStatus();
  }

  // --- 検索 -------------------------------------------------------------------------------------

  function queryRange(): [number, number] | null {
    return scope === "view" ? viewRange : scope === "range" ? range : null;
  }

  async function search() {
    save("eventLog.query", JSON.stringify(cond));
    save("eventLog.scope", scope === "range" ? "all" : scope);
    try {
      const query = toQuery(cond, queryRange());
      found = await conn.call("recording.find", { query });
      searchError = null;
    } catch (e) {
      searchError = errorText(e);
      found = null;
    }
    selected = found?.events.length ? 0 : -1;
    tl.setMarks(SEARCH_OWNER, "recording", found ? searchMarks(found.events) : []);
  }

  function clearSearch() {
    found = null;
    selected = -1;
    tl.setMarks(SEARCH_OWNER, "recording", []);
  }

  async function choose(i: number, jump: boolean) {
    selected = i;
    await tick();
    box?.querySelector(`[data-row="${i}"]`)?.scrollIntoView({ block: "nearest" });
    const e = found?.events[i];
    if (jump && e) reveal({ source: "recording", t: e.t, ...(e.t_end !== null && e.t_end > e.t ? { tEnd: e.t_end } : {}) });
  }

  function step(dir: 1 | -1) {
    const n = found?.events.length ?? 0;
    if (!n) return;
    void choose(selected < 0 ? 0 : (selected + dir + n) % n, true);
  }

  function onKey(e: KeyboardEvent) {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const n = found?.events.length ?? 0;
      if (n) void choose(Math.min(n - 1, Math.max(0, selected + (e.key === "ArrowDown" ? 1 : -1))), false);
    } else if (e.key === "Enter" && selected >= 0) void choose(selected, true);
  }

  function exportCsv() {
    if (found) download(`events-${status?.name ?? "recording"}.csv`, eventsCsv(found.events, origin, tickS), "text/csv");
  }

  /** 統計 → 生データ: その 1 回の区間のすべての出来事を検索に */
  function showInterval(t: number, d: number) {
    cond = emptyCondition();
    range = [t, t + d + 1];
    scope = "range";
    tab = "search";
    void search();
  }

  // --- 区間の統計 ---------------------------------------------------------------------------------

  async function measure() {
    save("eventLog.start", JSON.stringify(startCond));
    save("eventLog.end", JSON.stringify(endCond));
    try {
      stats = await conn.call("timing.measure", { start: toQuery(startCond), end: toQuery(endCond), bins: 24 });
      statsError = null;
    } catch (e) {
      stats = null;
      statsError = errorText(e);
    }
    picked = null;
    tl.setMarks(OUTLIER_OWNER, "recording", stats?.outliers && hz ? outlierMarks(stats.outliers, hz) : []);
  }

  const hist = $derived<Histogram[]>(stats?.histogram ?? []);
  const outliers = $derived<Outlier[]>(stats?.outliers ?? []);
  const bars = $derived(hz ? histogramBars(hist, hz, picked, outliers) : []);
  const inBin = $derived<[number, number][]>(picked === null ? [] : intervalsInBin(stats?.intervals ?? [], hist, picked)
    .slice(0, 500));
  const dur = (ticks: number) => (hz ? formatDuration(ticks / hz) : String(ticks));

  // --- 概要 -------------------------------------------------------------------------------------

  /** stream・kind ごとの件数を数える（表示中の範囲か記録全体） */
  async function countKinds() {
    const r = overviewScope === "view" ? viewRange : null;
    const q = kindQuery(r);
    try {
      const out = await conn.call("recording.query", { ...q, limit: 1000 }, { quiet: true });
      const span = r ? r[1] - r[0] : status?.span ? status.span[1] - status.span[0] + 1 : 0;
      overview = { ...kindStats(out, hz, span), partial: !!out.partial };
      overviewError = null;
    } catch (e) {
      overview = null;
      overviewError = errorText(e);
    }
  }

  /** 概要 → 検索: その stream・kind の出来事（範囲も同じ） */
  function searchKind(k: KindStat) {
    cond = { ...emptyCondition(), stream: k.stream, kind: k.kind };
    scope = overviewScope;
    tab = "search";
    void search();
  }

  // --- 印とメモ -----------------------------------------------------------------------------------

  async function readNotes() {
    try {
      const [marks, calls] = await Promise.all([
        conn.call("recording.find", { query: { stream: "annotation", kind: "mark" } }, { quiet: true }),
        conn.call("recording.find", { query: { stream: "host", kind: "call", body: { method: "recording.mark" } } },
                  { quiet: true }),
      ]);
      notes = notesOf(marks.events ?? [], calls.events ?? []);
    } catch {
      notes = [];
    }
  }

  async function addNote() {
    const text = noteText.trim();
    if (!text || !(await allowed(app))) return;
    const t = noteAt === "selection" && selection !== null ? selection : undefined;
    const out = await app.act("recording.mark", t === undefined ? { text } : { text, t });
    if (out === undefined) return;
    noteText = "";
    await readNotes();                                    // 時間軸の印の段は TimelineState が読み直す
  }

  const canNote = $derived(noteAt === "selection" ? selection !== null : !!status?.recording);
</script>

<div class="pane eventlog">
  <div class="pane-bar">
    <TabBar tabs={[{ id: "overview", label: $i18n("eventLog.overview"), title: $i18n("eventLog.overviewTip") },
                   { id: "search", label: $i18n("eventLog.search") }, { id: "stats", label: $i18n("eventLog.stats") },
                   { id: "notes", label: $i18n("eventLog.notes") }]} bind:value={tab} />
    <span class="spacer"></span>
    {#if has}
      <span class="muted mono" title={$i18n("eventLog.recordingTip")}>{status.name}{status.recording ? " ●" : ""}</span>
    {/if}
  </div>

  {#if !has}
    <div class="empty">
      <strong>{$i18n("eventLog.none")}</strong>
      <span>{$i18n("eventLog.noneHint")}</span>
      <div class="actions">
        <button onclick={() => dock.show("recordings")}>{$i18n("eventLog.openRecordings")}</button>
        <button onclick={startRecording}>{$i18n("recordings.start")}</button>
      </div>
    </div>
  {:else if tab === "overview"}
    <div class="pane-bar sub">
      <select bind:value={overviewScope} aria-label={$i18n("eventLog.scope")} title={$i18n("eventLog.scope")}>
        <option value="all">{$i18n("eventLog.scopeAll")}</option>
        <option value="view" disabled={!viewRange}>{$i18n("eventLog.scopeView")}</option>
      </select>
      <button class="small" onclick={() => void countKinds()}>{$i18n("common.refresh")}</button>
      {#if overview}
        <span class="muted">{$i18n("eventLog.overviewTotal", { n: overview.total.toLocaleString(), kinds: overview.rows.length })}</span>
      {/if}
    </div>
    {#if overviewError}<div class="error">{overviewError}</div>{/if}
    {#if overview?.partial}<div class="hint muted">{$i18n("eventLog.overviewPartial")}</div>{/if}
    {#if overview}
      <div class="rows">
        <table class="grid">
          <thead><tr><th>{$i18n("eventLog.stream")}</th><th>{$i18n("eventLog.kind")}</th><th>{$i18n("eventLog.countCol")}</th>
            <th>{$i18n("eventLog.share")}</th><th>{$i18n("eventLog.perSecond")}</th><th>{$i18n("eventLog.first")}</th>
            <th>{$i18n("eventLog.last")}</th><th></th></tr></thead>
          <tbody>
            {#each overview.rows as k (`${k.stream}.${k.kind}`)}
              <tr ondblclick={() => searchKind(k)}>
                <td class="mono">{k.stream}</td>
                <td class="mono">{k.kind}</td>
                <td class="mono num bar" style:--w="{overview.max ? (k.count / overview.max) * 100 : 0}%">{k.count.toLocaleString()}</td>
                <td class="mono num">{(k.share * 100).toFixed(k.share < 0.01 ? 2 : 1)} %</td>
                <td class="mono num">{Number.isFinite(k.perSecond) ? shortNumber(k.perSecond) : "—"}</td>
                <td><TimeLink to={{ source: "recording", t: k.first }} /></td>
                <td><TimeLink to={{ source: "recording", t: k.last }} /></td>
                <td><button class="small" title={$i18n("eventLog.searchKindTip")} onclick={() => searchKind(k)}>
                  {$i18n("eventLog.searchKind")}</button></td>
              </tr>
            {:else}
              <tr><td colspan="8" class="muted">{$i18n("eventLog.noEvents")}</td></tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/if}
  {:else if tab === "search"}
    <form class="pane-bar sub" onsubmit={(e) => { e.preventDefault(); void search(); }}>
      <ConditionFields bind:cond {streams} />
      <select bind:value={scope} aria-label={$i18n("eventLog.scope")} title={$i18n("eventLog.scope")}>
        <option value="all">{$i18n("eventLog.scopeAll")}</option>
        <option value="view" disabled={!viewRange}>{$i18n("eventLog.scopeView")}</option>
        {#if range}<option value="range">{$i18n("eventLog.scopeRange", { from: at(range[0]), to: at(range[1]) })}</option>{/if}
      </select>
      <button type="submit" class="primary">{$i18n("eventLog.find")}</button>
      {#if found}<button type="button" class="small" onclick={clearSearch}>{$i18n("eventLog.clear")}</button>{/if}
    </form>
    {#if searchError}<div class="error">{searchError}</div>{/if}
    {#if found}
      <div class="pane-bar sub">
        <span>{$i18n("eventLog.count", { n: found.count.toLocaleString() })}</span>
        {#if found.count > found.events.length}
          <span class="muted">{$i18n("eventLog.truncated", { n: found.events.length.toLocaleString() })}</span>
        {/if}
        <button class="small" disabled={!found.events.length} onclick={() => step(-1)} title={$i18n("eventLog.prevTip")}>
          {$i18n("eventLog.prev")}</button>
        <button class="small" disabled={!found.events.length} onclick={() => step(1)} title={$i18n("eventLog.nextTip")}>
          {$i18n("eventLog.next")}</button>
        {#if selected >= 0}<span class="muted">{selected + 1} / {found.events.length}</span>{/if}
        <span class="spacer"></span>
        <button class="small" onclick={() => { startCond = { ...cond }; tab = "stats"; }}>{$i18n("eventLog.useAsStart")}</button>
        <button class="small" onclick={() => { endCond = { ...cond }; tab = "stats"; }}>{$i18n("eventLog.useAsEnd")}</button>
        <button class="small" disabled={!found.events.length} onclick={exportCsv}>CSV</button>
      </div>
      <div class="rows" bind:this={box} tabindex="0" onkeydown={onKey} role="grid" aria-label={$i18n("eventLog.search")}>
        <table class="grid">
          <thead><tr><th>{$i18n("eventLog.time")}</th><th>{$i18n("eventLog.stream")}</th><th>{$i18n("eventLog.kind")}</th><th>a</th><th>b</th>
            <th>{$i18n("eventLog.summary")}</th></tr></thead>
          <tbody>
            {#each found.events as e, i (i)}
              <tr data-row={i} class:selected={i === selected} onclick={() => void choose(i, false)}
                  ondblclick={() => void choose(i, true)}>
                <td><TimeLink to={{ source: "recording", t: e.t, ...(e.t_end !== null && e.t_end > e.t ? { tEnd: e.t_end } : {}) }} /></td>
                <td class="mono">{e.stream}</td>
                <td class="mono">{e.kind}</td>
                <td class="mono num">{fieldText(e, "a")}</td>
                <td class="mono num">{fieldText(e, "b")}</td>
                <td class="sum" title={e.body ? JSON.stringify(e.body) : ""}>{summary(e)}</td>
              </tr>
            {:else}
              <tr><td colspan="6" class="muted">{$i18n("eventLog.noMatch")}</td></tr>
            {/each}
          </tbody>
        </table>
      </div>
    {:else if !searchError}
      <p class="hint muted">{$i18n("eventLog.searchHint")}</p>
    {/if}
  {:else if tab === "stats"}
    <form class="stats-form" onsubmit={(e) => { e.preventDefault(); void measure(); }}>
      <div><span class="label">{$i18n("eventLog.start")}</span><ConditionFields bind:cond={startCond} {streams} /></div>
      <div><span class="label">{$i18n("eventLog.end")}</span><ConditionFields bind:cond={endCond} {streams} /></div>
      <div>
        <button type="submit" class="primary">{$i18n("eventLog.measure")}</button>
        <span class="muted">{$i18n("eventLog.measureHint")}</span>
      </div>
    </form>
    {#if statsError}<div class="error">{statsError}</div>{/if}
    {#if stats}
      {#if !stats.count}
        <p class="hint muted">{$i18n("eventLog.noIntervals")}</p>
      {:else}
        <div class="summary">
          <span class="muted">{describeCondition(startCond)} → {describeCondition(endCond)}</span>
          <table class="grid figures">
            <thead><tr><th>{$i18n("eventLog.countCol")}</th><th>{$i18n("eventLog.min")}</th><th>{$i18n("eventLog.max")}</th>
              <th>{$i18n("eventLog.mean")}</th><th>σ</th><th>{$i18n("eventLog.median")}</th><th>p90</th><th>p99</th></tr></thead>
            <tbody><tr>
              <td class="mono">{stats.count}</td>
              <td><TimeLink to={{ source: "recording", t: stats.min_at, tEnd: stats.min_at + stats.min }}>{dur(stats.min)}</TimeLink></td>
              <td><TimeLink to={{ source: "recording", t: stats.max_at, tEnd: stats.max_at + stats.max }}>{dur(stats.max)}</TimeLink></td>
              <td class="mono">{dur(stats.mean)}</td>
              <td class="mono">{dur(stats.stdev)}</td>
              <td class="mono">{dur(stats.median)}</td>
              <td class="mono">{dur(stats.p90)}</td>
              <td class="mono">{dur(stats.p99)}</td>
            </tr></tbody>
          </table>
        </div>
        <div class="chart">
          <Chart bars={bars} height={140} xFormat={formatDuration} label={$i18n("eventLog.histogram")}
                 onpick={(_, bar) => (picked = bar)}
                 onopen={(_, bar) => { const first = bar === null ? undefined : intervalsInBin(stats.intervals, hist, bar)[0];
                                       if (first) reveal({ source: "recording", t: first[0], tEnd: first[0] + first[1] }); }} />
        </div>
        <div class="lists">
          <div>
            <div class="muted head">{picked === null ? $i18n("eventLog.pickBin") : $i18n("eventLog.inBin", {
              from: formatDuration(hist[picked].lo / hz), to: formatDuration(hist[picked].hi / hz), n: hist[picked].count })}</div>
            <table class="grid">
              <tbody>
                {#each inBin as [t, d], i (i)}
                  <tr ondblclick={() => reveal({ source: "recording", t, tEnd: t + d })}>
                    <td><TimeLink to={{ source: "recording", t, tEnd: t + d }} /></td>
                    <td class="mono">{dur(d)}</td>
                    <td><button class="small" onclick={() => showInterval(t, d)} title={$i18n("eventLog.eventsTip")}>
                      {$i18n("eventLog.events")}</button></td>
                  </tr>
                {/each}
              </tbody>
            </table>
          </div>
          <div>
            <div class="muted head">{$i18n("eventLog.outliers", { n: outliers.length })}</div>
            <table class="grid">
              <tbody>
                {#each outliers.slice(0, 500) as o, i (i)}
                  <tr ondblclick={() => reveal({ source: "recording", t: o.t, tEnd: o.t_end })}>
                    <td><TimeLink to={{ source: "recording", t: o.t, tEnd: o.t_end }} /></td>
                    <td class="mono warn">{dur(o.duration)}</td>
                    <td class="mono muted" title={$i18n("eventLog.scoreTip")}>{o.score.toFixed(1)}</td>
                    <td><button class="small" onclick={() => showInterval(o.t, o.duration)} title={$i18n("eventLog.eventsTip")}>
                      {$i18n("eventLog.events")}</button></td>
                  </tr>
                {:else}
                  <tr><td class="muted">{$i18n("eventLog.noOutliers")}</td></tr>
                {/each}
              </tbody>
            </table>
          </div>
        </div>
      {/if}
    {:else if !statsError}
      <p class="hint muted">{$i18n("eventLog.statsHint")}</p>
    {/if}
  {:else}
    <form class="pane-bar sub" onsubmit={(e) => { e.preventDefault(); void addNote(); }}>
      <input class="note" bind:value={noteText} placeholder={$i18n("eventLog.notePlaceholder")} />
      <select bind:value={noteAt} aria-label={$i18n("eventLog.noteAt")}>
        <option value="selection">{selection !== null ? $i18n("eventLog.atSelection", { t: at(selection) })
          : $i18n("eventLog.atSelectionNone")}</option>
        <option value="now" disabled={!status?.recording}>{$i18n("eventLog.atNow")}</option>
      </select>
      <button type="submit" disabled={!canNote || !noteText.trim()}>{$i18n("eventLog.addNote")}</button>
    </form>
    <div class="rows">
      <table class="grid">
        <thead><tr><th>{$i18n("eventLog.time")}</th><th>{$i18n("eventLog.by")}</th><th>{$i18n("eventLog.note")}</th></tr></thead>
        <tbody>
          {#each notes as n, i (i)}
            <tr ondblclick={() => reveal({ source: "recording", t: n.t })}>
              <td><TimeLink to={{ source: "recording", t: n.t }} /></td>
              <td class="who {n.role ?? ''}">{n.by ?? "—"}</td>
              <td>{n.text}</td>
            </tr>
          {:else}
            <tr><td colspan="3" class="muted">{$i18n("eventLog.noNotes")}</td></tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
</div>

<style>
  .eventlog { display: flex; flex-direction: column; }
  .sub { position: static; }
  .rows { flex: 1; overflow: auto; min-height: 60px; outline: none; }
  tr.selected td { background: var(--select); }
  tbody tr { cursor: default; }
  .num { text-align: right; }
  /* 件数の列: 最大に対する割合の棒を背景に */
  .bar { background: linear-gradient(to right, color-mix(in srgb, var(--accent) 22%, transparent) var(--w), transparent var(--w)); }
  .sum { max-width: 40ch; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .error { color: var(--bad); padding: 4px 8px; font-size: 12.5px; }
  .hint { padding: 8px 12px; margin: 0; }
  .stats-form { display: flex; flex-direction: column; gap: 4px; padding: 6px 8px; border-bottom: 1px solid var(--line); }
  .stats-form .label { display: inline-block; width: 4.5em; color: var(--muted); }
  .summary { padding: 4px 8px; }
  .figures { width: auto; }
  .chart { padding: 0 8px; }
  .lists { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; padding: 4px 8px; min-height: 0; }
  .lists .head { padding: 2px 0; }
  .warn { color: var(--warn); }
  .note { flex: 1; min-width: 12em; }
  .who.ai { color: var(--accent); }
  .who.human { color: var(--ok); }
  .who.hook { color: var(--warn); }
</style>
