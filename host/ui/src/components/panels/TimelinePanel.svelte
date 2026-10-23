<script lang="ts">
  import type { LaChannel } from "../../lib/waveform";
  // Timeline: 共通タイムライン（ロジアナの波形・記録の段。WebGL2 の部品 Timeline.svelte）と捕捉の設定、縮図、
  // 段の名前の列（折りたたみ・並べ替え・隠す）、段の一覧のメニュー、カーソル（選択・基準・差）の表示、時刻へ移動
  import { frequency } from "../../lib/format";
  import { allowed } from "../../lib/guard";
  import { shared } from "../../lib/context";
  import { i18n } from "../../lib/i18n";
  import { load, save } from "../../lib/prefs";
  import { formatDuration, formatSeconds, parseSeconds } from "../../lib/time";
  import { channelChoices, channelName, rateChoices } from "../../lib/waveform";
  import { isShown } from "../../lib/timeline/rows";
  import { originOf, type Source } from "../../lib/timeline/state";
  import { groupTitle, rowTitle } from "../../lib/timeline/titles";
  import Timeline from "../Timeline.svelte";
  import TimelineMinimap from "../TimelineMinimap.svelte";
  import TimelineRows from "../TimelineRows.svelte";

  const { app, tl, screen } = shared();

  let rate = $state(load("la.rate", "25000000"));
  let pretrigger = $state(load("la.pretrigger", "1024"));
  let trigCh = $state(load("la.trigCh", "none"));
  let edge = $state(load("la.edge", "fall"));
  let deep = $state(false);
  let afterBeats = $state(load("la.afterBeats", "65536"));
  let busy = $state(false);
  let cursor = $state<number | null>(null);
  let renderer = $state<"webgl2" | "canvas2d">("webgl2");

  const t = $derived($tl);
  // マウスのカーソルも画面の状態に（ほかの窓・AI が人の指す時刻を読める）
  $effect(() => screen.setMouse(cursor));
  const caps = $derived($app.probe?.capabilities ?? {});
  const rates = $derived(rateChoices(caps.la_sample_hz ?? 100e6));
  // 覚えていた速度がこのボードの選択肢に無ければ（最高速度が違うボード）、選択肢から選び直す
  $effect(() => {
    if (rates.length && !rates.includes(Number(rate))) rate = String(rates[Math.min(2, rates.length - 1)]);
  });
  const origin = $derived(originOf(t));
  // 時刻の桁: 1 画素の時間（と 1 単位）を区別できるだけ（t.width はデバイスの画素）
  const resolution = $derived(Math.max(t.tickS, ((t.view.end - t.view.start) * t.tickS) / Math.max(1, t.width)));
  const at = (time: number) => formatSeconds((time - origin) * t.tickS, resolution);

  // 時刻へ移動（始めからの時刻を打つ）。読めなければ欄に印
  let goText = $state("");
  let goBad = $state(false);
  function goTo(e: Event) {
    e.preventDefault();
    const s = parseSeconds(goText);
    goBad = s === null;
    if (s !== null) tl.goTo(s);
  }

  async function capture() {
    for (const [k, v] of [["la.rate", rate], ["la.pretrigger", pretrigger], ["la.trigCh", trigCh], ["la.edge", edge],
                          ["la.afterBeats", afterBeats]]) save(k, v);
    if (!(await allowed(app))) return;
    const bit = trigCh === "none" ? 0 : 1 << Number(trigCh);
    busy = true;
    await tl.capture({ rate_hz: Number(rate), pretrigger: Number(pretrigger), deep, afterBeats: Number(afterBeats),
                       rise: edge === "rise" ? bit : 0, fall: edge === "fall" ? bit : 0 });
    busy = false;
  }
  const laLayout = $derived((($app.probe?.capabilities ?? {}) as { la_layout?: LaChannel[] }).la_layout ?? null);
</script>

<div class="pane timeline">
  <div class="pane-bar">
    <label>{$i18n("timeline.source")}
      <select value={t.source} onchange={(e) => tl.setSource((e.currentTarget as HTMLSelectElement).value as Source)}>
        <option value="capture">{$i18n("timeline.lastCapture")}</option>
        <option value="recording">{$i18n("timeline.recording")}</option>
      </select>
    </label>
    {#if t.source === "capture"}
      <label>{$i18n("timeline.rate")} <select bind:value={rate}>{#each rates as r (r)}<option value={String(r)}>{frequency(r)}</option>{/each}</select></label>
      <label>{$i18n("timeline.pretrigger")} <input class="num" bind:value={pretrigger} inputmode="numeric" disabled={deep} /></label>
      <label>{$i18n("timeline.trigger")}
        <select bind:value={trigCh}>
          <option value="none">{$i18n("timeline.triggerNone")}</option>
          {#each channelChoices(caps) as ch (ch)}<option value={String(ch)}>{channelName(ch, laLayout)}</option>{/each}
        </select>
        <select bind:value={edge} disabled={trigCh === "none"} aria-label={$i18n("timeline.trigger")}>
          <option value="fall">{$i18n("timeline.falling")}</option><option value="rise">{$i18n("timeline.rising")}</option>
        </select>
      </label>
      {#if caps.mem_bytes}
        <label title={$i18n("timeline.deepTip")}><input type="checkbox" bind:checked={deep} /> {$i18n("timeline.deep")}</label>
        {#if deep}
          <label title={$i18n("timeline.afterTip")}>{$i18n("timeline.after")}
            <select bind:value={afterBeats}>
              {#each ["4096", "65536", "1048576"] as b (b)}<option value={b}>{$i18n("timeline.beats", { n: Number(b).toLocaleString() })}</option>{/each}
            </select>
          </label>
        {/if}
      {/if}
      <button class="primary" disabled={$app.link !== "open" || busy} title={$i18n("tip.capture")} onclick={capture}>
        {busy ? $i18n("timeline.capturing") : $i18n("timeline.capture")}</button>
    {:else}
      <button disabled={$app.link !== "open"} onclick={() => tl.reload()}>{$i18n("timeline.reload")}</button>
    {/if}
    <span class="spacer"></span>
    <span class="info muted">
      {#if t.source === "capture" && t.capture}
        {$i18n("timeline.captureInfo", { id: t.capture.id, who: t.capturedBy ? `${t.capturedBy}, ` : "", source: t.capture.source,
                                       length: t.capture.length.toLocaleString(), period: formatDuration(t.tickS) })}
      {:else if t.source === "recording" && t.recording}
        {$i18n("timeline.recordingInfo", { name: t.recording })}
      {:else if t.error}
        <span class="bad">{t.error}</span>
      {/if}
      {#if t.extent}
        · {$i18n("timeline.span", { span: formatDuration((t.view.end - t.view.start) * t.tickS) })}
        {#if t.selection !== null}· {$i18n("timeline.selection")} <strong>{at(t.selection)}</strong>{/if}
        {#if t.reference !== null}· {$i18n("timeline.reference")} <strong class="ref">{at(t.reference)}</strong>
          {#if t.selection !== null}· <strong>{$i18n("timeline.delta", { d: formatDuration((t.selection - t.reference) * t.tickS) })}</strong>{/if}
        {/if}
        {#if cursor !== null}· {$i18n("timeline.cursor")} {at(cursor)}{/if}
        {#if t.header?.wave.mode === "summary"}· {$i18n("timeline.summary")}{/if}
        {#if t.loading}· {$i18n("timeline.loading")}{/if}
        {#if renderer === "canvas2d"}· <span class="warn">{$i18n("timeline.slow")}</span>{/if}
      {/if}
    </span>
    <form class="goto" onsubmit={goTo}>
      <input class="num" class:bad-input={goBad} bind:value={goText} oninput={() => (goBad = false)} disabled={!t.extent}
             placeholder={$i18n("timeline.goToPlaceholder")} aria-label={$i18n("timeline.goTo")}
             title={goBad ? $i18n("timeline.goToBad") : $i18n("timeline.goToTip")} />
      <button class="small" disabled={!t.extent || !goText.trim()} title={$i18n("timeline.goToTip")}>{$i18n("timeline.goTo")}</button>
    </form>
    <button class="small" disabled={!t.extent} title="{$i18n('timeline.prevEvent')} (P)" aria-label={$i18n("timeline.prevEvent")}
            onclick={() => tl.seek(-1)}>◀</button>
    <button class="small" disabled={!t.extent} title="{$i18n('timeline.nextEvent')} (N)" aria-label={$i18n("timeline.nextEvent")}
            onclick={() => tl.seek(1)}>▶</button>
    <button class="small" disabled={t.selection === null && t.reference === null}
            title="{t.reference === null ? $i18n('timeline.setReference') : $i18n('timeline.clearReference')} (R)"
            onclick={() => tl.setReference(t.reference === null ? t.selection : null)}>
      {t.reference === null ? $i18n("timeline.setReference") : $i18n("timeline.clearReference")}</button>
    <details class="rowsmenu">
      <summary class="small" title={$i18n("timeline.rowsTip")}>{$i18n("timeline.rows")}</summary>
      <div class="menu">
        <div class="menu-actions">
          <button class="small" onclick={() => tl.setAllFolded(false)}>{$i18n("timeline.expandAll")}</button>
          <button class="small" onclick={() => tl.setAllFolded(true)}>{$i18n("timeline.collapseAll")}</button>
          <button class="small" onclick={() => tl.resetRows()}>{$i18n("timeline.resetRows")}</button>
        </div>
        {#each t.groups as g (g.id)}
          <div class="menu-group">{groupTitle(g.id, $i18n)}</div>
          {#each g.rows as r (r.id)}
            <label class="menu-row"><input type="checkbox" checked={isShown(r, t.config)}
                   onchange={(e) => tl.setRowShown(r.id, (e.currentTarget as HTMLInputElement).checked)} />
              {rowTitle(r, $i18n, laLayout)}</label>
          {/each}
        {/each}
      </div>
    </details>
    {#if t.live}
      {#if t.follow}
        <span class="live" title={$i18n("timeline.followingTip")}>● {$i18n("timeline.following")}</span>
      {:else}
        <button class="small" title={$i18n("timeline.followTip")} onclick={() => tl.toEdge(1)}>{$i18n("timeline.follow")}</button>
      {/if}
    {/if}
    <button class="small" disabled={!t.extent} onclick={() => tl.fit()}>{$i18n("timeline.zoomFit")}</button>
  </div>

  {#if !t.extent}
    <div class="empty">
      <strong>{t.source === "capture" ? $i18n("timeline.noCapture") : $i18n("timeline.noRecording")}</strong>
      <span>{t.source === "capture" ? $i18n("timeline.noCaptureHint") : $i18n("timeline.noRecordingHint")}</span>
    </div>
  {/if}
  {#if t.extent}<TimelineMinimap {tl} {t} />{/if}
  <!-- 時間軸は捕捉が無くても置く（幅が決まらないとタイルを読まない） -->
  <div class="plot" id="timeline">
    <TimelineRows {tl} {t} {cursor} />
    <Timeline {tl} {t} bind:cursor bind:renderer />
  </div>
</div>

<style>
  .live { color: var(--bad); font-size: 12px; white-space: nowrap; }
  .timeline { display: flex; flex-direction: column; }
  label { display: flex; gap: 4px; align-items: center; }
  .num { width: 70px; }
  .goto { display: flex; gap: 4px; align-items: center; }
  .bad-input { border-color: var(--bad); outline-color: var(--bad); }
  .info { font-size: 12px; }
  .bad { color: var(--bad); }
  .warn { color: var(--warn); }
  .plot { display: grid; grid-template-columns: 160px minmax(0, 1fr); padding: 0 0 4px; align-content: start;
          overflow-y: auto; flex: 1; min-height: 0; }
  .ref { color: var(--warn); }
  .rowsmenu { position: relative; }
  .rowsmenu summary { list-style: none; cursor: pointer; padding: 2px 8px; border: 1px solid var(--line); border-radius: 4px; }
  .menu { position: absolute; right: 0; top: 100%; z-index: 5; min-width: 240px; max-height: 60vh; overflow-y: auto;
          background: var(--panel); border: 1px solid var(--line); border-radius: 4px; padding: 6px;
          box-shadow: 0 4px 14px rgba(0, 0, 0, 0.18); font-size: 12px; }
  .menu-actions { display: flex; gap: 4px; flex-wrap: wrap; margin-bottom: 4px; }
  .menu-group { font-weight: 600; margin: 6px 0 2px; }
  .menu-row { display: flex; gap: 6px; padding: 1px 0 1px 8px; }
</style>
