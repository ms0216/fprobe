<script lang="ts">
  // クロストリガー: fprobe のクロストリガの全体（何が起きたら = 源、何をする = 行き先、どの源をどの行き先へ = 経路）を
  // 1 か所で見て設定する（docs/superpowers/specs/2026-10-23-ui-rebuild-design.md §3.3 の作る順の 2 番目、
  // docs/reports/2026-10-24-fprobe-value-analysis.md §5.7-2）。
  // - 経路の表: 行 = 源、列 = 行き先（ロジアナの取り込み・割り込み書き込み・設定済みの出力の端子）と回数・最後の時刻。
  //   セルは機器の事実（xtrig.status の route・xtrig.output_status の sources）。書き換えた間は手元の下書きで、適用で
  //   xtrig.route / xtrig.output_set を呼ぶ（ほかの道具が書いたビットを黙って消さない）
  // - 源・行き先の欄: 条件は API の引数そのまま（ApiFields）。今の設定と状態は機器のレジスタの読み戻し（xtrig.status の
  //   match・inject、xtrig.sequence_status の config、la.trigger_status）。回路に回数・時刻のレジスタが無いものは
  //   「機器から読み戻せない」とだけ示す（操作の記録の最後の設定を今の設定として出さない。推さない）
  // - 構成に無い源・行き先は隠さず、灰色にしてケイパビリティの名前と値を出す
  // ロジアナの窓（多段・番地一致・経路）・システム解析（FPGA の条件）・低レベル（出力の端子）と同じ API を呼ぶ。計算は lib/crosstrigger.ts
  import type { Snippet } from "svelte";
  import { errorText } from "../../lib/api";
  import type { FieldError, FieldValues } from "../../lib/apiform";
  import { shared } from "../../lib/context";
  import {
    SOURCES, UNREADABLE, actionAvailability, cellBlock, formCall, formValues, hexText, injectFacts, methodFields,
    outputColumns, outputSourcesCall, routeChanged, routeOf, routeParams, sourceAvailability, sourceFacts, toggleRoute,
    type Action, type Avail, type Block, type Fact, type LaTriggerStatus, type ObsTriggerStatus, type OutputPin, type Route, type RouteColumn,
    type SamplerTriggerStatus, type TriggerSource, type XtrigStatus,
  } from "../../lib/crosstrigger";
  import { allowed } from "../../lib/guard";
  import { i18n, type Key } from "../../lib/i18n";
  import type { SequenceStatus } from "../../lib/logic";
  import ApiFields from "./ApiFields.svelte";
  import TimeLink from "./TimeLink.svelte";

  const POLL_MS = 1000;
  const { app, conn, dock } = shared();
  const linked = $derived($app.link === "open");
  const caps = $derived(($app.probe?.capabilities ?? null) as Record<string, unknown> | null);
  const srcOk = $derived(sourceAvailability(caps));
  const actOk = $derived(actionAvailability(caps));

  // --- 読む（1 秒ごと。構成に無いものは呼ばない） -----------------------------------------------------

  let xtrig = $state<XtrigStatus | null>(null);
  let la = $state<LaTriggerStatus | null>(null);
  let sequence = $state<SequenceStatus | null>(null);
  let obs = $state<ObsTriggerStatus | null>(null);
  let sampler = $state<SamplerTriggerStatus | null>(null);
  let outputs = $state<{ outputs: OutputPin[]; actions: number; free: number } | null>(null);
  let readErrors = $state<Record<string, string>>({});
  let reading = false;

  $effect(() => {
    if (!linked) return;
    void read();
    const timer = setInterval(() => void read(), POLL_MS);
    return () => clearInterval(timer);
  });

  async function one<T>(method: string, on: boolean, errs: Record<string, string>): Promise<T | null> {
    if (!on) return null;
    try {
      return await conn.call(method as any, {}, { quiet: true });
    } catch (e) {
      errs[method] = errorText(e);
      return null;
    }
  }

  async function read() {
    if (reading) return;                       // 前の読みが 1 秒を超えたら重ねない
    reading = true;
    try {
      const errs: Record<string, string> = {};
      [xtrig, la, sequence, obs, sampler, outputs] = await Promise.all([
        one<XtrigStatus>("xtrig.status", srcOk.host.ok, errs),
        one<LaTriggerStatus>("la.trigger_status", srcOk.la_trigger.ok, errs),
        one<SequenceStatus>("xtrig.sequence_status", srcOk.sequence.ok, errs),
        one<ObsTriggerStatus>("fpga_obs.trigger_status", srcOk.obs_match.ok, errs),
        one<SamplerTriggerStatus>("sampler.trigger_status", srcOk.sampler_match.ok, errs),
        one<{ outputs: OutputPin[]; actions: number; free: number }>("xtrig.output_status", actOk.outputs.ok, errs),
      ]);
      readErrors = errs;
    } finally {
      reading = false;
    }
  }

  // --- 経路の表 -----------------------------------------------------------------------------------

  const RouteColumns: RouteColumn[] = ["to_la", "to_inject"];
  const facts = $derived(routeOf(xtrig));
  const columns = $derived(outputColumns(outputs?.outputs));
  let draft = $state<Route>({ to_la: [], to_inject: [] });
  let outDraft = $state<Record<number, TriggerSource[]>>({});
  let edited = $state(false);
  let routeError = $state<string | null>(null);
  // 書き換えていない間は事実に合わせる
  $effect(() => {
    if (edited) return;
    if (facts) draft = { to_la: [...facts.to_la], to_inject: [...facts.to_inject] };
    outDraft = Object.fromEntries(columns.map((c) => [c.channel, [...c.sources]]));
  });

  function toggle(column: RouteColumn, s: TriggerSource) {
    draft = toggleRoute(draft, column, s);
    edited = true;
  }

  function toggleOut(channel: number, s: TriggerSource) {
    const list = outDraft[channel] ?? [];
    outDraft = { ...outDraft, [channel]: list.includes(s) ? list.filter((x) => x !== s) : [...list, s] };
    edited = true;
  }

  const outChanged = (channel: number, s: TriggerSource) => {
    const col = columns.find((c) => c.channel === channel);
    return !!col && col.sources.includes(s) !== (outDraft[channel] ?? []).includes(s);
  };
  const routeDirty = $derived(!!facts && RouteColumns.some((c) => SOURCES.some((s) => routeChanged(facts, draft, c, s))));

  async function applyRoute() {
    if (!(await allowed(app))) return;
    routeError = null;
    let ok = true;
    if (routeDirty) ok = (await app.act("xtrig.route", routeParams(draft))) !== undefined;
    for (const col of columns) {
      if (!ok) break;
      const want = outDraft[col.channel] ?? [];
      if (!SOURCES.some((s) => outChanged(col.channel, s))) continue;
      const pin = outputs?.outputs.find((p) => p.channel === col.channel);
      const call = pin ? outputSourcesCall(pin, want) : { error: "notConfigured" as const };
      if ("error" in call) {
        routeError = $i18n(`xt.outErr.${call.error}` as Key, { ch: col.channel });
        ok = false;
        break;
      }
      ok = (await app.act(call.method as any, call.params as any)) !== undefined;
    }
    if (ok) edited = false;
    await read();
  }

  function revert() {
    edited = false;
    routeError = null;
  }

  // --- 条件の欄（API の引数そのまま） ------------------------------------------------------------------

  type FormMethod = "xtrig.set_match" | "fpga_obs.trigger_set" | "sampler.trigger_set" | "xtrig.set_inject";
  const FORMS: FormMethod[] = ["xtrig.set_match", "fpga_obs.trigger_set", "sampler.trigger_set", "xtrig.set_inject"];
  const fields = Object.fromEntries(FORMS.map((m) => [m, methodFields(m)])) as Record<FormMethod, ReturnType<typeof methodFields>>;
  let values = $state(Object.fromEntries(FORMS.map((m) => [m, Object.fromEntries(fields[m].map((f) => [f.name, ""]))])) as
    Record<FormMethod, FieldValues>);
  let formErrors = $state<Partial<Record<FormMethod, Record<string, FieldError>>>>({});
  const missing = (m: FormMethod) => fields[m].some((f) => f.required && !(values[m][f.name] ?? "").trim());

  async function submit(m: FormMethod) {
    const call = formCall(m, values[m]);
    if ("errors" in call) {
      formErrors = { ...formErrors, [m]: call.errors };
      return;
    }
    formErrors = { ...formErrors, [m]: {} };
    if (!(await allowed(app))) return;
    await app.act(call.method as any, call.params as any);
    await read();
  }

  /** 読み戻した今の設定を欄へ（機器の事実） */
  function fill(m: FormMethod, read: Record<string, unknown> | null | undefined) {
    values[m] = { ...Object.fromEntries(fields[m].map((f) => [f.name, ""])), ...formValues(m, read) };
    formErrors = { ...formErrors, [m]: {} };
  }

  async function press(method: string, params: Record<string, unknown> = {}) {
    if (!(await allowed(app))) return;
    await app.act(method as any, params as any);
    await read();
  }

  // --- 表示 ---------------------------------------------------------------------------------------

  const SOURCE_LABEL: Record<TriggerSource, Key> = {
    la_trigger: "logic.srcTrigger", dap_match: "logic.dapMatch", host: "logic.host", sequence: "logic.srcSequence",
    obs_match: "logic.srcObsMatch", sampler_match: "logic.srcSamplerMatch",
  };
  const SOURCE_API: Record<TriggerSource, string> = {
    la_trigger: "la.arm", dap_match: "xtrig.set_match", host: "xtrig.fire", sequence: "xtrig.set_sequence",
    obs_match: "fpga_obs.trigger_set", sampler_match: "sampler.trigger_set",
  };
  const ACTION_LABEL: Record<Action, Key> = { la: "xt.actLa", inject: "xt.actInject", outputs: "xt.actOutputs" };
  const COLUMN_ACTION: Record<RouteColumn, Action> = { to_la: "la", to_inject: "inject" };
  const statuses = $derived({ xtrig, la, sequence, obs, sampler });
  // 時刻 0 もタイムベースの事実の時刻（起きたかは回数・fired で決まる。0 を「無い」とみなさない）
  const recordingTo = (t: number | null) => (t !== null ? { source: "recording" as const, t } : null);
  const capText = (a: { cap: string; value: unknown }) => `${a.cap} = ${a.value === undefined ? "—" : JSON.stringify(a.value)}`;
  const absent = (a: Avail) => (a.ok ? null : $i18n("xt.absent", { cap: capText(a) }));
  const blockText = (b: Block) => (b.kind === "self" ? $i18n("xt.self")
    : $i18n(b.kind === "source" ? "xt.absentSource" : "xt.absentAction", { cap: capText(b) }));
  const EVENT_SHORT: Record<string, string> = { pattern_a: "A", pattern_b: "B", dap_match: "Match", host: "Host" };
  const patText = (p: { mask: number; value: number; rise: number; fall: number }) =>
    `mask=${hexText(p.mask)} value=${hexText(p.value)} rise=${hexText(p.rise)} fall=${hexText(p.fall)}`;
  const inject = $derived(injectFacts(xtrig));
  const hex = (n: number) => `0x${(n >>> 0).toString(16).toUpperCase().padStart(8, "0")}`;
  const STATE_MARK: Record<string, string> = { done: "✓", current: "●", waiting: "○", idle: "–" };
</script>

{#snippet when(t: Fact)}
  {#if t === UNREADABLE}<span class="muted small" title={$i18n("xt.unreadableTip")}>{$i18n("xt.unreadable")}</span>
  {:else if t !== null}<TimeLink to={recordingTo(t)} />{:else}<span class="muted">—</span>{/if}
{/snippet}

{#snippet count(n: Fact)}
  {#if n === UNREADABLE}<span class="muted small" title={$i18n("xt.unreadableTip")}>{$i18n("xt.unreadable")}</span>
  {:else}<span class="mono">{n ?? "—"}</span>{/if}
{/snippet}

{#snippet capState(name: string, c: { capturing: boolean; triggered: boolean; done: boolean; trigger_time: number | null })}
  <span>{name}: {c.capturing ? $i18n("xt.capturing") : c.done ? $i18n("xt.done") : $i18n("xt.idle")}{c.triggered
    ? ` · ${$i18n("xt.triggered")}` : ""}</span>
  {#if c.trigger_time !== null}{@render when(c.trigger_time)}{/if}
{/snippet}

{#snippet form(m: FormMethod, label: Key, extra?: Snippet)}
  <form class="form" onsubmit={(e) => { e.preventDefault(); void submit(m); }}>
    <ApiFields fields={fields[m]} bind:values={values[m]} errors={formErrors[m] ?? {}} />
    <button type="submit" disabled={missing(m)} title={missing(m) ? $i18n("xt.fillRequired") : m}>{$i18n(label)}</button>
    {@render extra?.()}
  </form>
{/snippet}

<div class="pane xt">
  <div class="pane-bar">
    <span class="muted">{$i18n("xt.summary")}</span>
    <span class="spacer"></span>
    <button class="small" onclick={() => void read()}>{$i18n("common.refresh")}</button>
  </div>

  <div class="body">
    {#if !srcOk.host.ok}
      <p class="gone">{$i18n("xt.noXtrig", { cap: capText(srcOk.host as { cap: string; value: unknown }) })}</p>
    {/if}

    <!-- 経路の表 -->
    <section>
      <h3>{$i18n("xt.route")} <span class="muted small">{$i18n("xt.routeTip")}</span></h3>
      <div class="scroll">
        <table class="grid route">
          <thead>
            <tr>
              <th class="left">{$i18n("xt.source")}</th>
              {#each RouteColumns as c (c)}
                {@const a = actOk[COLUMN_ACTION[c]]}
                <th class:gone={!a.ok} title={absent(a) ?? (c === "to_la" ? $i18n("xt.toLaTip") : $i18n("logic.toInjectTip"))}>
                  {$i18n(ACTION_LABEL[COLUMN_ACTION[c]])}<div class="mono small muted">{c}</div></th>
              {/each}
              {#each columns as col (col.channel)}
                <th title={col.editable ? $i18n("xt.outTip") : $i18n("xt.outErr.pulse", { ch: col.channel })}>
                  {$i18n("xt.outCh", { ch: col.channel })}</th>
              {/each}
              <th>{$i18n("xt.count")}</th>
              <th>{$i18n("xt.lastTime")}</th>
            </tr>
          </thead>
          <tbody>
            {#each SOURCES as s (s)}
              {@const ok = srcOk[s]}
              {@const f = sourceFacts(s, statuses)}
              <tr class:gone={!ok.ok}>
                <td class="left" title="{s} ({SOURCE_API[s]})">
                  {$i18n(SOURCE_LABEL[s])}
                  {#if !ok.ok}<div class="small reason">{absent(ok)}</div>{/if}
                </td>
                {#each RouteColumns as c (c)}
                  {@const b = cellBlock(s, c, srcOk, actOk)}
                  <td class:changed={routeChanged(facts, draft, c, s)} title={b ? blockText(b) : ""}>
                    {#if b?.kind === "self"}<span class="muted">·</span>{:else}
                    <input type="checkbox" checked={draft[c].includes(s)} disabled={!!b || !facts}
                           onchange={() => toggle(c, s)} aria-label="{c} {s}" />{/if}
                  </td>
                {/each}
                {#each columns as col (col.channel)}
                  <td class:changed={outChanged(col.channel, s)}>
                    <input type="checkbox" checked={(outDraft[col.channel] ?? []).includes(s)}
                           disabled={!col.editable || !ok.ok} onchange={() => toggleOut(col.channel, s)}
                           aria-label="ch{col.channel} {s}" />
                  </td>
                {/each}
                <td>{@render count(f.count)}</td>
                <td>{#if f.laTimes}
                  {#if f.laTimes.internal === null && f.laTimes.deep === null}<span class="muted">—</span>{/if}
                  {#if f.laTimes.internal !== null}<span class="small muted">{$i18n("xt.internal")}</span>
                    {@render when(f.laTimes.internal)}{/if}
                  {#if f.laTimes.deep !== null}<span class="small muted">{$i18n("xt.deep")}</span>
                    {@render when(f.laTimes.deep)}{/if}
                {:else}{@render when(f.time)}{/if}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
      {#if xtrig && !facts}<div class="muted small">{$i18n("xt.noRouteFacts")}</div>{/if}
      {#if facts?.unknown.length}<div class="muted small">{$i18n("xt.unknownSources", { names: facts.unknown.join(", ") })}</div>{/if}
      {#if actOk.outputs.ok && outputs && !columns.length}<div class="muted small">{$i18n("xt.noOutputColumns")}</div>{/if}
      <div class="bar">
        <button class="primary" disabled={!edited} onclick={() => void applyRoute()}>{$i18n("logic.apply")}</button>
        <button disabled={!edited} onclick={revert}>{$i18n("xt.revert")}</button>
        {#if edited}<span class="muted small">{$i18n("xt.editing")}</span>{/if}
        {#if routeError}<span class="error">{routeError}</span>{/if}
        {#if readErrors["xtrig.status"]}<span class="error">{readErrors["xtrig.status"]}</span>{/if}
      </div>
    </section>

    <div class="cols">
      <!-- 源 -->
      <section>
        <h3>{$i18n("xt.sources")}</h3>

        <div class="card" class:gone={!srcOk.la_trigger.ok}>
          <h4>{$i18n("logic.srcTrigger")} <span class="mono small muted">la_trigger</span></h4>
          {#if !srcOk.la_trigger.ok}<div class="reason small">{absent(srcOk.la_trigger)}</div>{:else}
            {#if la}
              <div class="facts mono small">
                {#if la.external_only}<span>{$i18n("xt.externalOnly")}</span>{:else}<span>{patText(la)}</span>{/if}
                {#if la.pulse && la.pulse.mode !== "none"}<span>pulse {la.pulse.mode} ch{la.pulse.channel} level={la.pulse.level}
                  {la.pulse.min_ns.toPrecision(3)}–{la.pulse.max_ns === null ? "∞" : la.pulse.max_ns.toPrecision(3)} ns</span>{/if}
                {#if la.filter && la.filter.min_width_ns !== null}<span>filter {hexText(la.filter.channels)} ≥{la.filter.min_width_ns.toPrecision(3)} ns</span>{/if}
              </div>
              <div class="facts small">
                {#if la.internal}{@render capState($i18n("xt.internal"), la.internal)}{/if}
                {#if la.deep}{@render capState($i18n("xt.deep"), la.deep)}{/if}
              </div>
            {:else if readErrors["la.trigger_status"]}<div class="error">{readErrors["la.trigger_status"]}</div>{/if}
            <div class="small muted">{$i18n("xt.laCountUnreadable")}</div>
            <div class="bar"><button class="small" onclick={() => dock.show("logicAnalyzer")}>{$i18n("xt.openLa")}</button></div>
          {/if}
        </div>

        <div class="card" class:gone={!srcOk.dap_match.ok}>
          <h4>{$i18n("logic.dapMatch")} <span class="mono small muted">dap_match</span></h4>
          {#if !srcOk.dap_match.ok}<div class="reason small">{absent(srcOk.dap_match)}</div>{:else}
            <div class="facts">
              <span>{$i18n("xt.countIs", { n: xtrig?.match?.count ?? "—" })}</span>
              {@render when(sourceFacts("dap_match", statuses).time)}
            </div>
            {#if xtrig?.match}
              {@const m = xtrig.match}
              <div class="facts mono small">
                <span>address={hexText(m.address)}/{hexText(m.address_mask)}</span>
                <span>data={hexText(m.data)}/{hexText(m.data_mask)}</span>
                <span>on_write={String(m.on_write)} on_read={String(m.on_read)}</span>
              </div>
            {/if}
            {#snippet matchFill()}<button type="button" disabled={!xtrig?.match} onclick={() => fill("xtrig.set_match", xtrig?.match)}
                                          title={$i18n("xt.toFieldsTip")}>{$i18n("xt.toFields")}</button>{/snippet}
            {@render form("xtrig.set_match", "xt.set", matchFill)}
          {/if}
        </div>

        <div class="card" class:gone={!srcOk.host.ok}>
          <h4>{$i18n("logic.host")} <span class="mono small muted">host</span></h4>
          {#if !srcOk.host.ok}<div class="reason small">{absent(srcOk.host)}</div>{:else}
            <div class="small muted">{$i18n("xt.hostUnreadable")}</div>
            <div class="bar"><button onclick={() => void press("xtrig.fire")} title={$i18n("logic.fireTip")}>{$i18n("logic.fire")}</button></div>
          {/if}
        </div>

        <div class="card" class:gone={!srcOk.sequence.ok}>
          <h4>{$i18n("logic.srcSequence")} <span class="mono small muted">sequence</span></h4>
          {#if !srcOk.sequence.ok}<div class="reason small">{absent(srcOk.sequence)}</div>{:else}
            {#if sequence}
              <div class="facts">
                <span>{sequence.fired ? $i18n("logic.fired") : sequence.armed ? $i18n("logic.waitingStage", { n: sequence.stage + 1 })
                                                                              : $i18n("logic.notArmed")}</span>
                {#if sequence.fired}{@render when(sequence.time)}{/if}
                <span class="mono" title={sequence.stage_states.join(" ")}>
                  {#each sequence.stage_states as st, i (i)}<span class="stage {st}">{i + 1}{STATE_MARK[st] ?? st}</span>{/each}
                </span>
              </div>
            {:else if readErrors["xtrig.sequence_status"]}<div class="error">{readErrors["xtrig.sequence_status"]}</div>{/if}
            {#if sequence?.config}
              <div class="facts mono small">
                {#each sequence.config.stages as st, i (i)}
                  <span>{i + 1}: {st.when.map((w) => EVENT_SHORT[w] ?? w).join(" | ") || "—"}{st.count > 1 ? ` ×${st.count}` : ""}{st.within_us !== null
                    ? ` ≤${st.within_us.toPrecision(4)} µs` : ""}</span>
                {/each}
              </div>
              <div class="facts mono small"><span>A: {patText(sequence.config.pattern_a)}</span><span>B: {patText(sequence.config.pattern_b)}</span></div>
            {/if}
            <div class="small muted">{$i18n("xt.seqCountUnreadable")}</div>
            <div class="bar"><button class="small" onclick={() => dock.show("logicAnalyzer")}>{$i18n("xt.openLaSeq")}</button></div>
          {/if}
        </div>

        <div class="card" class:gone={!srcOk.obs_match.ok}>
          <h4>{$i18n("logic.srcObsMatch")} <span class="mono small muted">obs_match</span></h4>
          {#if !srcOk.obs_match.ok}<div class="reason small">{absent(srcOk.obs_match)}</div>{:else}
            {#if obs}
              <div class="facts">
                {#if obs.condition !== null}
                  <span class="mono">{obs.condition}</span>
                  <span>halt={String(obs.halt)} la={String(obs.la)}</span>
                  <span>{$i18n("xt.countIs", { n: obs.count ?? "—" })}</span>
                  <span class="muted small">{$i18n("xt.obsTimeUnreadable")}</span>
                {:else if obs.orphan}
                  <span class="error">{$i18n("xt.obsOrphan", { mask: obs.mask ?? 0, value: obs.value ?? 0, n: obs.count ?? 0 })}</span>
                {:else}<span class="muted">{$i18n("xt.notSet")}</span>{/if}
              </div>
            {:else if readErrors["fpga_obs.trigger_status"]}<div class="error">{readErrors["fpga_obs.trigger_status"]}</div>{/if}
            {#snippet obsClear()}<button type="button" disabled={!obs || (obs.condition === null && !obs.orphan)}
                                        onclick={() => void press("fpga_obs.trigger_clear")}>{$i18n("xt.clear")}</button>{/snippet}
            {@render form("fpga_obs.trigger_set", "xt.set", obsClear)}
          {/if}
        </div>

        <div class="card" class:gone={!srcOk.sampler_match.ok}>
          <h4>{$i18n("logic.srcSamplerMatch")} <span class="mono small muted">sampler_match</span></h4>
          {#if !srcOk.sampler_match.ok}<div class="reason small">{absent(srcOk.sampler_match)}</div>{:else}
            {#if sampler}
              <div class="facts">
                {#if sampler.enabled}
                  <span class="mono">({$i18n("xt.word")} ^ {hex(sampler.value)}) & {hex(sampler.mask)} = 0 @ {hex(sampler.address)}</span>
                  <span>halt={String(sampler.halt)} la={String(sampler.la)}</span>
                  <span>{$i18n("xt.countIs", { n: sampler.count })}</span>
                  <span>{$i18n("xt.seen", { n: sampler.seen })}</span>
                  <span>{sampler.matched ? $i18n("xt.matchedNow") : $i18n("xt.notMatchedNow")}</span>
                {:else}<span class="muted">{$i18n("xt.notSet")}</span>{/if}
              </div>
              {#if sampler.enabled && sampler.seen === 0}<div class="small muted">{$i18n("xt.samplerUnseen")}</div>{/if}
            {:else if readErrors["sampler.trigger_status"]}<div class="error">{readErrors["sampler.trigger_status"]}</div>{/if}
            {#snippet samplerClear()}<button type="button" disabled={!sampler?.enabled}
                                            onclick={() => void press("sampler.trigger_clear")}>{$i18n("xt.clear")}</button>{/snippet}
            {@render form("sampler.trigger_set", "xt.set", samplerClear)}
          {/if}
        </div>
      </section>

      <!-- 行き先 -->
      <section>
        <h3>{$i18n("xt.actions")}</h3>

        <div class="card" class:gone={!actOk.la.ok}>
          <h4>{$i18n("xt.actLa")} <span class="mono small muted">to_la</span></h4>
          {#if !actOk.la.ok}<div class="reason small">{absent(actOk.la)}</div>{:else}
            <div class="small">{$i18n("xt.laExternal")}</div>
            {#if la}<div class="small">{la.external_only ? $i18n("xt.externalOnlyNow") : $i18n("xt.patternNow")}</div>{/if}
          {/if}
        </div>

        <div class="card" class:gone={!actOk.inject.ok}>
          <h4>{$i18n("xt.actInject")} <span class="mono small muted">to_inject</span></h4>
          {#if !actOk.inject.ok}<div class="reason small">{absent(actOk.inject)}</div>{:else}
            {#if inject}
              <div class="facts">
                <span>{$i18n("xt.injectResult", { result: inject.result })}</span>
                {#if inject.busy}<span>{$i18n("xt.busy")}</span>{/if}
                <span>{$i18n("xt.countIs", { n: inject.count })}</span>
                {@render when(inject.time)}
              </div>
            {/if}
            {#if xtrig?.inject}
              <div class="facts mono small">
                <span>AP{xtrig.inject.ap} {hexText(xtrig.inject.address)} ← {hexText(xtrig.inject.value)}</span>
              </div>
            {/if}
            {#snippet injectFill()}<button type="button" disabled={!xtrig?.inject} onclick={() => fill("xtrig.set_inject", xtrig?.inject)}
                                           title={$i18n("xt.toFieldsTip")}>{$i18n("xt.toFields")}</button>{/snippet}
            {@render form("xtrig.set_inject", "xt.set", injectFill)}
          {/if}
        </div>

        <div class="card" class:gone={!actOk.outputs.ok}>
          <h4>{$i18n("xt.actOutputs")} <span class="mono small muted">xtrig.output_*</span>
            {#if outputs}<span class="muted small">{$i18n("lowLevel.outputsFree", { free: outputs.free, actions: outputs.actions })}</span>{/if}</h4>
          {#if !actOk.outputs.ok}<div class="reason small">{absent(actOk.outputs)}</div>{:else}
            {#if outputs}
              <table class="grid">
                <thead><tr><th>ch</th><th>{$i18n("xt.state")}</th><th>mode</th><th>level</th><th>{$i18n("xt.condition")}</th><th></th></tr></thead>
                <tbody>
                  {#each outputs.outputs as o (o.channel)}
                    <tr>
                      <td class="mono">ch{o.channel}{#if o.pull_up}<span class="muted small gap" title={$i18n("xt.pullUpTip")}>pull-up</span>{/if}</td>
                      <td>{!o.configured ? $i18n("xt.outFree") : o.active ? $i18n("xt.outActive") : o.enabled ? $i18n("xt.outEnabled")
                           : $i18n("xt.outDisabled")}</td>
                      <td class="mono">{[o.mode ?? "", o.pulse_us ? `${o.pulse_us.min.toPrecision(3)}–${o.pulse_us.max.toPrecision(3)} µs` : ""].join(" ")}</td>
                      <td class="mono">{o.level ?? ""}</td>
                      <td class="mono small">{#if o.configured}{(o.sources ?? []).join(" | ")}{#if o.pattern?.mask}
                        {(o.sources ?? []).length ? " | " : ""}pins {hex(o.pattern.value)}/{hex(o.pattern.mask)}{o.pattern.any ? " any" : ""}{/if}{/if}</td>
                      <td class="nw">{#if o.configured}
                        <button class="small" onclick={() => void press(o.enabled ? "xtrig.output_disable" : "xtrig.output_enable", { channel: o.channel })}>
                          {o.enabled ? $i18n("lowLevel.disable") : $i18n("lowLevel.enable")}</button>
                        <button class="small" title={$i18n("lowLevel.releaseTip")}
                                onclick={() => void press("xtrig.output_release", { channel: o.channel })}>{$i18n("lowLevel.release")}</button>
                      {/if}</td>
                    </tr>
                  {/each}
                </tbody>
              </table>
            {:else if readErrors["xtrig.output_status"]}<div class="error">{readErrors["xtrig.output_status"]}</div>{/if}
            <div class="bar"><button class="small" onclick={() => dock.show("lowLevel")}>{$i18n("xt.openLowLevel")}</button></div>
          {/if}
        </div>
      </section>
    </div>
  </div>
</div>

<style>
  .xt { display: flex; flex-direction: column; min-height: 0; }
  .body { flex: 1; overflow: auto; padding: 6px 8px; display: flex; flex-direction: column; gap: 10px; }
  section { min-width: 0; display: flex; flex-direction: column; gap: 6px; }
  h3 { font-size: 12px; color: var(--muted); font-weight: 600; margin: 2px 0 0; display: flex; gap: 8px; align-items: baseline; }
  h4 { font-size: 12.5px; font-weight: 600; margin: 0; display: flex; gap: 6px; align-items: baseline; flex-wrap: wrap; }
  .cols { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 12px; }
  @media (max-width: 760px) { .cols { grid-template-columns: 1fr; } }
  .card { border: 1px solid var(--line); border-radius: 6px; padding: 6px 8px; display: flex; flex-direction: column; gap: 4px; }
  .scroll { overflow-x: auto; }
  .route { width: auto; }
  .route td, .route th { text-align: center; }
  .route .left { text-align: left; }
  td.changed { background: color-mix(in srgb, var(--accent) 18%, transparent); }
  .gone { opacity: 0.55; }
  .gone .reason, .reason { color: var(--muted); font-style: italic; }
  p.gone { margin: 0; color: var(--muted); }
  .facts { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; font-size: 12.5px; }
  .form { display: flex; gap: 6px; flex-wrap: wrap; align-items: center; }
  .bar { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
  .small { font-size: 11.5px; }
  .error { color: var(--bad); font-size: 12px; }
  .stage { margin-right: 6px; }
  .stage.done { color: var(--ok); }
  .stage.current { color: var(--accent); font-weight: 600; }
  .nw { white-space: nowrap; }
  .gap { margin-left: 4px; }
</style>
