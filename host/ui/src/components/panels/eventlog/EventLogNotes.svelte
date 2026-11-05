<script lang="ts">
  // Event Log の「メモ」: 調査の印（annotation.mark）の一覧と足す欄。表示だけ（読み書きは親）
  import type { Note } from "../../../lib/eventlog";
  import { i18n } from "../../../lib/i18n";
  import type { Reveal } from "../../../lib/timeline/types";
  import TimeLink from "../TimeLink.svelte";

  interface Props {
    notes: Note[];
    noteText: string;
    noteAt: "selection" | "now";
    selection: number | null;
    recording: boolean;
    canNote: boolean;
    at: (t: number) => string;
    reveal: (r: Reveal) => void;
    onadd: () => void;
  }
  let { notes, noteText = $bindable(), noteAt = $bindable(), selection, recording, canNote, at, reveal, onadd }: Props = $props();
</script>

<form class="pane-bar sub" onsubmit={(e) => { e.preventDefault(); onadd(); }}>
  <input class="note" bind:value={noteText} placeholder={$i18n("eventLog.notePlaceholder")} />
  <select bind:value={noteAt} aria-label={$i18n("eventLog.noteAt")}>
    <option value="selection">{selection !== null ? $i18n("eventLog.atSelection", { t: at(selection) })
      : $i18n("eventLog.atSelectionNone")}</option>
    <option value="now" disabled={!recording}>{$i18n("eventLog.atNow")}</option>
  </select>
  <button type="submit" disabled={!canNote || !noteText.trim()}>{$i18n("eventLog.addNote")}</button>
</form>
<div class="rows">
  <table class="grid">
    <thead><tr><th>{$i18n("eventLog.time")}</th><th>{$i18n("eventLog.by")}</th><th>{$i18n("eventLog.note")}</th></tr></thead>
    <tbody>
      {#each notes as n, i (i)}
        <tr ondblclick={() => reveal({ source: "recording", t: n.t })}>
          <td><TimeLink to={{ source: "recording", t: n.t }} /></td>
          <td class="who {n.role ?? ''}">{n.by ?? "—"}</td>
          <td>{n.text}</td>
        </tr>
      {:else}
        <tr><td colspan="3" class="muted">{$i18n("eventLog.noNotes")}</td></tr>
      {/each}
    </tbody>
  </table>
</div>

<style>
  .sub { position: static; }
  .rows { flex: 1; overflow: auto; min-height: 60px; outline: none; }
  tbody tr { cursor: default; }
  .note { flex: 1; min-width: 12em; }
  .who.ai { color: var(--accent); }
  .who.human { color: var(--ok); }
  .who.hook { color: var(--warn); }
</style>
