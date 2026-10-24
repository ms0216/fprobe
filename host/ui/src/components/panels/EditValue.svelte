<script lang="ts">
  // 見えている値をその場で書く欄（仕様 §3.6）: 書けるときは点線の下線と、どの API で書くかのツールチップを出し、
  // ダブルクリックで入力にする（Enter で書く・Esc でやめる）。書けないときは理由をツールチップに出す。
  // 書く・確かめる（AI の作業中）・引数を組み立てるのは呼ぶ側（onwrite。lib/valuewrite）。onwrite は誤りの文を返す
  import { i18n } from "../../lib/i18n";

  let { text, initial = text, via = null, note = "", reason = "", onwrite }:
    { text: string; initial?: string; via?: string | null; note?: string; reason?: string;
      onwrite: (input: string) => Promise<string | null> } = $props();

  let editing = $state<string | null>(null);
  let error = $state<string | null>(null);

  const tip = $derived(error ?? (via ? [$i18n("write.tip", { api: via }), note].filter(Boolean).join("\n")
                                     : [reason, note].filter(Boolean).join("\n")));

  function start(e: MouseEvent) {
    if (!via) return;
    e.stopPropagation();
    editing = initial;
    error = null;
  }

  async function commit() {
    if (editing === null) return;
    const input = editing;
    editing = null;
    if (input.trim() === "" || input === initial) return;
    error = await onwrite(input);
  }

  function onKey(e: KeyboardEvent) {
    e.stopPropagation();
    if (e.key === "Enter") {
      e.preventDefault();
      void commit();
    } else if (e.key === "Escape") {
      e.preventDefault();
      editing = null;
    }
  }

  const focus = (el: HTMLInputElement) => {
    el.focus();
    el.select();
  };
</script>

{#if editing !== null}
  <input class="mono edit" aria-label={$i18n("vars.edit")} bind:value={editing} use:focus onkeydown={onKey}
         onclick={(e) => e.stopPropagation()} ondblclick={(e) => e.stopPropagation()} onblur={() => (editing = null)} />
{:else}
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <span class="value" class:can-edit={!!via} class:bad={!!error} title={tip} ondblclick={start}>{text}</span>
{/if}

<style>
  .value { white-space: nowrap; }
  .can-edit { cursor: text; text-decoration: underline dotted color-mix(in srgb, var(--accent) 70%, transparent);
              text-underline-offset: 3px; }
  .bad { color: var(--bad); }
  .edit { width: 100%; min-width: 6em; padding: 0 4px; font: inherit; }
</style>
