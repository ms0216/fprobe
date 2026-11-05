<script lang="ts">
  // Logic Analyzer（Saleae Logic 2 の Analyzers・DSView のトリガ・PulseView の測定。G09）:
  // - Decode: 解読器（la.decoders）の選択と設定 → la.decode の表と、時間軸の波形の上の注釈（panels/logic/LaDecode）
  // - Trigger: 多段トリガ・番地一致・つなぎ先と状態、段の図（panels/logic/LaTrigger）
  // - Measure: 時間軸の基準と選択のカーソルの間（無ければ全体）の測定（panels/logic/LaMeasure）
  // 3 つの部品は、見ていないタブでも作ったまま隠す（タブを替えても入力・結果・注釈が残る）。捕捉の保存はここ。計算は lib/logic.ts
  import { shared } from "../../lib/context";
  import { allowed } from "../../lib/guard";
  import { i18n } from "../../lib/i18n";
  import { laExportParams, type LaExportFormat } from "../../lib/logic";
  import { load, save } from "../../lib/prefs";
  import { laChannelCount } from "../../lib/waveform";
  import LaDecode from "./logic/LaDecode.svelte";
  import LaMeasure from "./logic/LaMeasure.svelte";
  import LaTrigger from "./logic/LaTrigger.svelte";
  import TabBar from "./TabBar.svelte";

  type Tab = "decode" | "trigger" | "measure";

  const { app } = shared();
  let tab = $state<Tab>(load("logicAnalyzer.tab", "decode") as Tab);
  const linked = $derived($app.link === "open");          // つながってから読む（つなぎ直したら読み直す）
  // ch の数はデバイスの申告だけ（届くまで null: 条件の表は空、覚えた条件の幅も変えない）
  const declaredChannels = $derived(laChannelCount($app.probe?.capabilities));
  $effect(() => save("logicAnalyzer.tab", tab));

  // 捕捉の保存（la.export。PulseView の .sr・VCD。パスはデーモンの PC）
  let exportFormat = $state<LaExportFormat>(load("logicAnalyzer.exportFormat", "sr") as LaExportFormat);
  let exportPath = $state<string | null>(null);            // null = 形式の既定の名前
  let exported = $state<string | null>(null);

  async function exportCapture() {
    if (!(await allowed(app))) return;
    save("logicAnalyzer.exportFormat", exportFormat);
    const out = await app.act("la.export", laExportParams(exportFormat, exportPath));
    exported = out === undefined ? null : out.path ?? null;
  }
</script>

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

  <div class="tab" class:off={tab !== "decode"}><LaDecode {linked} /></div>
  <div class="tab" class:off={tab !== "trigger"}><LaTrigger active={tab === "trigger"} {linked} {declaredChannels} /></div>
  <div class="tab" class:off={tab !== "measure"}><LaMeasure active={tab === "measure"} {linked} /></div>
</div>

<style>
  .export { display: flex; gap: 6px; align-items: center; }
  .export input { width: 14em; }
  .la { display: flex; flex-direction: column; }
  .tab { display: contents; }
  .tab.off { display: none; }
</style>
