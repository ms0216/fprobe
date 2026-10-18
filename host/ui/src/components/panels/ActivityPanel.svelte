<script lang="ts">
  // Activity: AI と人の操作の記録（誰が・何を・結果）。fprobe 独自（人と AI が同じプローブを使う調停）。
  // 利用者・役割（AI / Human / Script / Hook）・誤りだけで絞る。記録中・開いた記録に入っている操作は、時刻のリンク・
  // 行のダブルクリックで時間軸のその時刻へ（記録の stream host の call。lib/activity.ts）、時間軸の選択で近い行を選ぶ
  import { tick } from "svelte";
  import { filterActivity, loadHostCalls, ROLES, roleOf, callKey, type HostCallIndex, type RoleFilter } from "../../lib/activity";
  import { shared } from "../../lib/context";
  import { clock, outcome, seconds } from "../../lib/format";
  import { i18n, type Key } from "../../lib/i18n";
  import { load, save } from "../../lib/prefs";
  import type { ActivityEntry } from "../../lib/state";
  import { onRecording } from "../../lib/notify";
  import { onTimeline } from "../../lib/timeline/events";
  import TimeLink from "./TimeLink.svelte";

  const REREAD_MS = 800;
  const ROLE_LABEL: Record<(typeof ROLES)[number], Key> = { ai: "activity.roleAi", human: "activity.roleHuman",
                                                             script: "activity.roleScript", hook: "activity.roleHook" };

  const { app, conn, reveal } = shared();
  let box = $state<HTMLDivElement>();
  let follow = $state(true);              // いちばん下を見ているときだけ新しい行に追従する
  let loading = false;
  let who = $state("");
  let role = $state<RoleFilter>(load("activity.role", "") as RoleFilter);
  let errorsOnly = $state(load("activity.errorsOnly", "0") === "1");
  let selected = $state<number | null>(null);
  let calls = $state<{ name: string; index: HostCallIndex } | null>(null);   // 記録の操作の索引
  let recording = $state(false);                                               // 記録中（索引を読み足す）
  let notice = $state<string | null>(null);
  let stale = false;                                                           // 索引の後に記録へ操作が入った

  const linked = $derived($app.link === "open");          // つながってから読む（つなぎ直したら読み直す）
  const names = $derived([...new Set($app.activity.map((e) => e.client))].sort());
  const rows = $derived(filterActivity($app.activity, { who, role, errorsOnly }));
  const chosen = $derived($app.activity.find((e) => e.seq === selected) ?? null);

  $effect(() => save("activity.role", role));
  $effect(() => save("activity.errorsOnly", errorsOnly ? "1" : "0"));
  $effect(() => {
    rows.length;
    if (follow) tick().then(() => box && (box.scrollTop = box.scrollHeight));
  });
  // 記録が変わったら（通知 recording。だれが変えても）索引を読み直す。記録中の操作は記録にも入るので、少し待って
  // まとめて読み直す（読み直しは quiet の読みだけなので、それ自体は操作の記録に入らず、通知が続かない）
  $effect(() => {
    if (!linked) return;
    void readCalls();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const stops = [
      onRecording(conn, () => void readCalls()),
      conn.on("activity", () => {
        if (!recording) return;
        stale = true;
        clearTimeout(timer);
        timer = setTimeout(() => void readCalls(), REREAD_MS);
      }),
    ];
    return () => {
      clearTimeout(timer);
      stops.forEach((stop) => stop());
    };
  });
  // 時間軸で選んだ時刻（記録）の直前の操作の行を選ぶ
  $effect(() => onTimeline({
    select: async (d) => {
      if (d.source !== "recording") return;
      const index = await fresh();
      const key = index?.keyAt(d.t);
      const e = key ? $app.activity.find((x) => callKey(x) === key) : undefined;
      if (e) await choose(e.seq);
    },
  }));

  async function readCalls() {
    stale = false;
    calls = await loadHostCalls(conn.call.bind(conn));
    try {
      recording = !!(await conn.call("recording.status", {}, { quiet: true }))?.recording;
    } catch {
      recording = false;
    }
  }

  /** 索引（記録中に操作が増えていたら読み直す） */
  async function fresh(): Promise<HostCallIndex | null> {
    if (stale || !calls) await readCalls();
    return calls?.index ?? null;
  }

  const timeOf = (e: ActivityEntry) => calls?.index.timeOf(e);

  async function jump(e: ActivityEntry) {
    const index = await fresh();
    const t = index?.timeOf(e);
    if (t === undefined) {
      notice = calls ? $i18n("activity.notRecorded") : $i18n("activity.noRecording");
      return;
    }
    notice = null;
    reveal({ source: "recording", t });
  }

  async function choose(seq: number) {
    selected = seq;
    follow = false;
    await tick();
    box?.querySelector(`[data-seq="${seq}"]`)?.scrollIntoView({ block: "nearest" });
  }

  async function onScroll() {
    if (!box) return;
    follow = box.scrollHeight - box.scrollTop - box.clientHeight < 8;
    if (box.scrollTop < 40 && $app.hasOlder && !loading) {
      loading = true;
      const before = box.scrollHeight;
      await app.loadOlder();
      await tick();
      box.scrollTop += box.scrollHeight - before;    // 読み足した分だけずらし、見ていた行を動かさない
      loading = false;
    }
  }

  function onKey(e: KeyboardEvent, entry: ActivityEntry) {
    if (e.key === "Enter") void jump(entry);
    else if (e.key === " ") {
      e.preventDefault();
      selected = selected === entry.seq ? null : entry.seq;
    }
  }

  const pretty = (v: unknown) => JSON.stringify(v, null, 2);
</script>

<div class="pane activity">
  <div class="pane-bar">
    <select bind:value={who} aria-label={$i18n("activity.everyone")}>
      <option value="">{$i18n("activity.everyone")}</option>
      {#each names as n (n)}<option value={n}>{n}</option>{/each}
    </select>
    <select bind:value={role} aria-label={$i18n("activity.role")}>
      <option value="">{$i18n("activity.allRoles")}</option>
      {#each ROLES as r (r)}<option value={r}>{$i18n(ROLE_LABEL[r])}</option>{/each}
    </select>
    <label><input type="checkbox" bind:checked={errorsOnly} /> {$i18n("activity.errorsOnly")}</label>
    {#if notice}<span class="muted notice">{notice}</span>{/if}
    <span class="spacer"></span>
    {#if calls}<span class="muted" title={$i18n("activity.linkedTip")}>{$i18n("activity.linked", { name: calls.name })}</span>{/if}
    {#if !follow}
      <button class="small" onclick={() => { follow = true; if (box) box.scrollTop = box.scrollHeight; }}>
        {$i18n("activity.latest")}</button>
    {/if}
  </div>
  <div class="rows" bind:this={box} onscroll={onScroll}>
    {#if $app.hasOlder}
      <div class="more muted">{$i18n("activity.olderHint")}
        <button class="small" onclick={() => app.loadOlder()}>{$i18n("activity.older")}</button></div>
    {/if}
    {#each rows as e (e.seq)}
      {@const t = timeOf(e)}
      <div class="row mono" class:error={e.error !== undefined} class:selected={e.seq === selected} data-seq={e.seq}
           role="button" tabindex="0" title={$i18n("activity.rowTip")}
           onclick={() => (selected = selected === e.seq ? null : e.seq)} ondblclick={() => void jump(e)}
           onkeydown={(k) => onKey(k, e)}>
        <span>{#if t !== undefined}<TimeLink to={{ source: "recording", t }}>{clock(e.time)}</TimeLink>
          {:else}<span class="muted">{clock(e.time)}</span>{/if}</span>
        <span class="who {roleOf(e)}">{e.client}</span>
        <span class="method">{e.method}</span>
        <span class="muted secs">{seconds(e.seconds)}</span>
        <span class="out">{outcome(e)}</span>
      </div>
    {:else}
      <div class="muted empty">{$i18n(errorsOnly || role || who ? "activity.noMatch" : "activity.none")}</div>
    {/each}
  </div>
  {#if chosen}
    <div class="detail mono">
      <div><strong>#{chosen.seq}</strong> {chosen.time} {chosen.client} ({roleOf(chosen)}) {chosen.method}
        {seconds(chosen.seconds)}
        <button class="small" onclick={() => chosen && void jump(chosen)}>{$i18n("activity.showInTimeline")}</button></div>
      <div class="cols">
        <div><div class="muted">{$i18n("activity.params")}</div><pre>{pretty(chosen.params)}</pre></div>
        <div>
          <div class="muted">{chosen.error !== undefined ? $i18n("activity.error") : $i18n("activity.result")}</div>
          <pre class:error={chosen.error !== undefined}>{chosen.error ?? pretty(chosen.result)}</pre>
        </div>
      </div>
    </div>
  {/if}
</div>

<style>
  .activity { display: flex; flex-direction: column; }
  .rows { flex: 1; overflow: auto; min-height: 60px; padding: 2px 4px; }
  .row {
    display: grid; grid-template-columns: 64px 90px minmax(140px, 220px) 56px 1fr; gap: 8px; width: 100%;
    text-align: left; border-radius: 4px; padding: 2px 6px; font-size: 12.5px; cursor: default;
  }
  .row:hover { background: var(--select); }
  .row:focus-visible { outline: 1px solid var(--accent); }
  .row.selected { background: var(--select); outline: 1px solid var(--accent); }
  .row.error .out { color: var(--bad); }
  .row > span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .secs { text-align: right; }
  .who.ai { color: var(--accent); }
  .who.human { color: var(--ok); }
  .who.hook { color: var(--warn); }
  .more, .empty { padding: 6px; }
  .notice { font-size: 12px; }
  .detail { border-top: 1px solid var(--line); padding: 6px 8px; max-height: 40%; overflow: auto; font-size: 12px; }
  .cols { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  pre { margin: 4px 0; white-space: pre-wrap; word-break: break-all; }
  pre.error { color: var(--bad); }
</style>
