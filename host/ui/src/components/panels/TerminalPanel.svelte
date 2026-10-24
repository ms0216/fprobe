<script lang="ts">
  // Terminal: RTT のチャンネル・SWO の ITM のポート・UART・セミホスティングを切り替えて表示（走行中も読む。
  // 色の制御コードを解釈）。書ける源は送信（行末を選ぶ）。RTT を始めていなければ Start RTT、SWO を受けていなければ
  // Start SWO（速さ・ITM と TPIU の設定）、UART はボーレート。ログの保存。
  // 行の時刻（VS Code の Serial Monitor・Tera Term の型）: Off / Time（受けた時刻）/ Device（記録中に受けた行の
  // デバイスの時刻）。記録中に受けた行は、時刻のクリック・行のダブルクリックで時間軸のその時刻へ。時間軸で選んだ時刻の
  // 行を選ぶ。行は見えている分だけ描く（多い行でも軽い）
  // 仮想端末（SEGGER RTT Viewer の Terminal 0〜F）: ファームが SEGGER_RTT_SetTerminal で切り替えたら、その源に
  // 「すべて」と端末ごとのタブを出す（デーモンが切り替えを文字から除いて位置を返す。lib/terminal の splitTerminals）
  import { tick, untrack } from "svelte";
  import { errorText } from "../../lib/api";
  import { shared } from "../../lib/context";
  import { allowed } from "../../lib/guard";
  import { i18n } from "../../lib/i18n";
  import { load, save } from "../../lib/prefs";
  import { TABLE_DIGITS, formatSeconds, timeText } from "../../lib/time";
  import { onTimeline } from "../../lib/timeline/events";
  import TabBar from "./TabBar.svelte";
  import {
    BAUDS, LINE_ENDINGS, SWO_BAUDS, TERMINAL_KEEP_CHARS, clockTime, swoConfigureParams, emptyScreen, feed, lineAtTime, lines, loadSources, plain,
    readSource, screenKey, splitTerminals, stampAt, terminalTabs, writeCall, type LineEnding, type Screen,
    type TerminalSource,
  } from "../../lib/terminal";

  const { app, conn, tl, reveal } = shared();
  const origins = tl.origins;
  const POLL_MS = 300;
  const ROW = 18;                                    // 行の高さ（CSS の画素。見えている行だけ描く）
  const KINDS = { rtt: "terminal.kind.rtt", swo: "terminal.kind.swo", uart: "terminal.kind.uart",
                  semihosting: "terminal.kind.semihosting" } as const;
  type TimeMode = "off" | "time" | "device";

  let sources = $state<TerminalSource[]>([]);
  let rtt = $state<boolean | null>(null);
  let rttBusy = $state(false);                       // RTT を探している（ELF に無ければすぐ理由が返る）
  let rttError = $state<string | null>(null);        // 始められなかった理由（ボタンの横に出す）
  let swo = $state(false);
  let active = $state(load("terminal.source", "rtt0"));
  let screens = $state<Record<string, Screen>>({});      // キー: 源の id（すべて）と「源/t番号」（仮想端末）
  // 仮想端末: 源ごとの読みの終わりの端末・使われた端末（切り替えが来た源だけ）・選んだ端末（null = すべて）
  const vtCurrent: Record<string, number> = {};
  let vtUsed = $state<Record<string, number[]>>({});
  let vtPick = $state<Record<string, number | null>>({});
  let hz = $state<Record<string, number | null>>({});
  let input = $state("");
  const saved = load("terminal.ending", "lf");
  let ending = $state<LineEnding>(saved in LINE_ENDINGS ? (saved as LineEnding) : "lf");
  const savedMode = load("terminal.time", "off");
  let timeMode = $state<TimeMode>(savedMode === "time" || savedMode === "device" ? savedMode : "off");
  let lost = $state(0);
  let box = $state<HTMLDivElement>();
  let scrollTop = $state(0);
  let viewHeight = $state(200);
  let follow = true;
  let picked = $state<number | null>(null);         // 選んだ行（時間軸の選択・クリック）
  let swoForm = $state(false);
  // SWO の速さ・ITM のポートは使う人が入れる（空から。2 MHz・ポート 0〜3 を暗黙に使わない）
  let swoBaud = $state(load("terminal.swoBaud", ""));
  let swoPorts = $state(load("terminal.swoPorts", ""));
  let swoSetup = $state(load("terminal.swoSetup", "0") === "1");
  let coreClock = $state(load("terminal.coreClock", ""));      // 既定の値を推測しない（使う側が入れる）
  const next: Record<string, number> = {};
  let polling = false;

  const online = $derived($app.link === "open");
  const connected = $derived($app.target?.state !== undefined && $app.target?.state !== "DISCONNECTED");
  const current = $derived(sources.find((s) => s.id === active) ?? sources[0]);
  const vtTabs = $derived(current ? terminalTabs(new Set(vtUsed[current.id] ?? [])) : []);
  const terminal = $derived(current && vtTabs.length ? vtPick[current.id] ?? null : null);
  const shownKey = $derived(current ? screenKey(current.id, terminal) : "");
  const screen = $derived(current ? screens[shownKey] ?? emptyScreen() : emptyScreen());
  const rows = $derived(lines(screen));
  const first = $derived(Math.max(0, Math.floor(scrollTop / ROW) - 10));
  const shown = $derived(rows.slice(first, Math.min(rows.length, first + Math.ceil(viewHeight / ROW) + 20)));
  const rate = $derived(current ? hz[current.id] ?? null : null);

  $effect(() => {
    if (online && connected) untrack(() => void refresh());
  });
  $effect(() => save("terminal.ending", ending));
  $effect(() => save("terminal.time", timeMode));
  $effect(() => {
    if (!online || !current) return;
    const timer = setInterval(() => void poll(), POLL_MS);
    return () => clearInterval(timer);
  });
  // 時間軸で選んだ時刻（記録）の行を選んで見せる
  $effect(() => onTimeline({
    select: (d) => {
      if (d.source !== "recording") return;
      const i = lineAtTime(screen, rows, d.t);
      if (i >= 0) showRow(i);
    },
  }));

  async function refresh() {
    try {
      const r = await loadSources(conn.call.bind(conn));
      sources = r.sources;
      rtt = r.rtt;
      swo = r.swo;
      if (!sources.some((s) => s.id === active) && sources.length) active = sources[0].id;
    } catch {
      sources = [];
      rtt = false;
    }
  }

  async function poll() {
    const source = current;
    if (!source || polling) return;
    polling = true;
    try {
      const r = await readSource(conn.call.bind(conn), source, next[source.id] ?? 0, true);
      if (r.next !== (next[source.id] ?? 0) || r.data) {
        next[source.id] = r.next;
        lost += r.lost;
        follow = box ? box.scrollHeight - box.scrollTop - box.clientHeight < ROW : true;
        const fed = { ...screens, [source.id]: feed(screens[source.id] ?? emptyScreen(), r.data, undefined, r.times) };
        if (r.terminals) {
          const split = splitTerminals(r, vtCurrent[source.id] ?? 0);
          vtCurrent[source.id] = split.current;
          for (const part of split.parts) {
            const k = screenKey(source.id, part.terminal);
            fed[k] = feed(fed[k] ?? emptyScreen(), part.data, undefined, part.times);
          }
          const used = new Set(vtUsed[source.id] ?? []);
          const switched = used.size > 0 || r.terminals.length > 0;
          if (switched) {
            for (const part of split.parts) used.add(part.terminal);
            for (const sw of r.terminals) used.add(sw.id);
            if (!used.has(0) && fed[screenKey(source.id, 0)]) used.add(0);
            if (used.size !== (vtUsed[source.id] ?? []).length) vtUsed = { ...vtUsed, [source.id]: [...used] };
          }
        }
        screens = fed;
        if (r.timebaseHz) hz = { ...hz, [source.id]: r.timebaseHz };
        if (follow) tick().then(toBottom);
      }
    } catch {
      /* 次の読みでまた試す */
    } finally {
      polling = false;
    }
  }

  function toBottom() {
    if (box) box.scrollTop = box.scrollHeight;
  }

  function showRow(i: number) {
    picked = i;
    follow = false;
    if (!box) return;
    const top = i * ROW;
    if (top < box.scrollTop || top + ROW > box.scrollTop + box.clientHeight) box.scrollTop = Math.max(0, top - box.clientHeight / 2);
  }

  function pick(id: string) {
    active = id;
    picked = null;
    save("terminal.source", id);
    tick().then(toBottom);
  }

  async function start() {
    if (!(await allowed(app))) return;
    rttBusy = true;
    rttError = null;
    try {
      await conn.call("rtt.start", {});
    } catch (e) {
      rttError = errorText(e);
    } finally {
      rttBusy = false;
    }
    await refresh();
  }

  /** SWO の受信を始める（ITM と TPIU も設定するなら swo.configure、ファームが設定済みなら swo.start） */
  async function startSwo(e: Event) {
    e.preventDefault();
    if (!(await allowed(app))) return;
    save("terminal.swoBaud", swoBaud);
    save("terminal.swoPorts", swoPorts);
    save("terminal.swoSetup", swoSetup ? "1" : "0");
    save("terminal.coreClock", coreClock);
    const configure = swoConfigureParams(swoPorts, swoBaud, coreClock);
    if (swoSetup ? !configure : !(Number(swoBaud) > 0)) return;
    const done = swoSetup
      ? await app.act("swo.configure", configure!)
      : await app.act("swo.start", { baud: Number(swoBaud) });
    if (done !== undefined || !swoSetup) swoForm = false;
    await refresh();
  }

  async function stopSwo() {
    if (!(await allowed(app))) return;
    await app.act("swo.stop", {});
    await refresh();
  }

  async function setBaud(baud: number) {
    if (!(await allowed(app))) return;
    const done = await app.act("uart.configure", { baud });
    if (done) sources = sources.map((s) => (s.id === "uart" ? { ...s, baud: done.baud } : s));
  }

  async function send(e: Event) {
    e.preventDefault();
    if (!current?.writable || !(await allowed(app))) return;
    const { method, params } = writeCall(current, input + LINE_ENDINGS[ending]);
    if ((await app.act(method, params)) !== undefined) input = "";
  }

  function clear() {
    if (current) {
      const id = current.id;
      screens = Object.fromEntries(Object.entries(screens).map(([k, v]) => [k, k === id || k.startsWith(`${id}/`) ? emptyScreen() : v]));
    }
    picked = null;
  }

  function pickTerminal(id: string) {
    if (!current) return;
    vtPick = { ...vtPick, [current.id]: id === "all" ? null : Number(id) };
    picked = null;
    tick().then(toBottom);
  }

  /** いまの源の文字をファイルに保存する（時刻を出していれば行の頭に） */
  function saveLog() {
    if (!current) return;
    const text = timeMode === "off" ? plain(screen)
      : rows.map((r) => `${timeOf(r.start)}  ${r.spans.map((s) => s.text).join("")}`).join("\n") + "\n";
    const url = URL.createObjectURL(new Blob([text], { type: "text/plain" }));
    const a = Object.assign(document.createElement("a"), { href: url, download: `fprobe-${shownKey.replace("/", "-")}.log` });
    a.click();
    URL.revokeObjectURL(url);
  }

  /** 行の始まりの時刻（表示の形） */
  function timeOf(start: number): string {
    const s = stampAt(screen, start);
    if (!s) return "";
    // デバイスの時刻: 記録の始めからの秒（時間軸・ほかのビューと同じ 0。記録が無ければタイムベースの 0 から）
    if (timeMode === "device") {
      if (s.t === null || !rate) return "—";
      return timeText($origins, { source: "recording", t: s.t }) ?? formatSeconds(s.t / rate, 1 / rate, TABLE_DIGITS);
    }
    return clockTime(s.wall);
  }

  function jump(start: number) {
    const s = stampAt(screen, start);
    if (s?.t !== null && s?.t !== undefined) reveal({ source: "recording", t: s.t });
  }

  const tAt = (start: number) => stampAt(screen, start)?.t ?? null;
</script>

<div class="pane terminal">
  {#if connected && sources.length}
    <div class="pane-bar">
      <TabBar tabs={sources.map((s) => ({ id: s.id, label: s.name, title: $i18n(KINDS[s.kind]) }))} value={current?.id}
              ariaLabel={$i18n("terminal.source")} onselect={pick} />
      {#if rtt === false}
        <button class="small" title={$i18n("terminal.notStartedHint")} disabled={rttBusy} onclick={start}>
          {$i18n(rttBusy ? "terminal.rttSearching" : "terminal.start")}</button>
        {#if rttError}<span class="rtt-error" title={rttError}>{rttError}</span>{/if}
      {/if}
      {#if !swo}
        <button class="small" title={$i18n("terminal.startSwoTip")} onclick={() => (swoForm = !swoForm)}>
          {$i18n("terminal.startSwo")}</button>
      {:else if current?.kind === "swo"}
        <button class="small" onclick={stopSwo}>{$i18n("terminal.stopSwo")}</button>
      {/if}
      {#if current?.kind === "uart"}
        <label class="baud" title={$i18n("terminal.baudTip")}>{$i18n("terminal.baud")}
          <select value={String(current.baud ?? "")} onchange={(e) => setBaud(Number((e.currentTarget as HTMLSelectElement).value))}>
            {#if !current.baud}<option value="">—</option>{/if}
            {#each BAUDS as b (b)}<option value={String(b)}>{b}</option>{/each}
          </select>
        </label>
      {/if}
      <span class="spacer"></span>
      <label title={$i18n("terminal.timestampsTip")}>{$i18n("terminal.timestamps")}
        <select bind:value={timeMode}>
          <option value="off">{$i18n("terminal.timeOff")}</option>
          <option value="time">{$i18n("terminal.timeClock")}</option>
          <option value="device">{$i18n("terminal.timeDevice")}</option>
        </select>
      </label>
      {#if lost}<span class="warn" title={$i18n("terminal.lostTip")}>{$i18n("terminal.lost", { n: lost })}</span>{/if}
      <button class="small" onclick={saveLog} title={$i18n("terminal.saveTip")}>{$i18n("terminal.save")}</button>
      <button class="small" onclick={clear}>{$i18n("terminal.clear")}</button>
    </div>
    {#if vtTabs.length}
      <div class="vt">
        <TabBar tabs={vtTabs.map((n) => ({ id: n === null ? "all" : String(n),
                                           label: n === null ? $i18n("terminal.vtAll") : $i18n("terminal.vt", { n: n.toString(16).toUpperCase() }) }))}
                value={terminal === null ? "all" : String(terminal)} ariaLabel={$i18n("terminal.vtTip")} onselect={pickTerminal} />
      </div>
    {/if}
    {#if swoForm && !swo}
      <form class="swo" onsubmit={startSwo}>
        <label>{$i18n("terminal.swoBaud")}
          <select bind:value={swoBaud} aria-required="true"><option value=""></option>{#each SWO_BAUDS as b (b)}<option value={String(b)}>{b.toLocaleString()}</option>{/each}</select><span
            class="req" aria-hidden="true">*</span>
        </label>
        <label title={$i18n("terminal.swoSetupTip")}><input type="checkbox" bind:checked={swoSetup} /> {$i18n("terminal.swoSetup")}</label>
        {#if swoSetup}
          <label title={$i18n("terminal.swoPortsTip")}>{$i18n("terminal.swoPorts")} <input class="num mono" bind:value={swoPorts}
            aria-required="true" spellcheck="false" /><span class="req" aria-hidden="true">*</span></label>
          <label>{$i18n("terminal.coreClock")} <input class="num mono" bind:value={coreClock} inputmode="numeric" /></label>
        {/if}
        <button class="primary small" disabled={swoSetup ? !swoConfigureParams(swoPorts, swoBaud, coreClock) : !(Number(swoBaud) > 0)}>
          {$i18n("terminal.startSwo")}</button>
        <button type="button" class="small" onclick={() => (swoForm = false)}>{$i18n("common.cancel")}</button>
      </form>
    {/if}
    {#if screen.discarded}
      <div class="muted small">{$i18n("terminal.discarded", { n: screen.discarded.toLocaleString(), limit: TERMINAL_KEEP_CHARS.toLocaleString() })}</div>
    {/if}
    <div class="out mono" bind:this={box} bind:clientHeight={viewHeight} onscroll={() => box && (scrollTop = box.scrollTop)}>
      <div style:height="{rows.length * ROW}px" class="space">
        {#each shown as row, k (first + k)}
          {@const i = first + k}
          {@const t = timeMode !== "off" ? tAt(row.start) : null}
          <!-- svelte-ignore a11y_no_static_element_interactions, a11y_click_events_have_key_events -->
          <div class="row" class:picked={i === picked} style:top="{i * ROW}px" onclick={() => (picked = i)}
               ondblclick={() => jump(row.start)}>
            {#if timeMode !== "off"}
              {#if t !== null}
                <button class="link time" title={$i18n("terminal.timeJumpTip")} onclick={(e) => { e.stopPropagation(); jump(row.start); }}>
                  {timeOf(row.start)}</button>
              {:else}
                <span class="time muted" title={$i18n("terminal.timeNoJump")}>{timeOf(row.start)}</span>
              {/if}
            {/if}
            <span class="text">{#each row.spans as s, j (j)}<span class="c{s.fg ?? ''}" class:b={s.bold}>{s.text}</span>{/each}</span>
          </div>
        {/each}
      </div>
    </div>
    {#if current?.writable}
      <form class="send" onsubmit={send}>
        <input class="mono" placeholder={$i18n("terminal.input")} aria-label={$i18n("terminal.input")} bind:value={input} />
        <select bind:value={ending} aria-label={$i18n("terminal.lineEnding")} title={$i18n("terminal.lineEnding")}>
          <option value="lf">LF</option><option value="cr">CR</option><option value="crlf">CR LF</option>
          <option value="none">{$i18n("terminal.noEnding")}</option>
        </select>
        <button disabled={!input && ending === "none"}>{$i18n("terminal.send")}</button>
      </form>
    {/if}
  {:else if connected}
    <div class="empty">
      <strong>{$i18n("terminal.notStarted")}</strong><span>{$i18n("terminal.notStartedHint")}</span>
      <div class="actions"><button onclick={start}>{$i18n("terminal.start")}</button></div>
    </div>
  {:else}
    <div class="empty"><span>{$i18n("hint.connectFirst")}</span></div>
  {/if}
</div>

<style>
  .terminal { display: flex; flex-direction: column; }
  label { display: flex; gap: 4px; align-items: center; font-size: 12px; }
  .warn { color: var(--warn); font-size: 12px; }
  .swo { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; padding: 4px 8px; border-bottom: 1px solid var(--line);
         background: var(--bg); }
  .num { width: 100px; }
  .out { flex: 1; min-height: 0; overflow: auto; font-size: 12.5px; position: relative; }
  .space { position: relative; min-width: 100%; }
  .row { position: absolute; left: 0; right: 0; height: 18px; line-height: 18px; padding: 0 10px; white-space: pre;
         display: flex; gap: 10px; cursor: default; }
  .row:hover { background: color-mix(in srgb, var(--select) 50%, transparent); }
  .row.picked { background: var(--select); }
  .time { flex: none; min-width: 12ch; text-align: right; font: inherit; font-size: 11.5px; }
  .text { flex: none; }
  .send { display: flex; gap: 6px; padding: 4px 8px; border-top: 1px solid var(--line); }
  .send input { flex: 1; }
  .b { font-weight: 700; }
  /* ANSI の 16 色（明暗の両方で読める色） */
  .c0 { color: var(--muted); } .c1 { color: #d14; } .c2 { color: #1a7f37; } .c3 { color: #b08800; }
  .c4 { color: #0969da; } .c5 { color: #8250df; } .c6 { color: #1b7c83; } .c7 { color: var(--text); }
  .c8 { color: var(--muted); } .c9 { color: #ff5c5c; } .c10 { color: #2da44e; } .c11 { color: #d4a72c; }
  .c12 { color: #54aeff; } .c13 { color: #c297ff; } .c14 { color: #39c5cf; } .c15 { color: var(--text); }
  .vt { padding: 0 8px; border-bottom: 1px solid var(--line); }
  .rtt-error { color: var(--bad); font-size: 12px; max-width: 40ch; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
</style>
