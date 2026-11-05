<script lang="ts">
  // Logic Analyzer の Measure: 時間軸の基準と選択のカーソルの間（無ければ全体）を la.measure / recording.waveform_measure で測る
  import { errorText } from "../../../lib/api";
  import { shared } from "../../../lib/context";
  import { i18n } from "../../../lib/i18n";
  import { formatHz, measureCall, measureRange, measuredChannels, type ChannelMeasure, type WidthStats } from "../../../lib/logic";
  import { TABLE_DIGITS, formatDuration, formatSeconds } from "../../../lib/time";
  import { originOf } from "../../../lib/timeline/state";
  import { laChannelLabel } from "../../../lib/lachannel";
  import type { LaChannel } from "../../../lib/waveform";
  import TimeLink from "../TimeLink.svelte";

  let { active, linked }: { active: boolean; linked: boolean } = $props();

  const { app, conn, tl } = shared();

  let measured = $state<any>(null);
  let measureError = $state<string | null>(null);
  const range = $derived(measureRange($tl.reference, $tl.selection));
  const call = $derived(measureCall($tl.source, range));
  const measureKey = $derived(JSON.stringify([call, $tl.capture?.id ?? null, $tl.recording]));

  // カーソル・捕捉・記録が変わったら測り直す（ドラッグ中は少し待つ）
  $effect(() => {
    if (!active || !linked) return;
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
  const laLayout = $derived((($app.probe?.capabilities ?? {}) as { la_layout?: LaChannel[] }).la_layout ?? null);
  const nameOf = (ch: number) => laChannelLabel($tl.groups, ch, $i18n, laLayout);
  const rangeText = $derived.by(() => {
    if (!range) return $i18n("logic.wholeRange");
    const s = $tl.tickS, origin = originOf($tl);
    const at = (t: number) => formatSeconds((t - origin) * s, s, TABLE_DIGITS);
    return `${at(range[0])} – ${at(range[1])} (Δ ${formatDuration((range[1] - range[0]) * s)})`;
  });
</script>

{#snippet width(w: WidthStats | undefined)}
  {#if w && w.count}
    <span class="w" title="σ {ns(w.stdev_ns)} · n {w.count}"><TimeLink to={atLink(w.min_at)}>{ns(w.min_ns)}</TimeLink>
      / <span class="mono">{ns(w.mean_ns)}</span> / <TimeLink to={atLink(w.max_at)}>{ns(w.max_ns)}</TimeLink></span>
  {:else}<span class="muted">—</span>{/if}
{/snippet}

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

<style>
  .sub { position: static; }
  .rows { flex: 1; overflow: auto; min-height: 60px; }
  .error { color: var(--bad); padding: 4px 8px; font-size: 12.5px; }
  .hint { padding: 8px 12px; margin: 0; }
  .w, .nw { white-space: nowrap; }
  .small-note { padding: 4px 8px; margin: 0; font-size: 12px; }
</style>
