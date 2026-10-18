<script lang="ts">
  // 記録の出来事の条件の欄（Event Log の検索・区間の統計の開始と終了）: stream・kind・a / b の比較・body の欄の値か
  // 正規表現。recording.find の query になる（lib/eventlog.ts の toQuery）
  import { COMPARES, COMPARE_TEXT, STREAMS, type Condition } from "../../lib/eventlog";
  import { i18n } from "../../lib/i18n";

  let { cond = $bindable(), streams = [] }: { cond: Condition; streams?: string[] } = $props();
  const choices = $derived([...new Set([...STREAMS, ...streams])].sort());
</script>

<span class="cond">
  <select bind:value={cond.stream} aria-label={$i18n("eventLog.stream")} title={$i18n("eventLog.stream")}>
    <option value="">{$i18n("eventLog.anyStream")}</option>
    {#each choices as s (s)}<option value={s}>{s}</option>{/each}
  </select>
  <input class="kind mono" bind:value={cond.kind} placeholder={$i18n("eventLog.kind")} title={$i18n("eventLog.kindTip")} />
  <span class="num">
    <span class="muted">a</span>
    <select bind:value={cond.aOp} aria-label="a">{#each COMPARES as c (c)}<option value={c}>{COMPARE_TEXT[c]}</option>{/each}</select>
    <input class="mono v" bind:value={cond.a} placeholder="—" title={$i18n("eventLog.valueTip")} />
  </span>
  <span class="num">
    <span class="muted">b</span>
    <select bind:value={cond.bOp} aria-label="b">{#each COMPARES as c (c)}<option value={c}>{COMPARE_TEXT[c]}</option>{/each}</select>
    <input class="mono v" bind:value={cond.b} placeholder="—" title={$i18n("eventLog.valueTip")} />
  </span>
  <span class="num">
    <input class="mono field" bind:value={cond.field} placeholder={$i18n("eventLog.field")} title={$i18n("eventLog.fieldTip")} />
    <select bind:value={cond.regex} aria-label={$i18n("eventLog.match")}>
      <option value={true}>~</option><option value={false}>=</option>
    </select>
    <input class="mono body" bind:value={cond.value} placeholder={cond.regex ? $i18n("eventLog.regex") : $i18n("eventLog.value")} />
  </span>
</span>

<style>
  .cond { display: inline-flex; gap: 6px; flex-wrap: wrap; align-items: center; }
  .num { display: inline-flex; gap: 2px; align-items: center; }
  .kind { width: 9em; }
  .v { width: 7em; }
  .field { width: 6em; }
  .body { width: 10em; }
</style>
