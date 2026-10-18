<script lang="ts">
  // 画面の中の確認の窓（lib/confirm.ts の問い）。ほかのダイアログの上にも出る（showModal の最上層）。
  // Esc・枠の外のクリックは「キャンセル」。取り返しのつかない操作では最初の焦点を「キャンセル」に置く
  import { tick } from "svelte";
  import { confirmer } from "../../lib/confirm";
  import { i18n } from "../../lib/i18n";

  let dialog = $state<HTMLDialogElement>();
  let okButton = $state<HTMLButtonElement>();
  let cancelButton = $state<HTMLButtonElement>();
  const request = confirmer;

  $effect(() => {
    const r = $request;
    if (!dialog) return;
    if (r && !dialog.open) dialog.showModal();
    if (!r && dialog.open) dialog.close();
    if (r) void tick().then(() => (r.danger ? cancelButton : okButton)?.focus());
  });

  function answer(ok: boolean) {
    if ($request) confirmer.answer(ok);
  }
</script>

<dialog bind:this={dialog} class="confirm" aria-labelledby="confirm-message"
        oncancel={(e) => { e.preventDefault(); answer(false); }}
        onclick={(e) => e.target === dialog && answer(false)}>
  {#if $request}
    <p id="confirm-message">{$request.message}</p>
    <div class="row">
      <button bind:this={cancelButton} onclick={() => answer(false)}>{$i18n("common.cancel")}</button>
      <button bind:this={okButton} class="primary" class:danger={$request.danger} onclick={() => answer(true)}>
        {$request.ok ?? $i18n("common.ok")}</button>
    </div>
  {/if}
</dialog>

<style>
  .confirm { border: 1px solid var(--line); border-radius: 10px; background: var(--panel); color: var(--text);
             padding: 16px 20px; min-width: 360px; max-width: min(560px, 92vw); box-shadow: 0 12px 40px rgba(0, 0, 0, 0.25); }
  .confirm::backdrop { background: rgba(0, 0, 0, 0.3); }
  p { margin: 0 0 16px; white-space: pre-line; line-height: 1.5; }
  .row { display: flex; gap: 8px; justify-content: flex-end; }
  .danger { background: var(--bad); border-color: var(--bad); }
</style>
