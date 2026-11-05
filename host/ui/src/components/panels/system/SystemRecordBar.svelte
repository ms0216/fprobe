<script lang="ts">
  // システム解析のツールバー: 記録の開始・停止（MCU のメモリ・ピン・FPGA の観測が 1 本の時間軸に入る）と、時間軸を開く。
  // 設定の値は親が持つ（覚える・記録の開始で使う）。形式・速さ・チャンネル数は使う人が選ぶ（画面で決めない）
  import Hint from "../Hint.svelte";
  import Req from "../Req.svelte";
  import { type Result } from "../../../lib/api";
  import { i18n } from "../../../lib/i18n";

  let {
    rec, linkOpen, needsRate, pinsStartMissing, withMemory = $bindable(), memoryRateText = $bindable(), withPins = $bindable(),
    pinsFormat = $bindable(), pinsRateText = $bindable(), pinsChannels = $bindable(), onStart, onStop, onTimeline,
  }: {
    rec: Result<"recording.status"> | null; linkOpen: boolean; needsRate: boolean; pinsStartMissing: boolean;
    withMemory: boolean; memoryRateText: string; withPins: boolean; pinsFormat: "" | "rle" | "raw"; pinsRateText: string;
    pinsChannels: string; onStart: () => void; onStop: () => void; onTimeline: () => void;
  } = $props();
</script>

<div class="pane-bar">
  <strong>{$i18n("panel.system")}</strong>
  <Hint text={$i18n("system.tip")} />
  <span class="spacer"></span>
  {#if rec?.recording}
    <span class="badge ok">● {$i18n("system.recording", { n: (rec.events ?? 0).toLocaleString() })}</span>
    <button onclick={onStop}>{$i18n("system.stopRecording")}</button>
  {:else}
    <label class="small" title={$i18n("system.withMemoryTip")}><input type="checkbox" class="check" bind:checked={withMemory} />{$i18n("system.withMemory")}</label>
    {#if withMemory}
      <label class="small" title={$i18n("system.memoryRateTip")}>{$i18n("system.memoryRate")}<Req />
        <input class="rate mono" inputmode="decimal" placeholder={$i18n("system.memoryRatePlaceholder")} aria-required="true"
               bind:value={memoryRateText} /></label>
    {/if}
    <label class="small" title={$i18n("system.withPinsTip")}><input type="checkbox" class="check" bind:checked={withPins} />{$i18n("system.withPins")}</label>
    {#if withPins}
      <label class="small" title={$i18n("timeline.rleTip")}>{$i18n("timeline.rle")}<Req />
        <select bind:value={pinsFormat} aria-required="true">
          <option value=""></option><option value="rle">RLE</option><option value="raw">RAW</option></select></label>
      {#if pinsFormat === "raw"}
        <label class="small" title={$i18n("system.pinsRateTip")}>{$i18n("system.pinsRate")}<Req />
          <input class="rate mono" inputmode="decimal" placeholder={$i18n("system.pinsRatePlaceholder")} aria-required="true"
                 bind:value={pinsRateText} /></label>
        <label class="small" title={$i18n("system.pinsChannelsTip")}>{$i18n("system.pinsChannels")}<Req />
          <select bind:value={pinsChannels} aria-required="true">
            <option value=""></option><option value="16">16</option><option value="8">8</option><option value="4">4</option><option value="2">2</option></select></label>
      {/if}
    {/if}
    <button title={needsRate ? $i18n("system.memoryRateNeeded") : withPins && pinsFormat === "raw" && pinsStartMissing ? $i18n("system.pinsRawNeeded") : $i18n("system.startRecordingTip")}
            disabled={!linkOpen || needsRate || (withPins && pinsStartMissing)}
            onclick={onStart}>{$i18n("system.startRecording")}</button>
  {/if}
  <button title={$i18n("system.timelineTip")} onclick={onTimeline}>{$i18n("system.timeline")}</button>
</div>

<style>
  .badge { font-size: 11px; border: 1px solid currentColor; border-radius: 8px; padding: 0 6px; }
  .ok { color: var(--ok); }
  .small { font-size: 12px; padding: 2px 0; }
  .check { width: auto; min-width: 0; margin-right: 4px; }
  input { width: 100%; min-width: 5em; box-sizing: border-box; }
  input.rate { width: 5em; min-width: 0; }
</style>
