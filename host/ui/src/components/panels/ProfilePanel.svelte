<script lang="ts">
  // Code Profile（Ozone の Code Profile・STM32CubeIDE の Statistical Profiling・MCUXpresso の SWO Profile・IAR の
  // Function Profiler / Code Coverage の型）: PC サンプリングの開始・停止、関数ごとの自分の時間の割合（信頼区間・サンプル数）
  // とカバレッジ（近似）、関数を開くと行ごと。時間軸の基準と選択のカーソルの間だけの集計（IAR の Timeline → Function
  // Profiler）。ソースの行番号に当たりの割合とカバレッジの色を出す（lib/profile.ts の sourceHeat を Source が読む）
  import { onDestroy } from "svelte";
  import { isNoData } from "../../lib/api";
  import { shared } from "../../lib/context";
  import SamplerNotice from "./SamplerNotice.svelte";
  import { onCalls } from "../../lib/notify";
  import { allowed } from "../../lib/guard";
  import { topCut } from "../../lib/cut";
  import { i18n } from "../../lib/i18n";
  import { Poller } from "../../lib/poll";
  import { load, save } from "../../lib/prefs";
  import {
    SymbolAddresses, heatOf, selectedRange, sourceHeat, type FunctionCoverage, type ProfileRow,
  } from "../../lib/profile";
  import { percent } from "../../lib/rtos";
  import { coreClock } from "../../lib/target";
  import LoadState from "./LoadState.svelte";

  const { app, conn, tl, showAddress } = shared();
  const quiet = { quiet: true };
  // 読む行の上限（API の top）。切れたら切る前の数（rows_total）と上限を出す
  const TOP = 500;                 // 関数の表
  const LINES_TOP = 1000;          // 開いた関数の行
  const HEAT_TOP = 100000;         // ソースの濃淡に使う行
  const symbols = new SymbolAddresses(conn);

  let method = $state(load("profile.method", "itm"));
  // 周期は使う人が選ぶ（画面の既定を持たない。選ぶまで始められない）
  const savedRate = Number(load("profile.rate", ""));
  let rate = $state<number | null>(savedRate > 0 ? savedRate : null);
  let scope = $state<"whole" | "selected">(load("profile.scope", "whole") === "selected" ? "selected" : "whole");
  let heat = $state(load("profile.heat", "1") === "1");
  $effect(() => save("profile.method", method));
  $effect(() => save("profile.rate", rate === null ? "" : String(rate)));
  $effect(() => save("profile.scope", scope));
  $effect(() => save("profile.heat", heat ? "1" : "0"));

  let result = $state<any>(null);            // profile.result（全体）か profile.aggregate（範囲）
  let summary = $state<any>(null);           // profile.result（全体。方法・取っているか・分類）
  let coverage = $state<Map<string, FunctionCoverage>>(new Map());
  let open = $state<string | null>(null);
  let lines = $state<ProfileRow[]>([]);
  let linesCut = $state<ReturnType<typeof topCut>>(null);
  let heatCut = $state<ReturnType<typeof topCut>>(null);
  let stopping = $state(false);
  let taken: string | null = null;           // 方法の欄を合わせた結果の方法（新しい結果のときだけ欄を合わせる）

  const online = $derived($app.link === "open");
  const connected = $derived(!!$app.target?.state && $app.target.state !== "DISCONNECTED");
  const running = $derived(!!summary?.running);
  const takenMethod = $derived(summary?.method ?? null);
  const range = $derived(selectedRange($tl, takenMethod));
  const span = $derived(scope === "selected" && "t0" in range ? range : null);

  // コア クロック（ITM の間隔・SWO の分周）: 利用者が入れた値はターゲットごとに覚え、無ければつないでいる
  // ターゲットの ELF の SystemCoreClock を読んだ値のまま欄に示す（使うかは利用者が確かめる。無ければ空）
  const targetName = $derived(($app.target?.target as string | undefined) ?? null);
  const clockKey = (name: string | null) => `profile.clock.${name ?? "?"}`;
  let clockMhz = $state<number | null>(null);
  let clockAuto = $state<string | null>(null);         // 既定の出どころ（欄のツールチップ）
  $effect(() => {
    const name = targetName;
    const saved = Number(load(clockKey(name), "0"));
    if (saved > 0) {
      clockMhz = saved;
      clockAuto = null;
    } else if (connected) {
      void coreClock(conn, name).then((c) => {
        if (name !== targetName || Number(load(clockKey(name), "0")) > 0) return;
        clockMhz = c?.mhz ?? null;
        clockAuto = c?.source ?? null;
      });
    }
  });
  function setClock(v: number) {
    clockMhz = v > 0 ? v : null;
    clockAuto = null;
    save(clockKey(targetName), String(v > 0 ? v : 0));
  }

  // 読み続け: 前の読みが終わってから、かかった時間に合わせて間をあける（要求を積もらせない。停止がすぐ効く）
  const poller = new Poller(refresh, { minMs: 1000, factor: 2, maxMs: 10000 });
  onDestroy(() => poller.dispose());
  $effect(() => {
    void span;
    void heat;
    if (online) void poller.refresh();
  });
  $effect(() => poller.poll(running && !stopping));
  $effect(() => onCalls(conn, ["profile.start", "profile.stop"], () => void poller.refresh()));
  onDestroy(() => sourceHeat.clear());

  async function refresh() {
    try {
      summary = await conn.call("profile.result", { by: "function", top: TOP }, quiet);
    } catch (e) {
      if (!isNoData(e)) throw e;                         // 読めなかった: 前の結果を出したまま誤りを出す
      summary = result = null;
      sourceHeat.clear();
      return;
    }
    if (summary.method && summary.method !== taken) {    // 方法の欄を結果に合わせる（ほかの利用者が始めたときも）
      taken = summary.method;
      method = summary.method;
    }
    const w = span;
    result = w ? await conn.call("profile.aggregate", { by: "function", t0: w.t0, t1: w.t1, top: TOP }, quiet) : summary;
    const cov = await conn.call("profile.coverage", {}, quiet);
    coverage = new Map((cov.functions as FunctionCoverage[]).map((f) => [f.function, f]));
    if (open) await loadLines(open);
    if (heat) {
      const all = w ? await conn.call("profile.aggregate", { by: "line", t0: w.t0, t1: w.t1, top: HEAT_TOP }, quiet)
                    : await conn.call("profile.result", { by: "line", top: HEAT_TOP }, quiet);
      sourceHeat.set(heatOf(all.rows, [...coverage.values()]));
      heatCut = topCut(all.rows.length, all.rows_total, HEAT_TOP);
    } else {
      sourceHeat.clear();
      heatCut = null;
    }
  }

  async function loadLines(fn: string) {
    const w = span;
    const r = w ? await conn.call("profile.aggregate", { by: "line", function: fn, t0: w.t0, t1: w.t1, top: LINES_TOP }, quiet)
                : await conn.call("profile.result", { by: "line", function: fn, top: LINES_TOP }, quiet);
    if (open !== fn) return;
    lines = [...r.rows].sort((a: ProfileRow, b: ProfileRow) => (a.line ?? 0) - (b.line ?? 0));
    linesCut = topCut(r.rows.length, r.rows_total, LINES_TOP);
  }

  async function toggle(fn: string) {
    open = open === fn ? null : fn;
    lines = [];
    linesCut = null;
    if (open) await loadLines(open).catch(() => {});
  }

  async function showLine(r: ProfileRow) {
    const address = await symbols.of(`${r.file}:${r.line}`);
    if (address !== null) showAddress(address);
  }

  async function start() {
    const r = rate;
    if (r === null || !(await allowed(app))) return;
    const done = await app.act("profile.start", { method: method as "itm" | "pcsr", rate_hz: r,
                                                  ...(method === "itm" ? { core_clock_hz: (clockMhz ?? 0) * 1e6 } : {}) });
    if (done !== undefined) await poller.refresh();
  }

  /** 止める: 読み続けをやめてからすぐ頼む（待つのは読みかけの 1 回だけ） */
  async function stop() {
    if (!(await allowed(app))) return;
    stopping = true;
    try {
      const done = await app.act("profile.stop", {});
      if (done !== undefined) await poller.refresh();
    } finally {
      stopping = false;
    }
  }

  const rows = $derived<ProfileRow[]>(result?.rows ?? []);
  const rowsCut = $derived(topCut(rows.length, result?.rows_total, TOP));
  const maxFraction = $derived(Math.max(1e-9, ...rows.map((r) => r.fraction)));
  const cat = (k: string) => percent(summary?.categories?.[k]?.fraction);
  const rangeReason = $derived("reason" in range ? (range.reason === "itm" ? $i18n("profile.itmNoRange")
                                                                            : $i18n("profile.needCursors")) : "");
</script>

<div class="pane profile">
  <SamplerNotice mine="profile" />
  <div class="pane-bar">
    {#if running}
      <span class="live">● {$i18n("cpuLoad.live")}</span>
      <button disabled={!connected || stopping} onclick={stop}>
        {stopping ? $i18n("profile.stopping") : $i18n("profile.stop")}</button>
    {:else}
      <label title={$i18n("profile.methodTip")}>{$i18n("profile.method")}
        <select bind:value={method}>
          <option value="itm">{$i18n("profile.itm")}</option>
          <option value="pcsr">{$i18n("profile.pcsr")}</option>
        </select></label>
      <label>{$i18n("profile.rate")}<span class="req" aria-hidden="true">*</span>
        <select bind:value={rate} aria-required="true">
          {#if rate === null}<option value={null} disabled></option>{/if}
          {#each [1000, 2000, 5000, 10000, 20000, 50000] as r (r)}<option value={r}>{r / 1000} kHz</option>{/each}
        </select></label>
      {#if method === "itm"}
        <label title={clockAuto === "SystemCoreClock" ? $i18n("profile.clockFromElf") : ""}>
          {$i18n("profile.coreClock")}<span class="req" aria-hidden="true">*</span><input class="num" type="number" min="1"
            aria-required="true" value={clockMhz ?? ""} onchange={(e) => setClock(Number(e.currentTarget.value))} /></label>
      {/if}
      <button class="primary" disabled={!connected || !$poller.loaded || rate === null || (method === "itm" && !clockMhz)}
              title={connected ? $i18n("profile.startTip") : $i18n("hint.connectFirst")}
              onclick={start}>{$i18n("profile.start")}</button>
    {/if}
    {#if summary}
      <span class="muted">{$i18n("profile.total", { n: summary.total })} ·
        {$i18n("profile.categories", { sleep: cat("sleep"), wfi: cat("wfi"), unknown: cat("unknown") })}</span>
    {/if}
    <span class="spacer"></span>
    <label title={"t0" in range ? $i18n("profile.selectedTip") : rangeReason}>{$i18n("profile.range")}
      <select bind:value={scope}>
        <option value="whole">{$i18n("profile.whole")}</option>
        <option value="selected" disabled={!("t0" in range)}>{$i18n("profile.selected")}</option>
      </select></label>
    <label title={$i18n("profile.heatTip")}><input type="checkbox" bind:checked={heat} /> {$i18n("profile.heat")}</label>
  </div>

  <LoadState state={$poller} onretry={() => void poller.refresh()} />
  {#if summary}
    {#if scope === "selected"}
      <div class="banner muted">{span ? $i18n("profile.window", { n: result?.window?.samples ?? 0 }) : rangeReason}</div>
    {/if}
    <table class="grid">
      <thead><tr><th>{$i18n("profile.function")}</th><th title={$i18n("profile.loadTip")}>{$i18n("profile.load")}</th>
        <th>{$i18n("profile.samples")}</th>
        <th title={$i18n("profile.coverageTip")}>{$i18n("profile.coverage")}</th></tr></thead>
      <tbody>
        {#each rows as r (r.function)}
          {@const cov = coverage.get(r.function)}
          <tr class:open={open === r.function}>
            <td class="mono fn">
              <button class="caret" title={$i18n("profile.expandTip")} aria-expanded={open === r.function}
                      onclick={() => toggle(r.function)}>{open === r.function ? "▾" : "▸"}</button>
              <button class="link" onclick={() => (r.address !== undefined ? showAddress(r.address)
                                                    : symbols.of(r.function).then((a) => a !== null && showAddress(a)))}>
                {r.function}</button>
            </td>
            <td class="mono load"><span class="bar"><span style:width="{(100 * r.fraction) / maxFraction}%"></span></span>
              {percent(r.fraction)}</td>
            <td class="mono">{r.samples}</td>
            <td class="mono">{cov ? `${percent(cov.line_fraction)} (${cov.lines_hit}/${cov.lines})` : "—"}</td>
          </tr>
          {#if open === r.function}
            {#each lines as l (`${l.file}:${l.line}`)}
              <tr class="line">
                <td class="mono"><button class="link" title={$i18n("profile.lineTip")} onclick={() => showLine(l)}>
                  {l.file}:{l.line}</button></td>
                <td class="mono load"><span class="bar"><span style:width="{(100 * l.fraction) / maxFraction}%"></span></span>
                  {percent(l.fraction)}</td>
                <td class="mono">{l.samples}</td>
                <td></td>
              </tr>
            {/each}
            {#if linesCut}
              <tr class="line"><td class="muted" colspan="4">{$i18n("common.truncated", linesCut)}</td></tr>
            {/if}
            {#each cov?.missed_lines ?? [] as m (m.join(":"))}
              <tr class="line missed">
                <td class="mono">{m[0]}:{m[1]}</td><td class="muted" colspan="3">{$i18n("profile.missedLine")}</td>
              </tr>
            {/each}
          {/if}
        {/each}
      </tbody>
    </table>
    {#if rowsCut}<div class="banner muted">{$i18n("common.truncated", rowsCut)}</div>{/if}
    {#if heat && heatCut}<div class="banner muted">{$i18n("profile.heatTruncated", heatCut)}</div>{/if}
  {:else if $poller.loaded}
    <div class="empty">
      <strong>{$i18n("profile.none")}</strong>
      <span>{$i18n("profile.noneHint")}</span>
      {#if !connected}<span>{$i18n("hint.connectFirst")}</span>{/if}
    </div>
  {/if}
</div>

<style>
  .profile label { display: inline-flex; gap: 4px; align-items: center; font-size: 12px; color: var(--muted); }
  .profile select { font-size: 12px; padding: 1px 4px; }
  input.num { width: 64px; font-size: 12px; padding: 1px 4px; }
  .live { color: var(--bad); font-size: 12px; }
  .banner { padding: 4px 8px; font-size: 12px; border-bottom: 1px solid var(--line); }
  .fn { white-space: nowrap; }
  .caret { border: none; background: none; padding: 0 4px 0 0; color: var(--muted); }
  tr.open td { background: var(--select); }
  .line td:first-child { padding-left: 28px; }
  .missed td { color: var(--muted); }
  .load { white-space: nowrap; }
  .bar { display: inline-block; width: 80px; height: 6px; background: var(--line); border-radius: 3px; margin-right: 6px;
         vertical-align: middle; overflow: hidden; }
  .bar span { display: block; height: 100%; background: var(--warn); }
</style>
