<script lang="ts">
  // 窓の「時刻に連動」の切り替え（lib/codeat.ts）。既定は連動、窓ごとに外せる（その人のブラウザに覚える）。
  // 連動している間だけ、時間軸の選択カーソルで recording.code_at を呼ぶ側（CodeAtLink）を使う
  import { untrack } from "svelte";
  import { isLinked, setLinked, type LinkedPanel } from "../../lib/codeat";
  import { shared } from "../../lib/context";
  import { i18n } from "../../lib/i18n";

  let { panel, linked = $bindable(true) }: { panel: LinkedPanel; linked?: boolean } = $props();
  const { codeAt } = shared();

  linked = untrack(() => isLinked(panel));                 // 窓の種類は替わらない（最初の値で覚えた設定を読む）
  $effect(() => setLinked(panel, linked));
  $effect(() => (linked ? codeAt.use() : undefined));
</script>

<label class="codeat-toggle" title={$i18n("codeAt.linkTip")}>
  <input type="checkbox" bind:checked={linked} /> {$i18n("codeAt.link")}
</label>

<style>
  .codeat-toggle { white-space: nowrap; }
</style>
