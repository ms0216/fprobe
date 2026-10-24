<script lang="ts">
  import type { LaChannel } from "../../lib/waveform";
  // Logic Analyzer（Saleae Logic 2 の Analyzers・DSView のトリガ・PulseView の測定。G09）:
  // - Decode: 解読器（la.decoders）の選択と設定 → la.decode の表（誤りだけ・種類で絞る、件数）と、時間軸の波形の上の注釈
  //   （setMarks "decode"）。新しい捕捉では解読し直す
  // - Trigger: 多段トリガ（xtrig.set_sequence: 段 1〜4・ピンの条件 A / B）・番地一致（xtrig.set_match）・つなぎ先
  //   （xtrig.route）と状態、段の図
  // - Measure: 時間軸の基準と選択のカーソルの間（無ければ全体）を la.measure / recording.waveform_measure で測る
  // 計算は lib/logic.ts
  import { errorText } from "../../lib/api";
  import { tick, untrack } from "svelte";
  import { shared } from "../../lib/context";
  import { allowed } from "../../lib/guard";
  import { i18n } from "../../lib/i18n";
  import {
    EVENTS, SOURCES, decodeMarks, emptyPattern, hiddenKinds, formatHz, lastApplied, measureCall, measureRange, measuredChannels, missingSettings, invalidSettings, newForm,
    patternEmpty, patternForm, patternOf, settingsOf, stageForms, stageLabel, stageState, stagesOf,
    type Annotation, type ChannelMeasure, type DecoderForm, type DecoderInfo, type PatternForm, type SequenceStatus, type StageForm,
    type TriggerEvent, type TriggerSource, type WidthStats,
    laExportParams, type LaExportFormat,
  } from "../../lib/logic";
  import { onCalls } from "../../lib/notify";
  import { load, save } from "../../lib/prefs";
  import { TABLE_DIGITS, formatDuration, formatSeconds } from "../../lib/time";
  import { nearestAtOrBefore, onTimeline } from "../../lib/timeline/events";
  import { originOf } from "../../lib/timeline/state";
  import { rowTitle } from "../../lib/timeline/titles";
  import { channelChoices, channelName, laChannelCount } from "../../lib/waveform";
  import type { FieldError, FieldValues } from "../../lib/apiform";
  import { formCall, methodFields } from "../../lib/crosstrigger";
  import ApiFields from "./ApiFields.svelte";
  import DecoderFields from "./DecoderFields.svelte";
  import TabBar from "./TabBar.svelte";
  import TimeLink from "./TimeLink.svelte";

  type Tab = "decode" | "trigger" | "measure";
  const CAPTURES = ["la.capture", "la.collect", "la.deep.capture", "la.deep.collect", "la.load"];
  const DECODE_LIMIT = 2000;
  const STATUS_MS = 1000;
  const MARK_OWNER = "decode";

  const { app, conn, tl, dock, reveal } = shared();
  let tab = $state<Tab>(load("logicAnalyzer.tab", "decode") as Tab);
  const linked = $derived($app.link === "open");          // つながってから読む（つなぎ直したら読み直す）
  // ch の数はデバイスの申告だけ（届くまで null: 条件の表は空、覚えた条件の幅も変えない）
  const declaredChannels = $derived(laChannelCount($app.probe?.capabilities));
  const channels = $derived(declaredChannels ?? 0);
  $effect(() => save("logicAnalyzer.tab", tab));

  // --- Decode -----------------------------------------------------------------------------------

  let decoders = $state<DecoderInfo[]>([]);
  let protocol = $state(load("logicAnalyzer.protocol", "uart"));
  let form = $state<DecoderForm>({ protocol: "", values: {}, lower: null });
  // 空の必須の欄（通信の設定は既定を持たない）。あれば解読を呼ばない
  const missing = $derived(form.protocol ? missingSettings(form, decoders) : []);
  // 解読器が申告した型で読めない欄（例 チャンネルに文字）。空でなければ解読を呼べない
  const invalid = $derived(form.protocol ? invalidSettings(form, decoders) : []);
  let errorsOnly = $state(false);
  let kind = $state("");
  let result = $state<any>(null);
  let decodeError = $state<string | null>(null);
  let noCapture = $state(false);
  let selected = $state(-1);
  let box = $state<HTMLDivElement>();
  let captureId = $state<number | null>(null);          // 最後の捕捉（新しい捕捉で解読し直す）
  // 捕捉の保存（la.export。PulseView の .sr・VCD。パスはデーモンの PC）
  let exportFormat = $state<LaExportFormat>(load("logicAnalyzer.exportFormat", "sr") as LaExportFormat);
  let exportPath = $state<string | null>(null);            // null = 形式の既定の名前
  let exported = $state<string | null>(null);

  $effect(() => {
    if (!linked) return;
    void conn.call("la.decoders", {}, { quiet: true }).then((d) => {
      decoders = d.decoders ?? [];
      untrack(() => choose(decoders.some((x) => x.name === protocol) ? protocol : decoders[0]?.name ?? ""));
    }).catch(() => (decoders = []));
    void readCapture();
    return onCalls(conn, CAPTURES, () => void readCapture());
  });
  // 新しい捕捉: 解読していたら同じ設定で解読し直す
  $effect(() => {
    const id = captureId;
    if (id !== null && untrack(() => result && result.capture?.id !== id)) void untrack(() => decode(true));
  });
  // 閉じたら注釈を消す
  $effect(() => () => tl.setMarks(MARK_OWNER, "capture", []));
  // 時間軸で選んだ時刻（捕捉）の注釈の行を選ぶ
  $effect(() => onTimeline({
    select: (d) => {
      const rows: Annotation[] = result?.annotations ?? [];
      if (d.source !== "capture" || !rows.length) return;
      const i = nearestAtOrBefore(rows.map((a) => a.start), d.t);
      if (i >= 0) void pick(i);
    },
  }));

  async function readCapture() {
    try {
      const v = await conn.call("la.view", { start: 0, end: 1, width: 1 }, { quiet: true });
      captureId = v.capture?.id ?? null;
      noCapture = false;
    } catch {
      captureId = null;
      noCapture = true;
    }
  }

  function formKey(p: string) {
    return `logicAnalyzer.decoder.${p}`;
  }

  function choose(p: string) {
    protocol = p;
    save("logicAnalyzer.protocol", p);
    const fresh = newForm(p, decoders);
    try {
      const kept = JSON.parse(load(formKey(p), "null")) as DecoderForm | null;
      // 覚えた欄は、いまの解読器の欄の名前に合わせる（解読器の版が変わっても壊れない）
      form = kept && kept.protocol === p ? merge(fresh, kept) : fresh;
    } catch {
      form = fresh;
    }
    kind = "";
  }

  function merge(fresh: DecoderForm, kept: DecoderForm): DecoderForm {
    const values = Object.fromEntries(Object.keys(fresh.values).map((k) => [k, kept.values?.[k] ?? fresh.values[k]]));
    const lower = fresh.lower && kept.lower ? merge(kept.lower.protocol === fresh.lower.protocol ? fresh.lower
      : newForm(kept.lower.protocol, decoders), kept.lower) : fresh.lower;
    return { protocol: fresh.protocol, values, lower };
  }

  /** 解読する（auto = 新しい捕捉での解読し直し: 人の操作ではないので操作の記録に残さない）。必須の欄が空なら呼ばない */
  async function decode(auto = false) {
    if (missing.length || invalid.length) return;
    save(formKey(protocol), JSON.stringify(form));
    try {
      // 種類: "" = 解読器の既定の種類、"*" = すべて（分かっている種類を全部渡す）、ほかはその種類だけ
      const all = Object.keys(result?.counts ?? {});
      const kinds = kind === "*" ? (all.length ? all : null) : kind ? [kind] : null;
      result = await conn.call("la.decode", { protocol, settings: settingsOf(form, decoders), errors_only: errorsOnly,
                                              kinds, limit: DECODE_LIMIT }, auto ? { quiet: true } : undefined);
      decodeError = null;
      noCapture = false;
    } catch (e) {
      result = null;
      decodeError = errorText(e);
    }
    selected = -1;
    tl.setMarks(MARK_OWNER, "capture", result ? decodeMarks(result.annotations) : []);
  }

  function clearDecode() {
    result = null;
    tl.setMarks(MARK_OWNER, "capture", []);
  }

  async function pick(i: number) {
    selected = i;
    await tick();
    box?.querySelector(`[data-row="${i}"]`)?.scrollIntoView({ block: "nearest" });
  }

  const at = (a: Annotation) => ({ source: "capture" as const, t: a.start, ...(a.end > a.start ? { tEnd: a.end } : {}) });
  const kinds = $derived(Object.keys(result?.counts ?? {}).sort());
  const hidden = $derived(result && kind === "" && !errorsOnly ? hiddenKinds(result) : { n: 0, kinds: [] });

  async function capture() {
    if (!(await allowed(app))) return;
    await tl.captureSaved({ deep: false });                // Timeline で入れて覚えた設定で（足りなければ理由を出す）
    await readCapture();
  }

  // --- Trigger ----------------------------------------------------------------------------------

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
    if (tab !== "trigger" || !linked) return;
    void readTrigger();
    const timer = setInterval(() => void readTrigger(), STATUS_MS);
    return () => clearInterval(timer);
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

  // --- Measure ----------------------------------------------------------------------------------

  let measured = $state<any>(null);
  let measureError = $state<string | null>(null);
  const range = $derived(measureRange($tl.reference, $tl.selection));
  const call = $derived(measureCall($tl.source, range));
  const measureKey = $derived(JSON.stringify([call, $tl.capture?.id ?? null, $tl.recording]));

  // カーソル・捕捉・記録が変わったら測り直す（ドラッグ中は少し待つ）
  $effect(() => {
    if (tab !== "measure" || !linked) return;
    void measureKey;
    const c = call;
    const timer = setTimeout(async () => {
      try {
        measured = await conn.call(c.method, c.params as any, { quiet: true });
        measureError = null;
      } catch (e) {
        measured = null;
        measureError = errorText(e);
      }
    }, 120);
    return () => clearTimeout(timer);
  });

  const rows = $derived(measuredChannels(measured?.channels));
  const unitSource = $derived<"capture" | "recording">(measured?.at_unit === "timebase" ? "recording" : "capture");
  const ns = (v: number | undefined) => (v === undefined ? "—" : formatDuration(v * 1e-9));
  const pct = (v: number | null | undefined) => (v === null || v === undefined ? "—" : `${(v * 100).toFixed(1)}%`);
  const atLink = (t: number | undefined) => (t === undefined ? null : { source: unitSource, t });
  function nameOf(ch: number): string {
    const def = $tl.groups.find((g) => g.id === "logic")?.rows.find((r) => r.id === `wave:${ch}`);
    return def ? rowTitle(def, $i18n, laLayout) : channelName(ch, laLayout);
  }
  const rangeText = $derived.by(() => {
    if (!range) return $i18n("logic.wholeRange");
    const s = $tl.tickS, origin = originOf($tl);
    const at = (t: number) => formatSeconds((t - origin) * s, s, TABLE_DIGITS);
    return `${at(range[0])} – ${at(range[1])} (Δ ${formatDuration((range[1] - range[0]) * s)})`;
  });
  const laLayout = $derived((($app.probe?.capabilities ?? {}) as { la_layout?: LaChannel[] }).la_layout ?? null);

  async function exportCapture() {
    if (!(await allowed(app))) return;
    save("logicAnalyzer.exportFormat", exportFormat);
    const out = await app.act("la.export", laExportParams(exportFormat, exportPath));
    exported = out === undefined ? null : out.path ?? null;
  }
</script>

{#snippet width(w: WidthStats | undefined)}
  {#if w && w.count}
    <span class="w" title="σ {ns(w.stdev_ns)} · n {w.count}"><TimeLink to={atLink(w.min_at)}>{ns(w.min_ns)}</TimeLink>
      / <span class="mono">{ns(w.mean_ns)}</span> / <TimeLink to={atLink(w.max_at)}>{ns(w.max_ns)}</TimeLink></span>
  {:else}<span class="muted">—</span>{/if}
{/snippet}

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

<div class="pane la">
  <div class="pane-bar">
    <TabBar tabs={[{ id: "decode", label: $i18n("logic.decode") }, { id: "trigger", label: $i18n("logic.trigger") },
                   { id: "measure", label: $i18n("logic.measure") }]} bind:value={tab} />
    <span class="spacer"></span>
    <form class="export" onsubmit={(e) => { e.preventDefault(); void exportCapture(); }} title={$i18n("logic.exportTip")}>
      <select bind:value={exportFormat} aria-label={$i18n("logic.exportFormat")} onchange={() => (exported = null)}>
        <option value="sr">PulseView (.sr)</option><option value="vcd">VCD</option>
      </select>
      <input class="mono" value={exportPath ?? ""} placeholder={`capture-${$i18n("logic.exportStamp")}.${exportFormat}`}
             aria-label={$i18n("logic.exportPath")}
             oninput={(e) => { exportPath = e.currentTarget.value || null; exported = null; }} />
      <button class="small">{$i18n("logic.export")}</button>
      {#if exported}<span class="muted small mono" title={exported}>{$i18n("logic.exported")}: {exported}</span>{/if}
    </form>
  </div>

  {#if tab === "decode"}
    {#if noCapture && !result}
      <div class="empty">
        <strong>{$i18n("logic.noCapture")}</strong>
        <span>{$i18n("logic.noCaptureHint")}</span>
        <div class="actions">
          <button class="primary" onclick={capture}>{$i18n("logic.capture")}</button>
          <button onclick={() => dock.show("timeline")}>{$i18n("logic.openTimeline")}</button>
        </div>
      </div>
    {:else}
      <form class="pane-bar sub" onsubmit={(e) => { e.preventDefault(); void decode(); }}>
        <label>{$i18n("logic.decoder")}
          <select value={protocol} onchange={(e) => choose((e.currentTarget as HTMLSelectElement).value)}>
            {#each decoders as d (d.name)}<option value={d.name} title={d.summary}>{d.name}</option>{/each}
          </select>
        </label>
        {#if form.protocol}<DecoderFields bind:form {decoders} channels={channelChoices($app.probe?.capabilities ?? {})} />{/if}
        <button type="submit" class="primary" disabled={!form.protocol || missing.length > 0 || invalid.length > 0}
                title={missing.length ? $i18n("logic.missing", { names: missing.join(", ") })
                  : invalid.length ? $i18n("logic.invalid", { names: invalid.join(", ") }) : undefined}>{$i18n("logic.run")}</button>
        {#if result}<button type="button" class="small" onclick={clearDecode}>{$i18n("eventLog.clear")}</button>{/if}
      </form>
      {#if decodeError}<div class="error">{decodeError}</div>{/if}
      {#if result}
        <div class="pane-bar sub">
          <span>{$i18n("logic.decoded", { n: result.total, errors: result.errors })}</span>
          {#each Object.entries(result.counts ?? {}) as [k, n] (k)}<span class="muted mono">{k} {n}</span>{/each}
          <span class="spacer"></span>
          <label><input type="checkbox" bind:checked={errorsOnly} onchange={() => void decode()} /> {$i18n("logic.errorsOnly")}</label>
          <select bind:value={kind} onchange={() => void decode()} aria-label={$i18n("logic.kind")}>
            <option value="">{result.kinds ? $i18n("logic.defaultKinds", { kinds: result.kinds.join(", ") })
                                            : $i18n("logic.allKinds")}</option>
            {#if result.kinds}<option value="*">{$i18n("logic.allKinds")}</option>{/if}
            {#each kinds as k (k)}<option value={k}>{k}</option>{/each}
          </select>
          {#if hidden.n}
            <span class="muted" title={hidden.kinds.join(", ")}>{$i18n("logic.hiddenKinds", { n: hidden.n })}</span>
          {/if}
          {#if result.matched > result.annotations.length}
            <span class="muted">{$i18n("eventLog.truncated", { n: result.annotations.length })}</span>
          {/if}
        </div>
        <div class="rows" bind:this={box}>
          <table class="grid">
            <thead><tr><th>{$i18n("eventLog.time")}</th><th>{$i18n("logic.kind")}</th><th>{$i18n("logic.text")}</th>
              <th>{$i18n("logic.error")}</th></tr></thead>
            <tbody>
              {#each result.annotations as a, i (i)}
                <tr data-row={i} class:selected={i === selected} class:bad={!!a.error} onclick={() => void pick(i)}
                    ondblclick={() => reveal(at(a))}>
                  <td><TimeLink to={at(a)} /></td>
                  <td class="mono">{a.kind}</td>
                  <td class="mono" title={a.fields ? JSON.stringify(a.fields) : ""}>{a.text}</td>
                  <td class="err">{a.error ?? ""}</td>
                </tr>
              {:else}
                <tr><td colspan="4" class="muted">{hidden.n
                  ? $i18n("logic.noAnnotationsHidden", { n: hidden.n, kinds: hidden.kinds.join(", ") })
                  : $i18n("logic.noAnnotations")}</td></tr>
              {/each}
            </tbody>
          </table>
        </div>
      {:else if !decodeError}
        <p class="hint muted">{$i18n("logic.decodeHint")}</p>
      {/if}
    {/if}

  {:else if tab === "trigger"}
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
          <thead><tr><th>#</th><th>{$i18n("logic.when")}<span class="req" aria-hidden="true">*</span></th>
            <th>{$i18n("logic.count")}<span class="req" aria-hidden="true">*</span></th><th>{$i18n("logic.within")}</th><th></th></tr></thead>
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

  {:else}
    <div class="pane-bar sub">
      <span>{$i18n("logic.measureRange")}</span>
      <span class="mono">{rangeText}</span>
      <span class="muted">{$tl.source === "capture" ? $i18n("timeline.lastCapture") : $i18n("timeline.recording")}</span>
      <span class="spacer"></span>
      <span class="muted">{$i18n("logic.measureHint")}</span>
    </div>
    {#if measureError}<div class="error">{measureError}</div>{/if}
    {#if measured}
      <div class="rows">
        <table class="grid">
          <thead><tr><th>{$i18n("logic.channel")}</th><th>{$i18n("logic.frequency")}</th><th>{$i18n("logic.duty")}</th>
            <th>{$i18n("logic.period")}</th><th>{$i18n("logic.high")}</th><th>{$i18n("logic.low")}</th><th>{$i18n("logic.edges")}</th></tr></thead>
          <tbody>
            {#each rows as [ch, m] (ch)}
              {@const c = m as ChannelMeasure}
              <tr>
                <td>{nameOf(ch)}</td>
                <td class="mono nw">{formatHz(c.frequency_hz)}</td>
                <td class="mono nw" title="{pct(c.duty_min)} – {pct(c.duty_max)}">{pct(c.duty)}</td>
                <td>{@render width(c.period)}</td>
                <td>{@render width(c.high)}</td>
                <td>{@render width(c.low)}</td>
                <td class="mono nw">↑{c.rises} ↓{c.falls}</td>
              </tr>
            {:else}
              <tr><td colspan="7" class="muted">{$i18n("logic.noEdges")}</td></tr>
            {/each}
          </tbody>
        </table>
        <p class="muted small-note">{$i18n("logic.resolution", { r: ns(measured.resolution_ns) })}</p>
      </div>
    {:else if !measureError}
      <p class="hint muted">{$i18n("logic.measuring")}</p>
    {/if}
  {/if}
</div>

<style>
  .export { display: flex; gap: 6px; align-items: center; }
  .export input { width: 14em; }
  .la { display: flex; flex-direction: column; }
  .sub { position: static; }
  .rows { flex: 1; overflow: auto; min-height: 60px; }
  tr.selected td { background: var(--select); }
  tr.bad .err { color: var(--bad); }
  .error { color: var(--bad); padding: 4px 8px; font-size: 12.5px; }
  .hint { padding: 8px 12px; margin: 0; }
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
  .w, .nw { white-space: nowrap; }
  .small-note { padding: 4px 8px; margin: 0; font-size: 12px; }
</style>
