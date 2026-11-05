<script lang="ts">
  import Hint from "./Hint.svelte";
  // Event Log（Tracealyzer / SystemView の Events・TRACE32 のトレースリスト。G07）: 記録の出来事の構造化された検索
  // （recording.find）と結果の表・時間軸の印、区間の統計（timing.measure: 分布図・長い順の上位 ↔ 生データ）、調査の印とメモ
  // （annotation.mark・recording.mark）、記録の概要（stream・kind ごとの件数・割合・1 秒あたり・最初・最後 → 検索へ）。
  // 行のダブルクリック・時刻のリンクで時間軸へ、時間軸の選択で近い行を選ぶ。
  // 計算は lib/eventlog.ts
  import { errorText } from "../../lib/api";
  import { tick, untrack } from "svelte";
  import { download } from "../../lib/chart";
  import { shared } from "../../lib/context";
  import {
    emptyCondition, eventsCsv, histogramBars, kindRows, notesOf,
    longestMarks, measureParams, searchMarks, toQuery, type Condition, type Histogram, type KindStat, type Note, type Longest,
    type RecEvent,
  } from "../../lib/eventlog";
  import { allowed } from "../../lib/guard";
  import { queryCut, topCut } from "../../lib/cut";
  import { parseCount } from "../../lib/system";
  import { i18n } from "../../lib/i18n";
  import { onCalls, onRecording } from "../../lib/notify";
  import { load, save } from "../../lib/prefs";
  import { formatDuration, timeText } from "../../lib/time";
  import { nearestAtOrBefore, onTimeline } from "../../lib/timeline/events";
  import EventLogNotes from "./eventlog/EventLogNotes.svelte";
  import EventLogOverview from "./eventlog/EventLogOverview.svelte";
  import EventLogSearch from "./eventlog/EventLogSearch.svelte";
  import EventLogStats from "./eventlog/EventLogStats.svelte";
  import TabBar from "./TabBar.svelte";

  type Tab = "overview" | "search" | "stats" | "notes";
  type Scope = "all" | "view" | "range";
  const SEARCH_OWNER = "eventLog", LONGEST_OWNER = "eventLog.longest";
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
  let streamsCut = $state<ReturnType<typeof queryCut>>(null);  // stream の選択肢を上限で切った
  const STREAMS_LIMIT = 100;
  const IN_BIN_SHOWN = 500;                              // 棒の中の区間の表に描く行の上限（切ったら数と上限を出す）

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
  // 分布図の棒の数・長い順の上位の数は使う人が入れる（空から。画面の数・API の既定を暗黙に使わない）
  let binsText = $state(load("eventLog.bins", ""));
  let longestText = $state(load("eventLog.longestN", ""));
  let picked = $state<number | null>(null);
  let statsError = $state<string | null>(null);

  // 概要（stream・kind ごとの件数）
  let overview = $state<{ rows: KindStat[]; total: number; max: number } | null>(null);
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
    tl.setMarks(LONGEST_OWNER, "recording", []);
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
        const out = await conn.call("recording.query", { sql: "SELECT DISTINCT stream FROM events", limit: STREAMS_LIMIT },
                                    { quiet: true });
        streams = (out.rows ?? []).map((r: unknown[]) => String(r[0]));
        streamsCut = queryCut(out, STREAMS_LIMIT);
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
    save("eventLog.bins", binsText);
    save("eventLog.longestN", longestText);
    const params = measureParams(startCond, endCond, binsText, longestText);
    if (!params) return;
    try {
      stats = await conn.call("timing.measure", params);
      statsError = null;
    } catch (e) {
      stats = null;
      statsError = errorText(e);
    }
    picked = null;
    tl.setMarks(LONGEST_OWNER, "recording", stats?.longest && hz ? longestMarks(stats.longest, hz) : []);
  }

  const hist = $derived<Histogram[]>(stats?.histogram ?? []);
  const longest = $derived<Longest[]>(stats?.longest ?? []);
  const bars = $derived(hz ? histogramBars(hist, hz, picked) : []);
  const inBinAll = $derived<[number, number][]>(picked === null ? [] : (hist[picked]?.intervals ?? []));
  const inBin = $derived(inBinAll.slice(0, IN_BIN_SHOWN));
  const inBinCut = $derived(topCut(inBin.length, inBinAll.length, IN_BIN_SHOWN));
  const canMeasure = $derived(parseCount(binsText) !== null && parseCount(longestText) !== null);
  const dur = (ticks: number) => (hz ? formatDuration(ticks / hz) : String(ticks));

  // --- 概要 -------------------------------------------------------------------------------------

  /** stream・kind ごとの件数を数える（表示中の範囲か記録全体） */
  async function countKinds() {
    const r = overviewScope === "view" ? viewRange : null;
    try {
      overview = kindRows(await conn.call("recording.kinds", r ? { t0: r[0], t1: r[1] } : {}, { quiet: true }));
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
      const marks = await conn.call("recording.find", { query: { stream: "annotation", kind: "mark" } }, { quiet: true });
      notes = notesOf(marks.events ?? []);
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
      <Hint text={$i18n("eventLog.noneHint")} />
      <div class="actions">
        <button onclick={() => dock.show("recordings")}>{$i18n("eventLog.openRecordings")}</button>
        <button onclick={startRecording}>{$i18n("recordings.start")}</button>
      </div>
    </div>
  {:else if tab === "overview"}
    <EventLogOverview {overview} error={overviewError} bind:scope={overviewScope} {viewRange} oncount={() => void countKinds()}
                      onsearch={searchKind} />
  {:else if tab === "search"}
    <EventLogSearch bind:cond bind:scope bind:box {streams} {streamsCut} {viewRange} {range} {at} {found} error={searchError}
                    {selected} onsearch={() => void search()} onclear={clearSearch} onstep={step}
                    onchoose={(i, jump) => void choose(i, jump)} onkey={onKey} onexport={exportCsv}
                    onstart={() => { startCond = { ...cond }; tab = "stats"; }}
                    onend={() => { endCond = { ...cond }; tab = "stats"; }} />
  {:else if tab === "stats"}
    <EventLogStats bind:startCond bind:endCond bind:binsText bind:longestText bind:picked {streams} {stats} error={statsError}
                   {canMeasure} {hz} {hist} {longest} {bars} {inBin} {inBinCut} {dur} {reveal} onmeasure={() => void measure()}
                   onevents={showInterval} />
  {:else}
    <EventLogNotes {notes} bind:noteText bind:noteAt {selection} recording={!!status?.recording} {canNote} {at} {reveal}
                   onadd={() => void addNote()} />
  {/if}
</div>

<style>
  .eventlog { display: flex; flex-direction: column; }
</style>
