<script lang="ts">
  // Breakpoints: ブレークポイントとウォッチポイントの一覧（外す・すべて外す）。行をクリックするとそのソースへ。
  // ブレークポイントごとの条件・ヒット カウント・ログ メッセージの編集（VS Code の Edit Condition / Hit Count /
  // Log Message、Ozone の Breakpoint Properties）、ログ・ブレークポイントの記録（時刻があれば時間軸へ）、
  // ベクタ キャッチ（IAR・J-Link の Vector Catch）、Flash ブレークポイントの様子、デバッグの方式（停止 / モニタ。
  // J-Link の Monitor Mode）（詳細に畳む）
  import { onDestroy } from "svelte";
  import {
    BreakpointLog, VECTOR_CATCH, catchState, changesBreakpoints, changesCatch, editOf, formatLog, logMarks, setParams, toggled,
    type ActionEdit, type LogEntry, type VectorCatchState,
  } from "../../lib/breakpoints";
  import { shared } from "../../lib/context";
  import { allowed } from "../../lib/guard";
  import { i18n, t, type Key } from "../../lib/i18n";
  import { load, save } from "../../lib/prefs";
  import { nearestAtOrBefore, onTimeline } from "../../lib/timeline/events";
  import TimeLink from "./TimeLink.svelte";
  import { hex32 } from "./format";

  const { app, conn, dbg, ui, tl, reveal, safety } = shared();
  const online = $derived($app.link === "open");
  const connected = $derived(!!$app.target?.state && $app.target.state !== "DISCONNECTED");

  let editing = $state<string | null>(null);            // 編集中のブレークポイントの location
  let edit = $state<ActionEdit>({ condition: "", hitCount: "", logMessage: "" });
  let editError = $state<string | null>(null);
  let vc = $state<VectorCatchState | null>(null);
  let entries = $state<LogEntry[]>([]);
  let selected = $state<number | null>(null);           // 選んだログの seq
  let flashBp = $state<any>(null);
  let mode = $state<any>(null);                         // target.debug_mode.get
  const MODES = ["halt", "monitor"] as const;
  let open = $state<Record<string, boolean>>(sections());
  let logList = $state<HTMLElement>();

  /** 開いている詳細（ログ・ベクタ キャッチ・Flash）を覚える */
  function sections(): Record<string, boolean> {
    try {
      return JSON.parse(load("breakpoints.sections", '{"log":true}'));
    } catch {
      return { log: true };
    }
  }

  async function go(file: string | undefined, line: number | undefined) {
    if (!file || line === undefined) return;
    await dbg.openFile(file);
    ui.select(file, line);
  }

  function startEdit(location: string, action: any) {
    editing = location;
    edit = editOf(action);
    editError = null;
  }

  async function apply() {
    if (!editing) return;
    let params;
    try {
      params = setParams(editing, edit);
    } catch {
      editError = t("bp.badHitCount");
      return;
    }
    if (!(await allowed(app)) || !(await safety.confirmHalt("breakpoint", t))) return;
    if ((await app.act("debug.breakpoint.set", params)) !== undefined) editing = null;
  }

  function onEditKey(e: KeyboardEvent) {
    if (e.key === "Enter") void apply();
    else if (e.key === "Escape") editing = null;
  }

  // --- ログ・ブレークポイントの記録（1 秒ごとに続きを読む）・時間軸の印 ---
  const feed = new BreakpointLog((m, p, o) => conn.call(m, p, o));
  const stopFeed = feed.subscribe((e) => (entries = e));
  const timer = setInterval(() => {
    if (online && connected) void feed.poll();
  }, 1000);
  $effect(() => {
    if (online) void feed.poll();
  });
  $effect(() => tl.setMarks("breakpoints.log", "recording", logMarks(entries)));
  onDestroy(() => {
    clearInterval(timer);
    stopFeed();
    tl.setMarks("breakpoints.log", "recording", []);
  });
  // 時間軸で選んだ時刻に当たるログの行を選ぶ（記録の時刻を持つ行だけ）
  $effect(() => onTimeline({
    select: (d) => {
      if (d.source !== "recording") return;
      const timed = entries.filter((e) => e.t !== undefined);
      const i = nearestAtOrBefore(timed.map((e) => e.t!), d.t);
      if (i < 0) return;
      selected = timed[i].seq;
      logList?.querySelector(`[data-seq="${selected}"]`)?.scrollIntoView({ block: "nearest" });
    },
  }));
  const revealEntry = (e: LogEntry) => e.t !== undefined && reveal({ source: "recording", t: e.t });

  // --- ベクタ キャッチ・Flash ブレークポイント（接続したら・誰かが変えたら読む） ---
  $effect(() => {
    if (online && connected) void readCatch();
    else vc = null;
  });
  $effect(() => conn.on("activity", (e: { method: string; params?: unknown }) => {
    if (changesCatch(e) && connected) void readCatch();
    if (changesBreakpoints(e) && open.flash) void readFlash();
    if (e.method === "target.debug_mode.set" && connected) void readMode();
  }));
  $effect(() => {
    if (online && connected) void readMode();
    else mode = null;
  });

  async function readMode() {
    try {
      mode = await conn.call("target.debug_mode.get", {}, { quiet: true });
    } catch {
      mode = null;
    }
  }

  async function setMode(m: (typeof MODES)[number]) {
    if (!mode || !(await allowed(app))) return;
    const r = await app.act("target.debug_mode.set", { mode: m });
    mode = r ?? mode;
    if (!r) void readMode();                            // 断られた: いまの方式に戻す
  }

  const keeps = (m: any): string =>
    (m.keeps_running ?? []).map((e: any) => `${e.name} (0x${e.priority.toString(16).toUpperCase()})`).join(", ") || "-";
  $effect(() => {
    if (open.flash && online) void readFlash();
  });

  async function readCatch() {
    try {
      vc = catchState(await conn.call("target.vector_catch.get", {}, { quiet: true }), vc ?? undefined);
    } catch {
      vc = null;
    }
  }

  async function setCatch(name: string, on: boolean) {
    if (!vc || !(await allowed(app))) return;
    const r = await app.act("target.vector_catch", { catch: toggled(vc.enabled, name, on) as any });
    if (r) vc = catchState(r, vc);
  }

  async function readFlash() {
    try {
      flashBp = await conn.call("debug.breakpoint.flash", {}, { quiet: true });
    } catch {
      flashBp = null;
    }
  }

  async function setFlashEnabled(enabled: boolean) {
    if (!(await allowed(app))) return;
    const r = await app.act("debug.breakpoint.flash", { enabled });
    if (r) flashBp = r;
  }

  async function restoreFlash() {
    if (!(await allowed(app))) return;
    if (await app.act("debug.breakpoint.flash_restore")) await readFlash();
  }

  function toggleSection(name: string, e: Event) {
    open = { ...open, [name]: (e.currentTarget as HTMLDetailsElement).open };
    save("breakpoints.sections", JSON.stringify(open));
  }

  const tags = (a: any): string[] => [
    ...(a?.condition ? [t("bp.tagCondition", { c: a.condition })] : []),
    ...(a?.ignore_count ? [t("bp.tagHitCount", { n: a.ignore_count })] : []),
    ...(a && a.log !== null ? [t("bp.tagLog", { m: a.message ?? a.log.join(", ") })] : []),
  ];
</script>

<div class="pane">
  <div class="pane-bar">
    <span class="spacer"></span>
    <button class="small" disabled={!online || !$dbg.breakpoints.length && !$dbg.watchpoints.length}
            title={$i18n("tip.removeAllBreakpoints")} onclick={async () => (await allowed(app)) && dbg.removeAllBreakpoints()}>
      {$i18n("cmd.removeAllBreakpoints")}</button>
  </div>
  {#if $dbg.breakpoints.length || $dbg.watchpoints.length}
    <ul class="list mono">
      {#each $dbg.breakpoints as b (b.address)}
        <li>
          <span class="dot" class:log={b.action && b.action.log !== null} class:cond={b.action && b.action.log === null}></span>
          <button class="link" onclick={() => go(b.file, b.line)}>{b.location}</button>
          <span class="info">
            <span class="muted">{b.where ?? hex32(b.address)}</span>
            {#if b.kind}<span class="tag" title={$i18n("bp.kindTip")}>{b.kind}</span>{/if}
            {#each tags(b.action) as tag (tag)}<span class="tag action">{tag}</span>{/each}
            {#if b.action?.hits}<span class="muted">{$i18n("bp.hits", { n: b.action.hits })}</span>{/if}
            {#if b.tool}<span class="muted">{$i18n("bp.tool", { tool: b.tool })}</span>{/if}
            {#if b.action?.last_error}<span class="bad">{$i18n("bp.conditionError", { e: b.action.last_error })}</span>{/if}
          </span>
          <span class="buttons">
            <button class="small" title={$i18n("bp.editTip")} disabled={!online || !!b.tool}
                    onclick={() => (editing === b.location ? (editing = null) : startEdit(b.location, b.action))}>
              {$i18n("bp.edit")}</button>
            <button class="small" aria-label={$i18n("common.remove")} title={$i18n("common.remove")} disabled={!online}
                    onclick={async () => (await allowed(app)) && dbg.removeBreakpoint(b.location)}>×</button>
          </span>
        </li>
        {#if editing === b.location}
          <li class="editor">
            <!-- svelte-ignore a11y_no_static_element_interactions -->
            <div class="fields" onkeydown={onEditKey}>
              <label for="bp-cond">{$i18n("bp.condition")}</label>
              <input id="bp-cond" class="mono" bind:value={edit.condition} placeholder={$i18n("bp.conditionHint")} spellcheck="false" />
              <label for="bp-hit">{$i18n("bp.hitCount")}</label>
              <input id="bp-hit" class="mono" bind:value={edit.hitCount} placeholder={$i18n("bp.hitCountHint")} />
              <label for="bp-log">{$i18n("bp.logMessage")}</label>
              <input id="bp-log" class="mono" bind:value={edit.logMessage} placeholder={$i18n("bp.logMessageHint")} spellcheck="false" />
              <span></span>
              <span class="row">
                <button class="small primary" onclick={apply}>{$i18n("bp.apply")}</button>
                <button class="small" onclick={() => (editing = null)}>{$i18n("common.cancel")}</button>
                {#if editError}<span class="bad">{editError}</span>{/if}
              </span>
            </div>
          </li>
        {/if}
      {/each}
      {#each $dbg.watchpoints as w (w.address)}
        <li>
          <span class="watch"></span>
          <span>{hex32(w.address)} · {w.size} · {w.kind}</span>
          <span class="muted">{$i18n("breakpoints.watchpoints")}</span>
          <span class="buttons">
            <button class="small" aria-label={$i18n("common.remove")} title={$i18n("common.remove")} disabled={!online}
                    onclick={async () => (await allowed(app)) && dbg.clearWatchpoint(w.address)}>×</button>
          </span>
        </li>
      {/each}
    </ul>
  {:else}
    <div class="empty"><span>{$i18n("breakpoints.none")}</span><span>{$i18n("breakpoints.hint")}</span></div>
  {/if}

  <details open={open.log} ontoggle={(e) => toggleSection("log", e)}>
    <summary>{$i18n("bp.log")} {#if entries.length}<span class="count">{entries.length}</span>{/if}</summary>
    {#if entries.length}
      <div class="row pad">
        <span class="spacer"></span>
        <button class="small" title={$i18n("bp.clearLogTip")} onclick={() => feed.clear()}>{$i18n("bp.clearLog")}</button>
      </div>
      <div class="log" bind:this={logList}>
        <table class="grid">
          <thead><tr><th>{$i18n("bp.time")}</th><th>{$i18n("bp.location")}</th><th>{$i18n("bp.hit")}</th>
            <th>{$i18n("bp.message")}</th></tr></thead>
          <tbody>
            {#each entries as e (e.seq)}
              <tr data-seq={e.seq} class:selected={selected === e.seq} onclick={() => (selected = e.seq)}
                  ondblclick={() => revealEntry(e)}>
                <td><TimeLink to={e.t !== undefined ? { source: "recording", t: e.t } : null} /></td>
                <td class="mono"><button class="link" onclick={() => go(e.file, e.line)}>{e.file && e.line !== undefined
                  ? `${e.file}:${e.line}` : e.where}</button></td>
                <td class="mono">{e.hit}</td>
                <td class="mono">{formatLog(e)}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {:else}
      <div class="empty"><span>{$i18n("bp.logNone")}</span><span>{$i18n("bp.logNoneHint")}</span></div>
    {/if}
  </details>

  <details open={open.vc} ontoggle={(e) => toggleSection("vc", e)}>
    <summary>{$i18n("vc.title")}</summary>
    {#if vc}
      <div class="catch">
        {#each VECTOR_CATCH as v (v.name)}
          {@const missing = vc.unsupported.includes(v.name)}
          <label class:muted={missing} title={missing ? $i18n("vc.unsupported") : $i18n(`vc.tip.${v.name}` as Key)}>
            <input type="checkbox" checked={vc.enabled.includes(v.name)} disabled={!online || missing}
                   onchange={(e) => setCatch(v.name, e.currentTarget.checked)} />
            <span class="mono">{v.label}</span>
          </label>
        {/each}
      </div>
    {:else}
      <div class="empty"><span>{$i18n("vc.connect")}</span></div>
    {/if}
  </details>

  <details open={open.mode} ontoggle={(e) => toggleSection("mode", e)}>
    <summary title={$i18n("dm.tip")}>{$i18n("dm.title")}
      {#if mode}<span class="count">{$i18n(mode.mode === "monitor" ? "dm.monitor" : "dm.halt")}</span>{/if}</summary>
    {#if mode}
      <div class="row pad">
        {#each MODES as m (m)}
          <label class:muted={m === "monitor" && !mode.available}>
            <input type="radio" name="debug-mode" checked={mode.mode === m}
                   disabled={!online || (m === "monitor" && !mode.available)} onchange={() => setMode(m)} />
            {$i18n(m === "monitor" ? "dm.monitor" : "dm.halt")}</label>
        {/each}
        {#if mode.available}
          <span class="muted mono">{$i18n("dm.priority", { p: `0x${mode.priority.toString(16).toUpperCase()}` })}</span>
        {/if}
      </div>
      {#if mode.available}
        <div class="stats mono muted">{$i18n("dm.keeps", { list: keeps(mode) })}</div>
        {#if mode.stack_overflow}<div class="stats bad">{$i18n("dm.overflow")}</div>{/if}
      {:else}
        <div class="stats muted">{mode.reason}</div>
      {/if}
    {:else}
      <div class="empty"><span>{$i18n("dm.connect")}</span></div>
    {/if}
  </details>

  <details open={open.flash} ontoggle={(e) => toggleSection("flash", e)}>
    <summary title={$i18n("bp.flashTip")}>{$i18n("bp.flash")}</summary>
    {#if flashBp?.available}
      <div class="row pad">
        <label><input type="checkbox" checked={flashBp.enabled} disabled={!online}
                      onchange={(e) => setFlashEnabled(e.currentTarget.checked)} /> {$i18n("bp.flashEnabled")}</label>
        <span class="muted">{$i18n("bp.flashHardware", { n: flashBp.hardware.length })}</span>
        {#if flashBp.dormant.length}<span class="muted">{$i18n("bp.flashDormant", { n: flashBp.dormant.length })}</span>{/if}
        <span class="spacer"></span>
        <button class="small" disabled={!online} title={$i18n("bp.flashRestoreTip")} onclick={restoreFlash}>
          {$i18n("bp.flashRestore")}</button>
      </div>
      {#if flashBp.flash.length}
        <ul class="list mono">
          {#each flashBp.flash as f (f.address)}
            <li class="plain"><span>{hex32(f.address)}</span><span class="muted">{f.where ?? ""}</span>
              <span>{f.state === "written" ? $i18n("bp.flashWritten") : $i18n("bp.flashPending")}</span></li>
          {/each}
        </ul>
      {/if}
      <div class="stats mono muted">
        {$i18n("bp.flashStats")}: {Object.entries(flashBp.stats).map(([k, v]) => `${k} ${v}`).join(" · ")}
      </div>
    {:else}
      <div class="empty"><span>{$i18n("bp.flashUnavailable")}</span></div>
    {/if}
  </details>
</div>

<style>
  .list { list-style: none; margin: 0; padding: 4px 0; }
  li { display: grid; grid-template-columns: 14px auto 1fr auto; gap: 8px; align-items: center; padding: 2px 10px; }
  li.plain { grid-template-columns: auto 1fr auto; }
  li.editor { display: block; padding: 2px 10px 6px 32px; }
  .info { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; min-width: 0; }
  .buttons { display: flex; gap: 4px; }
  .dot, .watch { width: 9px; height: 9px; border-radius: 50%; background: var(--bad); }
  .dot.log { border-radius: 1px; transform: rotate(45deg); }               /* ログ（VS Code の Logpoint の菱形） */
  .dot.cond { box-shadow: inset 0 0 0 2px var(--panel); outline: 1px solid var(--bad); }
  .watch { border-radius: 2px; background: var(--warn); }
  .tag { font-size: 11px; border: 1px solid var(--line); border-radius: 4px; padding: 0 4px; color: var(--muted); }
  .tag.action { color: var(--accent); border-color: currentColor; }
  .bad { color: var(--bad); }
  .fields { display: grid; grid-template-columns: max-content 1fr; gap: 4px 8px; align-items: center; }
  .fields label { font-size: 12px; color: var(--muted); }
  .row { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
  .row.pad { padding: 4px 10px; }
  .spacer { flex: 1; }
  details { border-top: 1px solid var(--line); }
  summary { padding: 4px 10px; cursor: pointer; color: var(--muted); font-size: 12px; font-weight: 600; }
  .count { font-weight: 400; margin-left: 4px; }
  .log { max-height: 240px; overflow: auto; }
  .log tr { cursor: default; }
  .log tr.selected td { background: var(--select); }
  .catch { display: grid; grid-template-columns: repeat(auto-fill, minmax(110px, 1fr)); gap: 2px 8px; padding: 4px 10px 8px; }
  .catch label { display: inline-flex; gap: 4px; align-items: center; }
  .stats { padding: 2px 10px 6px; font-size: 11.5px; }
</style>
