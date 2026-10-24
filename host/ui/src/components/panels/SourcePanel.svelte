<script lang="ts">
  // Source: 行の左の余白にブレークポイント、いまの行（▶）の強調、選んだ行（Run to Cursor・F9 の対象）、
  // 逆アセンブルの混在表示（Mixed）。何も開いていなければ Open ELF / Load Demo / 最近使った項目。
  import { tick } from "svelte";
  import { command, type CommandState } from "../../lib/commands";
  import { shared } from "../../lib/context";
  import { lineMenu, openMenu } from "../../lib/contextmenu";
  import { frameLine, markedLines, type Instruction } from "../../lib/debugger";
  import { allowed } from "../../lib/guard";
  import { i18n } from "../../lib/i18n";
  import { load, save } from "../../lib/prefs";
  import { baseName, sourceHeat } from "../../lib/profile";
  import { confirmer } from "../../lib/confirm";
  import { lineSpec, measureParams } from "../../lib/cycles";
  import { haltedAt, lineMarks, missingList, missingText, sampleText, signedDuration,
           type CodeAtSample } from "../../lib/codeat";
  import { formatDuration, timeText } from "../../lib/time";
  import CodeAtToggle from "./CodeAtToggle.svelte";

  const ctx = shared();
  const { app, dbg, ui, safety, tl, codeAt } = ctx;
  const origins = tl.origins;

  // 時刻に連動（時間軸の選択カーソルの記録の時刻 → recording.code_at の直前・直後の PC のサンプルの行と止まった場所）
  let linked = $state(true);
  const atTime = $derived(linked && $tl.source === "recording" ? $codeAt : null);
  const at = $derived(atTime?.result ?? null);
  const atMarks = $derived(lineMarks(at, $dbg.open));
  const atHalt = $derived(haltedAt(at));
  const atText = (t: number) => timeText($origins, { source: "recording", t }) ?? `t = ${t}`;
  const markText: Record<string, string> = { before: "‹", after: "›", halt: "■" };
  let atShown: number | null = null;

  // 応答が替わったら、止まった場所の行を開いて見せる（止まっていなければ開かない。直前・直後のサンプルの
  // どちらを見るかは使う人が下の欄で選ぶ）
  $effect(() => {
    const go = atHalt;
    if (!at || !go?.file || go.line == null || at.t === atShown) return;
    atShown = at.t;
    void showPlace(go);
  });

  async function showPlace(p: { file?: string | null; line?: number | null }) {
    if (!p.file || p.line == null) return;
    if (dbg.snapshot.files.some((f) => f.name === p.file)) await dbg.openFile(p.file);
    await tick();
    code?.querySelector(`[data-line="${p.line}"]`)?.scrollIntoView({ block: "center" });
  }
  const placeTip = (s: CodeAtSample) => `${s.where ?? ""} · ${$i18n("codeAt.pc", { pc: `0x${(s.value >>> 0).toString(16)}` })}`;

  let code = $state<HTMLElement>();
  let mixed = $state(load("source.mixed", "0") === "1");
  // 2 点の間のサイクル数（cycles.measure）: 選んだ行を始点・終点に（関数名・0x の番地も書ける）
  let cyclesOpen = $state(false);
  let cycStart = $state(load("cycles.start", ""));
  let cycEnd = $state(load("cycles.end", ""));
  let cycCount = $state(load("cycles.count", ""));
  let cycResult = $state<any>(null);
  let cycError = $state<string | null>(null);
  $effect(() => save("source.mixed", mixed ? "1" : "0"));

  const online = $derived($app.link === "open");
  const lines = $derived($dbg.source ? $dbg.source.text.replace(/\n$/, "").split("\n") : []);
  const breakable = $derived(new Set($dbg.source?.lines ?? []));
  const marked = $derived(markedLines($dbg));
  const disabledMarks = $derived(markedLines($dbg, (b) => b.enabled === false));
  const stopLine = $derived($dbg.stop?.file === $dbg.open ? $dbg.stop?.line : undefined);
  // 呼び出し履歴で選んだ呼び出し元のフレームの行（VS Code の「フォーカスのあるフレーム」）
  const framePlace = $derived($dbg.frame > 0 && $dbg.stop ? frameLine($dbg.backtrace[$dbg.frame]) : null);
  const frameLineNo = $derived(framePlace?.file === $dbg.open ? framePlace?.line : undefined);
  const framePc = $derived($dbg.frame > 0 ? $dbg.backtrace[$dbg.frame]?.pc : undefined);
  const selected = $derived($ui.selection?.file === $dbg.open ? $ui.selection?.line : undefined);
  // Code Profile の当たりの割合（行番号の色の濃さ）と当たらなかった行（灰色）。Code Profile が置く
  const heat = $derived($sourceHeat.lines.get(baseName($dbg.open)));
  const missed = $derived($sourceHeat.missed.get(baseName($dbg.open)));
  // 混在表示: 止まった PC の前後の命令を、行ごとにまとめる
  const byLine = $derived.by(() => {
    const out = new Map<number, Instruction[]>();
    if (!mixed) return out;
    for (const ins of $dbg.disasm) {
      if (ins.file !== $dbg.open || ins.line == null) continue;
      out.set(ins.line, [...(out.get(ins.line) ?? []), ins]);
    }
    return out;
  });
  const cs = $derived<CommandState>({ link: $app.link, target: $app.target?.state, elf: $dbg.elf,
                                         selection: $ui.selection });

  // 止まった行（呼び出し元のフレームを選んでいればその行）へ移す
  $effect(() => {
    const line = frameLineNo ?? stopLine;
    if (line === undefined || !code) return;
    const box = code;
    tick().then(() => box.querySelector(`[data-line="${line}"]`)?.scrollIntoView({ block: "center" }));
  });

  // ほかのビュー・時間軸から頼まれた行（ui.revealLine）へ移す
  $effect(() => {
    if (!$ui.reveals || !code || selected === undefined || !$dbg.source) return;
    const box = code, line = selected;
    tick().then(() => box.querySelector(`[data-line="${line}"]`)?.scrollIntoView({ block: "center" }));
  });

  function run(id: string) {
    const c = command(id);
    if (!c.blocked(cs)) void c.run(ctx, cs);
  }

  /** 行の右クリック（lib/contextmenu.ts の lineMenu）: その行を選び、ブレークポイント・ここまで実行のコマンドを
   *  その行で（使えない理由もコマンドと同じ） */
  function lineContext(e: MouseEvent, n: number) {
    if (!$dbg.open) return;
    ui.select($dbg.open, n);
    const at = { ...cs, selection: { file: $dbg.open, line: n } };
    const toggle = command("toggleBreakpoint"), runTo = command("runToCursor");
    const bp = dbg.breakpointAt(n);
    openMenu(e, lineMenu(bp, toggle.blocked(at) ?? (bp || breakable.has(n) ? null : "menu.bp.noCode"), runTo.blocked(at), {
      toggle: () => toggle.run(ctx, at),
      enable: async (enabled) => (await allowed(app)) && bp && dbg.enableBreakpoint(bp.location, enabled),
      runTo: () => runTo.run(ctx, at),
    }));
  }

  async function openRecent(path: string) {
    if ((await allowed(app)) && (await dbg.loadElf(path))) ui.remember(path);
  }

  const hex = (n: number) => n.toString(16).padStart(8, "0");

  function pickLine(which: "start" | "end") {
    if (selected === undefined || !$dbg.source) return;
    const spec = lineSpec({ file: $dbg.source.name, line: selected });
    if (which === "start") cycStart = spec;
    else cycEnd = spec;
  }

  /** ターゲットを走らせ、始点から終点までを count 回測る（終わると終点で止まっている）ので確かめる */
  async function measureCycles() {
    const built = measureParams(cycStart, cycEnd, cycCount);
    if ("error" in built) {
      cycError = $i18n("cycles.badField", { field: built.error });
      return;
    }
    if (!(await allowed(app))) return;
    if (!(await confirmer.ask($i18n("cycles.confirm", built.params as never), { ok: $i18n("cycles.measure") }))) return;
    save("cycles.start", cycStart);
    save("cycles.end", cycEnd);
    save("cycles.count", cycCount);
    cycError = null;
    const out = await app.act("cycles.measure", built.params);
    cycResult = out ?? null;
  }
</script>

<div class="pane source">
  {#if $tl.source === "recording"}
    <!-- 時刻に連動: 時間軸で選んだ記録の時刻の、直前・直後の PC のサンプルの行と止まった場所（recording.code_at） -->
    <div class="pane-bar codeat" title={at ? `elf: ${at.elf ?? "—"} · recorded_elf: ${at.recorded_elf ?? "—"}` : undefined}>
      <CodeAtToggle panel="source" bind:linked />
      {#if atTime && atTime.t !== null}
        <span class="muted">{$i18n("codeAt.at", { time: atText(atTime.t) })}</span>
        {#if atTime.loading}<span class="muted">{$i18n("codeAt.loading")}</span>{/if}
        {#if atTime.error}<span class="bad">{atTime.error}</span>{/if}
        {#if at}
          {#each [["before", at.pc_before], ["after", at.pc_after]] as const as [kind, s] (kind)}
            <span class="at-item {kind}" title={$i18n(kind === "before" ? "codeAt.beforeTip" : "codeAt.afterTip")}>
              <span class="at-mark">{markText[kind]}</span>
              <span class="muted">{$i18n(kind === "before" ? "codeAt.before" : "codeAt.after")}</span>
              {#if s?.file}
                <button class="link mono" title={placeTip(s)} onclick={() => void showPlace(s)}>{sampleText($i18n, s)}</button>
                <span class="muted mono">{signedDuration(s.dt_s, (x) => formatDuration(x))}</span>
              {:else if s}
                <span class="mono">{sampleText($i18n, s)}</span>
                <span class="muted mono">{signedDuration(s.dt_s, (x) => formatDuration(x))}</span>
              {:else}
                <span class="muted">{$i18n("codeAt.none")}</span>
              {/if}
            </span>
          {/each}
          {#if at.pc_gap_s !== null}
            <span class="muted mono" title={$i18n("codeAt.gapTip")}>{$i18n("codeAt.gap", { gap: formatDuration(at.pc_gap_s) })}</span>
          {/if}
          {#if atHalt}
            <span class="at-item halt" title={$i18n("codeAt.haltedTip")}>
              <span class="at-mark">{markText.halt}</span>
              <span>{$i18n("codeAt.halted")}</span>
              <button class="link mono" disabled={!atHalt.file} onclick={() => void showPlace(atHalt)}>
                {atHalt.where ?? (atHalt.pc !== undefined ? $i18n("codeAt.pc", { pc: `0x${(atHalt.pc >>> 0).toString(16)}` }) : "")}</button>
              {#if atHalt.halt_reason}<span class="muted">{atHalt.halt_reason}</span>{/if}
              <span class="muted mono">{signedDuration(atHalt.dt_s, (x) => formatDuration(x))}</span>
            </span>
          {/if}
          {#each missingList(at) as m (m.key)}<span class="muted note">{missingText($i18n, m.key, m.reason)}</span>{/each}
        {/if}
      {/if}
    </div>
  {/if}
  {#if $dbg.files.length}
    <div class="pane-bar">
      <select aria-label={$i18n("panel.source")} value={$dbg.open ?? ""}
              onchange={(e) => dbg.openFile((e.currentTarget as HTMLSelectElement).value)}>
        {#if !$dbg.open}<option value="" disabled>{$i18n("source.pickFile")}</option>{/if}
        {#each $dbg.files as f (f.name)}<option value={f.name} title={f.path}>{f.name}</option>{/each}
      </select>
      <span class="muted mono path" title={$dbg.source?.path ?? ""}>{$dbg.source?.path ?? ""}</span>
      <span class="spacer"></span>
      <label title={$i18n("source.mixedTip")}><input type="checkbox" bind:checked={mixed} /> {$i18n("source.mixed")}</label>
      <button class="small" class:on={cyclesOpen} title={$i18n("cycles.tip")} onclick={() => (cyclesOpen = !cyclesOpen)}>
        {$i18n("cycles.title")}</button>
    </div>
    {#if cyclesOpen}
      <form class="pane-bar cycles" onsubmit={(e) => { e.preventDefault(); void measureCycles(); }}>
        <input class="mono" bind:value={cycStart} placeholder={$i18n("cycles.start")} aria-label={$i18n("cycles.start")} />
        <button type="button" class="small" disabled={selected === undefined} title={$i18n("cycles.useLineTip")}
                onclick={() => pickLine("start")}>{$i18n("cycles.useLine")}</button>
        <span class="muted">→</span>
        <input class="mono" bind:value={cycEnd} placeholder={$i18n("cycles.end")} aria-label={$i18n("cycles.end")} />
        <button type="button" class="small" disabled={selected === undefined} title={$i18n("cycles.useLineTip")}
                onclick={() => pickLine("end")}>{$i18n("cycles.useLine")}</button>
        <label class="muted">{$i18n("cycles.count")}<span class="req" aria-hidden="true">*</span>
          <input class="mono num" bind:value={cycCount} inputmode="numeric" aria-required="true" /></label>
        <button class="small" disabled={!online}>{$i18n("cycles.measure")}</button>
        {#if cycError}<span class="bad">{cycError}</span>{/if}
        {#if cycResult?.summary}
          {@const sm = cycResult.summary}
          <span class="mono">{$i18n("cycles.result", { count: sm.count, min: sm.min, max: sm.max, median: sm.median })}</span>
          {#if sm.time_us}<span class="muted mono">{$i18n("cycles.resultUs", { min: sm.time_us.min.toFixed(3), max: sm.time_us.max.toFixed(3),
            median: sm.time_us.median.toFixed(3) })}</span>{/if}
          {#each cycResult.notes ?? [] as n, k (k)}<span class="muted note" title={n}>{n}</span>{/each}
        {/if}
      </form>
    {/if}
    {#if $dbg.source}
      <div class="lines mono" bind:this={code}>
        {#each lines as text, i (i)}
          {@const n = i + 1}
          {@const atLine = atMarks.get(n)}
          <!-- svelte-ignore a11y_no_static_element_interactions（右クリックのメニュー。キーでは F9・Ctrl+F10 が同じ操作） -->
          <div class="line" class:stop={n === stopLine} class:frame={n === frameLineNo} class:selected={n === selected}
               class:at-before={atLine?.includes("before")} class:at-after={atLine?.includes("after")}
               class:at-halt={atLine?.includes("halt")} data-line={n} oncontextmenu={(e) => lineContext(e, n)}>
            <button class="gutter" disabled={!breakable.has(n) || !online}
                    title={$i18n("source.breakpointTip", { line: n })} aria-label={$i18n("source.breakpointTip", { line: n })}
                    onclick={async () => (await allowed(app))
                                         && (marked.has(n) || await safety.confirmHalt("breakpoint", $i18n))
                                         && dbg.toggleBreakpoint(n)}>
              {#if marked.has(n)}<span class="dot" class:off={disabledMarks.has(n)}></span>{:else if breakable.has(n)}<span class="can"></span>{/if}
            </button>
            <span class="arrow" class:caller={n === frameLineNo && n !== stopLine} class:at={!!atLine && n !== stopLine && n !== frameLineNo}>
              {n === stopLine ? "▶" : n === frameLineNo ? "▷" : atLine ? atLine.map((k) => markText[k]).join("") : ""}</span>
            <!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions -->
            <span class="no" class:missed={missed?.has(n)} onclick={() => ui.select($dbg.open!, n)}
                  style:background={heat?.has(n) ? `color-mix(in srgb, var(--warn) ${Math.round(15 + 60 * heat.get(n)!.samples / heat.get(n)!.max)}%, transparent)` : null}
                  title={heat?.has(n) ? $i18n("profile.heatLine", { p: `${(heat.get(n)!.fraction * 100).toFixed(1)}%`, n: heat.get(n)!.samples })
                         : missed?.has(n) ? $i18n("profile.missedLine") : null}>{n}</span>
            <!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions -->
            <span class="text" onclick={() => ui.select($dbg.open!, n)}>{text || " "}</span>
          </div>
          {#each byLine.get(n) ?? [] as ins (ins.address)}
            <div class="asm" class:pc={ins.address === $dbg.stop?.pc} class:frame={ins.address === framePc}>
              <span></span><span class="arrow">{ins.address === $dbg.stop?.pc ? "▶" : ""}</span>
              <span class="no">{hex(ins.address)}</span>
              <span class="text">{ins.mnemonic} {ins.operands}</span>
            </div>
          {/each}
        {/each}
      </div>
    {:else if $dbg.open}
      <p class="empty">{$i18n("source.missing", { path: $dbg.files.find((f) => f.name === $dbg.open)?.path ?? $dbg.open })}</p>
    {:else}
      <p class="empty">{$i18n("source.pickFile")}</p>
    {/if}
  {:else}
    <div class="empty start">
      <strong class="title">{$i18n("source.noProgram")}</strong>
      <span>{$i18n("source.noProgramHint")}</span>
      <div class="actions">
        <button class="primary" disabled={!online} onclick={() => run("openElf")}>{$i18n("cmd.openElf")}</button>
        <button disabled={!online} title={$i18n("tip.loadDemo")} onclick={() => run("loadDemo")}>{$i18n("cmd.loadDemo")}</button>
      </div>
      {#if $ui.recent.length}
        <span class="recent-title">{$i18n("source.recent")}</span>
        <ul class="recent">
          {#each $ui.recent as path (path)}
            <li><button class="link mono" disabled={!online} onclick={() => openRecent(path)}>{path}</button></li>
          {/each}
        </ul>
      {/if}
    </div>
  {/if}
</div>

<style>
  .cycles input { width: 11em; }
  .cycles input.num { width: 4em; }
  .cycles .bad { color: var(--bad); }
  .cycles .note { font-size: 11.5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 30em; }
  button.on { outline: 1px solid var(--accent); }
  .source { display: flex; flex-direction: column; }
  .path { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 50%; font-size: 12px; }
  .lines { flex: 1; overflow: auto; padding: 4px 0; }
  .line, .asm { display: grid; grid-template-columns: 18px 14px 44px 1fr; align-items: center; min-height: 19px; }
  .line.selected { background: var(--select); }
  .line.stop { background: color-mix(in srgb, var(--warn) 22%, transparent); }
  .line.frame { background: color-mix(in srgb, var(--ok) 18%, transparent); }
  /* 記録の時刻の行（止まった行 = 黄色とは別の色）: 直前・直後のサンプルは青の左の線、止まった場所は青の枠 */
  .line.at-before, .line.at-after { box-shadow: inset 3px 0 0 var(--accent);
                                    background: color-mix(in srgb, var(--accent) 12%, transparent); }
  .line.at-after:not(.at-before) { box-shadow: inset 3px 0 0 color-mix(in srgb, var(--accent) 55%, transparent); }
  .line.at-halt { outline: 1px solid var(--accent); outline-offset: -1px; }
  .arrow.at { color: var(--accent); }
  .codeat { flex-wrap: wrap; row-gap: 2px; font-size: 12px; }
  .codeat .at-item { display: inline-flex; gap: 4px; align-items: baseline; }
  .codeat .at-mark { color: var(--accent); }
  .codeat .bad { color: var(--bad); }
  .codeat .note { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 28em; }
  .asm.frame { color: var(--text); background: color-mix(in srgb, var(--ok) 10%, transparent); }
  .arrow.caller { color: var(--ok); }
  /* 命令の番地は 8 桁の 16 進で行番号（44px）より広い: 番地の列だけ広げる（重なって読めなかった） */
  .asm { color: var(--muted); font-size: 12px; grid-template-columns: 18px 14px 76px 1fr; }
  .asm.pc { color: var(--text); background: color-mix(in srgb, var(--warn) 12%, transparent); }
  .asm .no { font-size: 11px; }
  .gutter { border: none; background: none; padding: 0; height: 19px; display: grid; place-items: center; cursor: pointer; }
  .gutter:disabled { cursor: default; }
  .dot { width: 10px; height: 10px; border-radius: 50%; background: var(--bad); }
  .dot.off { background: none; box-shadow: inset 0 0 0 2px var(--bad); }         /* 無効（中抜き） */
  .can { width: 8px; height: 8px; border-radius: 50%; }
  .gutter:not(:disabled):hover .can { background: color-mix(in srgb, var(--bad) 45%, transparent); }
  .arrow { color: var(--warn); font-size: 11px; text-align: center; }
  .no.missed { text-decoration: line-through; opacity: 0.6; }
  .no { color: var(--muted); text-align: right; padding-right: 10px; user-select: none; cursor: pointer; }
  .text { white-space: pre; cursor: text; }
  .start { max-width: 640px; }
  .title { font-size: 16px; }
  .recent-title { margin-top: 8px; color: var(--text); }
  .recent { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 4px; }
</style>
