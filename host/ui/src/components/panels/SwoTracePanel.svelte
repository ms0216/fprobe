<script lang="ts">
  // SWO Trace（IAR の SWO Trace・STM32CubeIDE の SWV Data Trace / Exception Trace）: ターゲットを止めずに ITM・DWT を
  // 設定し（例外トレース・タイムスタンプ・データトレース = 変数へのアクセスを値・PC・時刻つきで）、結果を一覧にする。
  // 時刻はタイムベース: 時刻のリンク・行のダブルクリックで時間軸へ、時間軸の選択で近い行を選ぶ、アクセスを時間軸に印
  import { errorText } from "../../lib/api";
  import { tick, untrack } from "svelte";
  import { shared } from "../../lib/context";
  import { allowed } from "../../lib/guard";
  import { i18n } from "../../lib/i18n";
  import { load, save } from "../../lib/prefs";
  import {
    MAX_COMPARATORS, configureParams, exceptionRows, tailOffset, valueText, type Access, type TraceSettings,
  } from "../../lib/swotrace";
  import { SWO_BAUDS } from "../../lib/terminal";
  import { nearestAtOrBefore, onTimeline } from "../../lib/timeline/events";
  import type { Origin } from "../../lib/time";
  import Empty from "./Empty.svelte";
  import TimeLink from "./TimeLink.svelte";

  const LIMIT = 500;
  const POLL_MS = 700;
  const { app, conn, tl, showAddress, reveal } = shared();

  const loaded = (() => {
    try {
      return JSON.parse(load("swoTrace.settings", "null")) as TraceSettings | null;
    } catch {
      return null;
    }
  })();
  let settings = $state<TraceSettings>(loaded ?? { exceptions: true, timestamps: false, swoBaud: 2_000_000,
                                                   coreClockHz: null, variables: [{ expression: "", access: "write" }] });
  let result = $state<any>(null);
  let error = $state<string | null>(null);
  let picked = $state<number | null>(null);
  let filter = $state("");
  let box = $state<HTMLDivElement>();
  let reading = false;

  const connected = $derived(!!$app.target?.state && $app.target.state !== "DISCONNECTED");
  const running = $derived(!!result?.running);
  const accesses = $derived<Access[]>(result?.accesses ?? []);
  const times = $derived(accesses.map((a) => a.t));
  const exceptions = $derived(exceptionRows(result?.exceptions?.counts ?? {}));

  $effect(() => save("swoTrace.settings", JSON.stringify(settings)));
  // 取っている間は周期的に読み直す
  $effect(() => {
    if (!running || !connected) return;
    const timer = setInterval(() => void read(), POLL_MS);
    return () => clearInterval(timer);
  });
  // 前の設定の結果があれば最初に読む（ほかの窓・AI が設定したものも）
  $effect(() => {
    if (connected) untrack(() => void read());
  });
  // アクセスを時間軸に印（記録の時刻）。閉じたら消す
  $effect(() => {
    tl.setMarks("swoTrace", "recording", accesses.map((a) => ({ t: a.t, label: `${a.name}=${valueText(a.value)}` })));
    return () => tl.setMarks("swoTrace", "recording", []);
  });
  $effect(() => onTimeline({
    select: (d) => {
      if (d.source !== "recording") return;
      const i = nearestAtOrBefore(times, d.t);
      if (i >= 0) show(i);
    },
  }));

  async function read() {
    if (reading) return;
    reading = true;
    try {
      const first = await conn.call("swo.trace", { offset: 0, limit: 1, ...(filter ? { name: filter } : {}) }, { quiet: true });
      result = await conn.call("swo.trace", { offset: tailOffset(first.total, LIMIT), limit: LIMIT,
                                              ...(filter ? { name: filter } : {}) }, { quiet: true });
      error = null;
    } catch (e) {
      const text = errorText(e);
      if (!/swo_configure/.test(text)) error = text;               // まだ設定していないのは誤りにしない
    } finally {
      reading = false;
    }
  }

  async function startTrace(e: Event) {
    e.preventDefault();
    if (!(await allowed(app))) return;
    const done = await app.act("swo.configure", configureParams(settings) as any);
    if (done !== undefined) await read();
  }

  async function stopTrace() {
    if (!(await allowed(app))) return;
    const done = await app.act("swo.unconfigure", {});
    if (done !== undefined) result = done;
  }

  function show(i: number) {
    picked = i;
    void tick().then(() => box?.querySelector(`[data-row="${i}"]`)?.scrollIntoView({ block: "nearest" }));
  }

  // 時刻は記録の始めからの秒（記録が無ければトレースの窓の始めから）
  const windowStart = $derived<Origin | undefined>(result?.window?.timebase_hz
    ? { t0: result.window.t0, tickS: 1 / result.window.timebase_hz } : undefined);
</script>

<div class="pane swo">
  {#if connected}
    <form class="pane-bar" onsubmit={startTrace}>
      <label><input type="checkbox" bind:checked={settings.exceptions} /> {$i18n("swoTrace.exceptions")}</label>
      <label title={$i18n("swoTrace.timestampsTip")}><input type="checkbox" bind:checked={settings.timestamps} /> {$i18n("swoTrace.timestamps")}</label>
      {#each settings.variables as v, i (i)}
        <span class="var">
          <input class="mono expr" placeholder={$i18n("swoTrace.variable")} aria-label={$i18n("swoTrace.variable")} bind:value={v.expression} />
          <select bind:value={v.access} aria-label={$i18n("swoTrace.access")}>
            <option value="write">{$i18n("swoTrace.write")}</option><option value="read">{$i18n("swoTrace.read")}</option>
            <option value="rw">{$i18n("swoTrace.readWrite")}</option>
          </select>
          {#if settings.variables.length > 1}
            <button type="button" class="small" aria-label={$i18n("common.remove")}
                    onclick={() => (settings.variables = settings.variables.filter((_, k) => k !== i))}>×</button>
          {/if}
        </span>
      {/each}
      {#if settings.variables.length < MAX_COMPARATORS}
        <button type="button" class="small" title={$i18n("swoTrace.addTip")}
                onclick={() => (settings.variables = [...settings.variables, { expression: "", access: "write" }])}>+</button>
      {/if}
      <label>{$i18n("terminal.swoBaud")}
        <select bind:value={settings.swoBaud}>{#each SWO_BAUDS as b (b)}<option value={b}>{b.toLocaleString()}</option>{/each}</select>
      </label>
      <label>{$i18n("terminal.coreClock")}
        <input class="num mono" value={settings.coreClockHz ?? ""} inputmode="numeric"
               onchange={(e) => (settings.coreClockHz = Number((e.currentTarget as HTMLInputElement).value) || null)} />
      </label>
      <span class="spacer"></span>
      {#if running}
        <button type="button" onclick={stopTrace}>{$i18n("swoTrace.stop")}</button>
      {:else}
        <button class="primary" title={$i18n("swoTrace.startTip")}>{$i18n("swoTrace.start")}</button>
      {/if}
    </form>
    {#if error}<p class="bad">{error}</p>{/if}
    {#if result}
      <div class="summary muted">
        <span>{$i18n("swoTrace.accesses", { n: result.total })}</span>
        {#if result.overflows || result.lost}<span class="warn">{$i18n("swoTrace.lost", { overflows: result.overflows, lost: result.lost })}</span>{/if}
        {#each exceptions as x (x.name)}<span class="exc">{x.name} <b>{x.count}</b></span>{/each}
        <span class="spacer"></span>
        <input class="mono filter" placeholder={$i18n("swoTrace.filter")} aria-label={$i18n("swoTrace.filter")}
               bind:value={filter} onchange={() => read()} />
      </div>
      <div class="rows" bind:this={box}>
        <table class="grid mono">
          <thead><tr><th>{$i18n("swoTrace.time")}</th><th>{$i18n("common.name")}</th><th>{$i18n("swoTrace.access")}</th>
            <th>{$i18n("common.value")}</th><th>PC</th></tr></thead>
          <tbody>
            {#each accesses as a, i (i)}
              <tr data-row={i} class:picked={i === picked} onclick={() => (picked = i)}
                  ondblclick={() => reveal({ source: "recording", t: a.t })}>
                <td><TimeLink to={{ source: "recording", t: a.t }} fallback={windowStart} /></td>
                <td>{a.name}</td>
                <td>{a.write ? "W" : "R"}</td>
                <td class="v">{valueText(a.value)}</td>
                <td>{#if a.pc !== undefined}<button class="link" onclick={(e) => { e.stopPropagation(); showAddress(a.pc!); }}>
                  {a.function ?? `0x${a.pc.toString(16)}`}{a.line ? ` (${a.file}:${a.line})` : ""}</button>{/if}</td>
              </tr>
            {:else}
              <tr><td colspan="5" class="muted">{$i18n("swoTrace.noAccesses")}</td></tr>
            {/each}
          </tbody>
        </table>
      </div>
    {:else if !error}
      <div class="empty"><strong>{$i18n("swoTrace.notStarted")}</strong><span>{$i18n("swoTrace.hint")}</span></div>
    {/if}
  {:else}
    <Empty need="connected" />
  {/if}
</div>

<style>
  .swo { display: flex; flex-direction: column; }
  label { display: flex; gap: 4px; align-items: center; font-size: 12px; }
  .var { display: flex; gap: 2px; }
  .expr { width: 120px; }
  .num { width: 90px; }
  .filter { width: 120px; }
  .summary { display: flex; gap: 12px; align-items: center; padding: 3px 8px; font-size: 12px; flex-wrap: wrap;
             border-bottom: 1px solid var(--line); }
  .summary .spacer { flex: 1; }
  .exc b { color: var(--text); }
  .warn { color: var(--warn); }
  .bad { color: var(--bad); padding: 4px 8px; margin: 0; font-size: 12px; }
  .rows { flex: 1; min-height: 0; overflow: auto; }
  tr.picked td { background: var(--select); }
  tbody tr { cursor: default; }
</style>
