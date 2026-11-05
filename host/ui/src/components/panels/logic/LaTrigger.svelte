<script lang="ts">
  // Logic Analyzer の Trigger: 多段トリガ（xtrig.set_sequence: 段 1〜4・ピンの条件 A / B）・番地一致（xtrig.set_match）・
  // つなぎ先（xtrig.route）と状態、段の図
  import { errorText } from "../../../lib/api";
  import { untrack } from "svelte";
  import { shared } from "../../../lib/context";
  import { allowed } from "../../../lib/guard";
  import { i18n } from "../../../lib/i18n";
  import {
    EVENTS, SOURCES, emptyPattern, lastApplied, patternEmpty, patternForm, patternOf, stageForms, stageLabel, stageState, stagesOf,
    type PatternForm, type SequenceStatus, type StageForm, type TriggerEvent, type TriggerSource,
  } from "../../../lib/logic";
  import { intervalNow } from "../../common/interval";
  import { load, save } from "../../../lib/prefs";
  import { laChannelLabel } from "../../../lib/lachannel";
  import type { LaChannel } from "../../../lib/waveform";
  import type { FieldError, FieldValues } from "../../../lib/apiform";
  import { formCall, methodFields } from "../../../lib/crosstrigger";
  import ApiFields from "../ApiFields.svelte";
  import Req from "../Req.svelte";
  import TimeLink from "../TimeLink.svelte";

  const STATUS_MS = 1000;

  let { active, linked, declaredChannels }: { active: boolean; linked: boolean; declaredChannels: number | null } = $props();

  const { app, conn, tl } = shared();
  const channels = $derived(declaredChannels ?? 0);

  function restore<T>(key: string, fallback: T): T {
    try {
      return JSON.parse(load(key, "null")) ?? fallback;
    } catch {
      return fallback;
    }
  }
  // 段の事象・回数は使う人が入れる（画面の既定を持たない）。覚えた回数は文字の欄に
  const emptyStage = (): StageForm => ({ when: [], count: "", withinUs: "" });
  let stages = $state<StageForm[]>(restore<StageForm[]>("logicAnalyzer.stages", [emptyStage()])
    .map((s) => ({ ...s, count: s.count === undefined || s.count === null ? "" : String(s.count) })));
  let patA = $state<PatternForm>(restore("logicAnalyzer.patternA", emptyPattern(0)));
  let patB = $state<PatternForm>(restore("logicAnalyzer.patternB", emptyPattern(0)));
  // 番地一致は Cross Trigger と同じく API の仕様の欄（必須は空から、省ける欄は空 = 送らない。既定はデーモンが決める）。
  // 前の版は画面の既定（全ビットのマスク・値 0 等）を入れて覚えていたので、覚える名前を変えて空から始める
  const matchFields = methodFields("xtrig.set_match");
  let match = $state<FieldValues>({ ...Object.fromEntries(matchFields.map((f) => [f.name, ""])),
                                    ...restore<FieldValues>("logicAnalyzer.matchFields", {}) });
  let matchErrors = $state<Record<string, FieldError>>({});
  let toLa = $state<TriggerSource[]>(restore("logicAnalyzer.toLa", []));
  let toInject = $state<TriggerSource[]>(restore("logicAnalyzer.toInject", []));
  // 経路の表は、書き換えていない間は機器のレジスタの事実（xtrig.status の route）に合わせる。ほかの道具
  // （fpga_obs.trigger_set・sampler.trigger_set・ルール）が書いたビットを、表の適用で黙って消さない
  let routeEdited = $state(false);
  // 多段トリガの状態: 仕掛けているか（armed）・段ごとの状態もレジスタの事実（xtrig.sequence_status）をそのまま描く
  let seqStatus = $state<SequenceStatus | null>(null);
  let xtStatus = $state<any>(null);
  let triggerError = $state<string | null>(null);

  // チャンネルの数に合わせる（覚えた条件がほかのボードのものでも）
  $effect(() => {
    const n = declaredChannels;
    if (n === null) return;
    untrack(() => {
      if (patA.level.length !== n) patA = patternForm(patternOf(patA), n);
      if (patB.level.length !== n) patB = patternForm(patternOf(patB), n);
    });
  });
  // 状態はトリガのタブを見ている間だけ読む
  $effect(() => {
    if (!active || !linked) return;
    return intervalNow(() => readTrigger(), STATUS_MS);
  });

  async function readTrigger() {
    try {
      [seqStatus, xtStatus] = await Promise.all([conn.call("xtrig.sequence_status", {}, { quiet: true }),
                                                 conn.call("xtrig.status", {}, { quiet: true })]);
      const route = (xtStatus as any)?.route;
      if (route && !routeEdited) {
        toLa = (route.to_la ?? []).filter((s: string) => (SOURCES as readonly string[]).includes(s));
        toInject = (route.to_inject ?? []).filter((s: string) => (SOURCES as readonly string[]).includes(s));
      }
    } catch {
      seqStatus = xtStatus = null;
    }
  }

  function keep() {
    save("logicAnalyzer.stages", JSON.stringify(stages));
    save("logicAnalyzer.patternA", JSON.stringify(patA));
    save("logicAnalyzer.patternB", JSON.stringify(patB));
    save("logicAnalyzer.matchFields", JSON.stringify(match));
    save("logicAnalyzer.toLa", JSON.stringify(toLa));
    save("logicAnalyzer.toInject", JSON.stringify(toInject));
  }

  async function applySequence() {
    keep();
    let params;
    try {
      const a = patternOf(patA), b = patternOf(patB);
      params = { stages: stagesOf(stages), ...(patternEmpty(a) ? {} : { pattern_a: a }),
                 ...(patternEmpty(b) ? {} : { pattern_b: b }), arm: true };
      triggerError = null;
    } catch (e) {
      triggerError = errorText(e);
      return;
    }
    if (!(await allowed(app))) return;
    if (await app.act("xtrig.set_sequence", params) !== undefined) await readTrigger();
  }

  async function applyMatch() {
    keep();
    const c = formCall("xtrig.set_match", match);
    if ("errors" in c) {
      matchErrors = c.errors;
      return;
    }
    matchErrors = {};
    triggerError = null;
    if (!(await allowed(app))) return;
    await app.act("xtrig.set_match", c.params as any);
    await readTrigger();
  }

  async function applyRoute() {
    keep();
    if (!(await allowed(app))) return;
    if ((await app.act("xtrig.route", { to_la: toLa, to_inject: toInject })) !== undefined) routeEdited = false;
  }

  async function fire() {
    if (await allowed(app)) await app.act("xtrig.fire");
  }

  /** AI・ほかの画面が最後に設定した多段トリガを欄に読み込む */
  function loadLastSequence() {
    const e = lastApplied($app.activity, "xtrig.set_sequence");
    const p = e?.params as any;
    if (!p?.stages) return;
    stages = stageForms(p.stages);
    patA = patternForm(p.pattern_a, channels);
    patB = patternForm(p.pattern_b, channels);
  }

  function loadLastRoute() {
    const p = lastApplied($app.activity, "xtrig.route")?.params as any;
    if (!p) return;
    toLa = (p.to_la ?? []).filter((s: string) => (SOURCES as readonly string[]).includes(s));
    toInject = (p.to_inject ?? []).filter((s: string) => (SOURCES as readonly string[]).includes(s));
    routeEdited = true;
  }

  const lastSequence = $derived(lastApplied($app.activity, "xtrig.set_sequence"));
  const lastRoute = $derived(lastApplied($app.activity, "xtrig.route"));

  function toggle<T>(list: T[], v: T): T[] {
    return list.includes(v) ? list.filter((x) => x !== v) : [...list, v];
  }

  const EVENT_LABEL: Record<TriggerEvent, "logic.patternA" | "logic.patternB" | "logic.dapMatch" | "logic.host"> =
    { pattern_a: "logic.patternA", pattern_b: "logic.patternB", dap_match: "logic.dapMatch", host: "logic.host" };
  const SOURCE_LABEL: Record<TriggerSource, "logic.srcTrigger" | "logic.dapMatch" | "logic.host" | "logic.srcSequence" | "logic.srcObsMatch" | "logic.srcSamplerMatch"> =
    { la_trigger: "logic.srcTrigger", dap_match: "logic.dapMatch", host: "logic.host", sequence: "logic.srcSequence",
      obs_match: "logic.srcObsMatch", sampler_match: "logic.srcSamplerMatch" };
  // 時刻 0 もタイムベースの事実の時刻（起きたかは回数・fired で決める。0 を「無い」とみなさない）
  const recordingTo = (t: number) => ({ source: "recording" as const, t });

  const laLayout = $derived((($app.probe?.capabilities ?? {}) as { la_layout?: LaChannel[] }).la_layout ?? null);
  const nameOf = (ch: number) => laChannelLabel($tl.groups, ch, $i18n, laLayout);
</script>

{#snippet patternTable(p: PatternForm, label: string)}
  <table class="grid pattern">
    <thead><tr><th>{label}</th>{#each p.level as _, ch (ch)}<th class="ch" title={nameOf(ch)}>{ch}</th>{/each}</tr></thead>
    <tbody>
      <tr><td class="muted">{$i18n("logic.level")}</td>
        {#each p.level as _, ch (ch)}
          <td class="ch"><select bind:value={p.level[ch]} aria-label="{label} ch{ch} {$i18n('logic.level')}">
            <option value="x">×</option><option value="0">0</option><option value="1">1</option></select></td>
        {/each}</tr>
      <tr><td class="muted">{$i18n("logic.rise")} ↑</td>
        {#each p.rise as _, ch (ch)}<td class="ch"><input type="checkbox" bind:checked={p.rise[ch]} aria-label="{label} ch{ch} ↑" /></td>{/each}</tr>
      <tr><td class="muted">{$i18n("logic.fall")} ↓</td>
        {#each p.fall as _, ch (ch)}<td class="ch"><input type="checkbox" bind:checked={p.fall[ch]} aria-label="{label} ch{ch} ↓" /></td>{/each}</tr>
    </tbody>
  </table>
{/snippet}

<div class="trigger">
  <section>
    <h3>{$i18n("logic.sequence")}
      {#if lastSequence}<button class="link small" onclick={loadLastSequence}
        title={$i18n("logic.loadLastTip")}>{$i18n("logic.loadLast", { who: lastSequence.client })}</button>{/if}</h3>
    <svg class="diagram" viewBox="0 0 {stages.length * 150 + 10} 56" role="img" aria-label={$i18n("logic.diagram")}
         style:max-width="{stages.length * 150 + 10}px">
      <defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto">
        <path d="M0,0 L10,5 L0,10 z" class="head" /></marker></defs>
      {#each stages as s, i (i)}
        {@const l = stageLabel(s, i)}
        <g class="stage {stageState(i, seqStatus)}">
          <rect x={i * 150 + 4} y="6" width="118" height="44" rx="6" />
          <text x={i * 150 + 63} y="24" text-anchor="middle" class="ev">{l.events}</text>
          <text x={i * 150 + 63} y="40" text-anchor="middle" class="det">{l.detail || $i18n("logic.stageN", { n: i + 1 })}</text>
        </g>
        {#if i < stages.length - 1}
          <line x1={i * 150 + 124} y1="28" x2={i * 150 + 152} y2="28" class="arrow" marker-end="url(#arrow)" />
        {/if}
      {/each}
    </svg>
    <table class="grid stages">
      <thead><tr><th>#</th><th>{$i18n("logic.when")}<Req /></th>
        <th>{$i18n("logic.count")}<Req /></th><th>{$i18n("logic.within")}</th><th></th></tr></thead>
      <tbody>
        {#each stages as s, i (i)}
          <tr>
            <td class="mono">{i + 1}</td>
            <td>{#each EVENTS as ev (ev)}
              <label class="chk"><input type="checkbox" checked={s.when.includes(ev)}
                                        onchange={() => (s.when = toggle(s.when, ev))} /> {$i18n(EVENT_LABEL[ev])}</label>
            {/each}</td>
            <td><input class="num" inputmode="numeric" aria-required="true" bind:value={s.count} /></td>
            <td>{#if i > 0}<input class="num" bind:value={s.withinUs} placeholder="∞" inputmode="decimal" /> µs{/if}</td>
            <td><button class="small" disabled={stages.length < 2} onclick={() => (stages = stages.filter((_, k) => k !== i))}
                        aria-label={$i18n("common.remove")}>×</button></td>
          </tr>
        {/each}
      </tbody>
    </table>
    <div class="bar">
      <button class="small" disabled={stages.length >= 4}
              onclick={() => (stages = [...stages, emptyStage()])}>{$i18n("logic.addStage")}</button>
      <span class="spacer"></span>
      <button class="primary" onclick={applySequence}>{$i18n("logic.arm")}</button>
    </div>
    <div class="patterns">
      {@render patternTable(patA, $i18n("logic.patternA"))}
      {@render patternTable(patB, $i18n("logic.patternB"))}
    </div>
  </section>

  <section>
    <h3>{$i18n("logic.status")}</h3>
    {#if seqStatus}
      <div class="status">
        <span>{seqStatus.fired ? $i18n("logic.fired") : seqStatus.armed ? $i18n("logic.waitingStage", { n: seqStatus.stage + 1 }) : $i18n("logic.notArmed")}</span>
        {#if seqStatus.fired}<TimeLink to={recordingTo(seqStatus.time)} />{/if}
      </div>
    {/if}
    {#if xtStatus}
      <div class="status muted">
        <span>{$i18n("logic.matchCount", { n: xtStatus.match?.count ?? 0 })}</span>
        {#if xtStatus.match?.count}<TimeLink to={recordingTo(xtStatus.match.time)} />{/if}
        <span>{$i18n("logic.injectStatus", { result: xtStatus.inject?.result ?? "—", n: xtStatus.inject?.count ?? 0 })}</span>
      </div>
    {/if}
    {#if !seqStatus && !xtStatus}<p class="muted">{$i18n("common.notAvailable")}</p>{/if}

    <h3>{$i18n("logic.match")}</h3>
    <form class="match" onsubmit={(e) => { e.preventDefault(); void applyMatch(); }}>
      <ApiFields fields={matchFields} bind:values={match} errors={matchErrors} />
      <button type="submit">{$i18n("logic.apply")}</button>
    </form>

    <h3>{$i18n("logic.route")}
      {#if lastRoute}<button class="link small" onclick={loadLastRoute}
        title={$i18n("logic.loadLastTip")}>{$i18n("logic.loadLast", { who: lastRoute.client })}</button>{/if}</h3>
    <table class="grid route">
      <thead><tr><th></th>{#each SOURCES as s (s)}<th>{$i18n(SOURCE_LABEL[s])}</th>{/each}</tr></thead>
      <tbody>
        <tr><td>{$i18n("logic.toLa")}</td>
          {#each SOURCES as s (s)}<td><input type="checkbox" checked={toLa.includes(s)} disabled={s === "la_trigger"}
                                             onchange={() => { toLa = toggle(toLa, s); routeEdited = true; }} aria-label="{$i18n('logic.toLa')} {s}" /></td>{/each}</tr>
        <tr><td title={$i18n("logic.toInjectTip")}>{$i18n("logic.toInject")}</td>
          {#each SOURCES as s (s)}<td><input type="checkbox" checked={toInject.includes(s)}
                                             onchange={() => { toInject = toggle(toInject, s); routeEdited = true; }} aria-label="{$i18n('logic.toInject')} {s}" /></td>{/each}</tr>
      </tbody>
    </table>
    <div class="bar">
      <button onclick={applyRoute}>{$i18n("logic.apply")}</button>
      <button onclick={fire} title={$i18n("logic.fireTip")}>{$i18n("logic.fire")}</button>
    </div>
    {#if triggerError}<div class="error">{triggerError}</div>{/if}
  </section>
</div>

<style>
  .error { color: var(--bad); padding: 4px 8px; font-size: 12.5px; }
  .trigger { display: grid; grid-template-columns: minmax(0, 1.3fr) minmax(0, 1fr); gap: 12px; padding: 6px 8px; overflow: auto; }
  @media (max-width: 820px) { .trigger { grid-template-columns: 1fr; } }
  section { min-width: 0; display: flex; flex-direction: column; gap: 6px; }
  h3 { font-size: 12px; color: var(--muted); font-weight: 600; margin: 4px 0 0; display: flex; gap: 8px; align-items: baseline; }
  .bar { display: flex; gap: 6px; align-items: center; }
  .bar .spacer { flex: 1; }
  .chk { display: inline-flex; gap: 3px; align-items: center; margin-right: 8px; white-space: nowrap; }
  .num { width: 5em; }
  .patterns { display: flex; gap: 12px; flex-wrap: wrap; }
  .pattern { width: auto; }
  .pattern .ch { text-align: center; padding: 1px 2px; }
  .pattern select { padding: 0 2px; }
  .status { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
  .match { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
  .route { width: auto; }
  .route td, .route th { text-align: center; }
  .diagram { width: 100%; height: 56px; }
  .diagram rect { fill: var(--panel); stroke: var(--line); stroke-width: 1.5; }
  .diagram .current rect { stroke: var(--accent); stroke-width: 2.5; }
  .diagram .done rect { stroke: var(--ok); fill: color-mix(in srgb, var(--ok) 12%, var(--panel)); }
  .diagram text { fill: var(--text); font-size: 12px; }
  .diagram .det { fill: var(--muted); font-size: 11px; }
  .diagram .arrow { stroke: var(--muted); stroke-width: 1.5; }
  .diagram .head { fill: var(--muted); }
</style>
