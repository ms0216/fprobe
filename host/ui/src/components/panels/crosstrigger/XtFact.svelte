<script lang="ts">
  // クロストリガーの事実 1 つ: 時刻（時間軸へのリンク。時刻 0 もタイムベースの事実の時刻）か回数。
  // 機器から読み戻せないもの（UNREADABLE）は「読み戻せない」とだけ示す（推さない）。
  import { UNREADABLE, type Fact } from "../../../lib/crosstrigger";
  import { i18n } from "../../../lib/i18n";
  import TimeLink from "../TimeLink.svelte";

  let { kind, value }: { kind: "time" | "count"; value: Fact } = $props();
</script>

{#if value === UNREADABLE}<span class="muted small" title={$i18n("xt.unreadableTip")}>{$i18n("xt.unreadable")}</span>
{:else if kind === "count"}<span class="mono">{value ?? "—"}</span>
{:else if value !== null}<TimeLink to={{ source: "recording", t: value }} />{:else}<span class="muted">—</span>{/if}

<style>
  .small { font-size: 11.5px; }
</style>
