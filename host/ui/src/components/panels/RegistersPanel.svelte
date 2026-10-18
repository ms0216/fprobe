<script lang="ts">
  // Registers: コアのレジスタ（止まったときに読む）。前に止まったときから変わった値を赤く。値のダブルクリックで書く
  // （10 進・0x・0b。Enter で書き、Tab で書いて次へ、Esc でやめる。止まっているときだけ）
  import { tick } from "svelte";
  import { shared } from "../../lib/context";
  import { allowed } from "../../lib/guard";
  import { i18n } from "../../lib/i18n";
  import { parseField } from "../../lib/peripherals";
  import Empty from "./Empty.svelte";

  const { app, dbg } = shared();
  let previous: Record<string, number> | null = null;
  let changed = $state(new Set<string>());
  let editing = $state<{ name: string; text: string } | null>(null);
  let error = $state<string | null>(null);

  $effect(() => {
    const now = $dbg.registers;
    if (!now) return;
    changed = new Set(previous ? Object.keys(now).filter((k) => previous![k] !== now[k]) : []);
    previous = now;
  });

  const halted = $derived(!!$dbg.stop);
  const names = $derived(Object.keys($dbg.registers ?? {}));
  const hex = (n: number) => `0x${(n >>> 0).toString(16).toUpperCase().padStart(8, "0")}`;

  function start(name: string) {
    if (!halted) return;
    error = null;
    editing = { name, text: hex($dbg.registers![name]) };
  }

  async function commit(next: boolean) {
    if (!editing) return;
    const { name, text } = editing;
    const value = parseField(text, 32);
    if (value === null) {
      error = $i18n("registers.badValue", { value: text });
      return;
    }
    const i = names.indexOf(name);
    editing = next && i + 1 < names.length ? { name: names[i + 1], text: hex($dbg.registers![names[i + 1]]) } : null;
    const old = $dbg.registers?.[name];
    if ((old === undefined || value !== old >>> 0) && (await allowed(app))) await dbg.writeRegister(name, value);
  }

  function onKey(e: KeyboardEvent) {
    if (e.key === "Enter" || e.key === "Tab") {
      e.preventDefault();
      void commit(e.key === "Tab");
    } else if (e.key === "Escape") {
      e.preventDefault();
      editing = null;
    }
  }

  const focus = (el: HTMLInputElement) => {
    void tick().then(() => {
      el.focus();
      el.select();
    });
  };
</script>

<div class="pane" class:stale={$dbg.stale}>
  {#if $dbg.registers}
    {#if error}<div class="pane-bar"><span class="bad">{error}</span></div>{/if}
    <table class="grid mono">
      <thead><tr><th>{$i18n("common.name")}</th><th>{$i18n("common.value")}</th><th></th></tr></thead>
      <tbody>
        {#each Object.entries($dbg.registers) as [name, value] (name)}
          <tr class:changed={changed.has(name)}><td>{name.toUpperCase()}</td>
            {#if editing?.name === name}
              <td><input class="cell" aria-label={$i18n("registers.edit")} bind:value={editing.text} use:focus
                         onkeydown={onKey} onblur={() => editing?.name === name && (editing = null)} /></td>
            {:else}
              <!-- svelte-ignore a11y_no_static_element_interactions -->
              <td class="v" title={halted ? $i18n("registers.editTip") : $i18n("tip.notHalted")}
                  ondblclick={() => start(name)}>{hex(value)}</td>
            {/if}
            <td class="muted">{value >>> 0}</td></tr>
        {/each}
      </tbody>
    </table>
  {:else}
    <Empty need="halted" />
  {/if}
</div>

<style>
  .v { cursor: text; }
  .cell { width: 13ch; padding: 0 2px; font: inherit; }
  .bad { color: var(--bad); font-size: 12px; }
</style>
