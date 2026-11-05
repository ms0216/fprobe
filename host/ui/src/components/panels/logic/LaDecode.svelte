<script lang="ts">
  // Logic Analyzer の Decode: 解読器（la.decoders）の選択と設定 → la.decode の表（誤りだけ・種類で絞る、件数）と、時間軸の
  // 波形の上の注釈（setMarks "decode"）。新しい捕捉では解読し直す
  import Hint from "../Hint.svelte";
  import { errorText, type Result } from "../../../lib/api";
  import { tick, untrack } from "svelte";
  import { shared } from "../../../lib/context";
  import { allowed } from "../../../lib/guard";
  import { i18n } from "../../../lib/i18n";
  import {
    decodeMarks, invalidSettings, missingSettings, newForm, settingsOf, type Annotation, type DecoderForm, type DecoderInfo,
  } from "../../../lib/logic";
  import { onCalls } from "../../../lib/notify";
  import { load, save } from "../../../lib/prefs";
  import { nearestAtOrBefore, onTimeline } from "../../../lib/timeline/events";
  import { channelChoices } from "../../../lib/waveform";
  import DecoderFields from "../DecoderFields.svelte";
  import TimeLink from "../TimeLink.svelte";

  const CAPTURES = ["la.capture", "la.collect", "la.deep.capture", "la.deep.collect", "la.load"];
  const DECODE_LIMIT = 2000;
  const MARK_OWNER = "decode";

  let { linked }: { linked: boolean } = $props();

  const { app, conn, tl, dock, reveal } = shared();

  let decoders = $state<DecoderInfo[]>([]);
  let protocol = $state(load("logicAnalyzer.protocol", "uart"));
  let form = $state<DecoderForm>({ protocol: "", values: {}, lower: null });
  // 空の必須の欄（通信の設定は既定を持たない）。あれば解読を呼ばない
  const missing = $derived(form.protocol ? missingSettings(form, decoders) : []);
  // 解読器が申告した型で読めない欄（例 チャンネルに文字）。空でなければ解読を呼べない
  const invalid = $derived(form.protocol ? invalidSettings(form, decoders) : []);
  let errorsOnly = $state(false);
  let kind = $state("");
  let result = $state<Result<"la.decode"> | null>(null);
  let decodeError = $state<string | null>(null);
  let noCapture = $state(false);
  let selected = $state(-1);
  let box = $state<HTMLDivElement>();
  let captureId = $state<number | null>(null);          // 最後の捕捉（新しい捕捉で解読し直す）

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
  // 表に出ない種類と件数は API の hidden_kinds（画面は引き算をしない）。件数の合計の表示だけ
  const hidden = $derived(result && kind === "" && !errorsOnly ? (result.hidden_kinds ?? {}) : {});
  const hiddenText = $derived(Object.entries(hidden).map(([k, n]) => `${k} ${n}`).join(", "));

  async function capture() {
    if (!(await allowed(app))) return;
    await tl.captureSaved({ deep: false });                // Timeline で入れて覚えた設定で（足りなければ理由を出す）
    await readCapture();
  }
</script>

{#if noCapture && !result}
  <div class="empty">
    <strong>{$i18n("logic.noCapture")}</strong>
    <Hint text={$i18n("logic.noCaptureHint")} />
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
      {#if hiddenText}
        <span class="muted">{$i18n("logic.hiddenKinds", { kinds: hiddenText })}</span>
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
            <tr><td colspan="4" class="muted">{hiddenText
              ? $i18n("logic.noAnnotationsHidden", { kinds: hiddenText })
              : $i18n("logic.noAnnotations")}</td></tr>
          {/each}
        </tbody>
      </table>
    </div>
  {:else if !decodeError}
    <p class="hint muted">{$i18n("logic.decodeHint")}</p>
  {/if}
{/if}

<style>
  .sub { position: static; }
  .rows { flex: 1; overflow: auto; min-height: 60px; }
  tr.selected td { background: var(--select); }
  tr.bad .err { color: var(--bad); }
  .error { color: var(--bad); padding: 4px 8px; font-size: 12.5px; }
  .hint { padding: 8px 12px; margin: 0; }
</style>
