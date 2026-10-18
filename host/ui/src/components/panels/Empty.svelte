<script lang="ts">
  // パネルの空の状態: 何が要るかと、その操作のボタン（ELF が無い → Open ELF / Load Demo、未接続 → Connect、
  // 走行中 → Halt）
  import { command, tooltip, type CommandState } from "../../lib/commands";
  import { shared } from "../../lib/context";
  import { i18n, type Key } from "../../lib/i18n";

  let { need = "halted" }: { need?: "halted" | "connected" | "elf" } = $props();
  const ctx = shared();
  const { app, dbg, ui } = ctx;

  const cs = $derived<CommandState>({ link: $app.link, target: $app.target?.state, elf: $dbg.elf,
                                         selection: $ui.selection });
  const missing = $derived.by(() => {
    if (need === "elf" && !$dbg.files.length) return "elf";
    if (!cs.target || cs.target === "DISCONNECTED") return "connected";
    if (need === "halted" && cs.target !== "HALTED") return "halted";
    return null;
  });
  const actions = $derived(missing === "elf" ? ["openElf", "loadDemo"] : missing === "connected" ? ["connect"]
                           : missing === "halted" ? ["halt"] : []);
  const hint = $derived<Key>(missing === "elf" ? "hint.openElfFirst" : missing === "connected" ? "hint.connectFirst"
                             : "hint.haltFirst");

  function run(id: string) {
    const c = command(id);
    if (!c.blocked(cs)) void c.run(ctx, cs);
  }
</script>

{#if missing}
  <div class="empty">
    <span>{$i18n(hint)}</span>
    <div class="actions">
      {#each actions as id (id)}
        {@const c = command(id)}
        <button disabled={c.blocked(cs) !== null} title={tooltip(c, cs, $i18n)} onclick={() => run(id)}>
          {$i18n(c.label)}</button>
      {/each}
    </div>
  </div>
{/if}
