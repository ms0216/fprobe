<script lang="ts">
  // パネルの読みの状態（lib/poll.ts の PollState）: 初めての読みの最中は「読み込み中」、読めなかったら理由と再試行。
  // 前に読めた結果があるときは、それを出したまま上に誤りの帯だけを出す（パネルは loaded まで空の状態を出さない）
  import { i18n } from "../../lib/i18n";
  import type { PollState } from "../../lib/poll";

  let { state, onretry }: { state: PollState; onretry?: () => void } = $props();
</script>

{#if state.error}
  <div class="load-error" role="alert">
    <span>⚠ {$i18n("load.failed", { error: state.error })}</span>
    {#if onretry}<button disabled={state.loading} onclick={onretry}>{$i18n("load.retry")}</button>{/if}
  </div>
{/if}
{#if state.loading && !state.loaded}
  <div class="empty" aria-busy="true"><span class="loading">{$i18n("load.loading")}</span></div>
{/if}

<style>
  .load-error { display: flex; gap: 8px; align-items: center; padding: 4px 8px; font-size: 12px; color: var(--bad);
                border-bottom: 1px solid var(--line); }
  .load-error button { font-size: 12px; }
  .loading::before { content: ""; display: inline-block; width: 10px; height: 10px; margin-right: 6px; border-radius: 50%;
                     border: 2px solid var(--line); border-top-color: var(--accent); vertical-align: -1px;
                     animation: spin 0.8s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
</style>
