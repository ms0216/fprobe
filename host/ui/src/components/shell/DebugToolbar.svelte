<script lang="ts">
  // デバッグのツールバー（Ozone・Keil・VS Code に共通の並び）: Open / Download・Reset / Continue・Halt /
  // Step Over・Into・Out・Instruction・Run to Cursor / Toggle Breakpoint / Connect・Disconnect。ツールチップに名前とショートカット
  import Icon from "../Icon.svelte";
  import { command, isRunning, tooltip, type CommandState } from "../../lib/commands";
  import { shared } from "../../lib/context";
  import { i18n } from "../../lib/i18n";

  let { cs }: { cs: CommandState } = $props();
  const ctx = shared();
  const { app, dbg } = ctx;

  const GROUPS = [["openElf", "loadDemo"], ["download", "reset"], ["continue", "halt"],
                  ["stepOver", "stepInto", "stepOut", "stepInstruction", "runToCursor"], ["toggleBreakpoint"], ["connect", "disconnect"]];
  const STEP_LABEL = { over: "cmd.stepOver", into: "cmd.stepInto", out: "cmd.stepOut" } as const;
  const connected = $derived(cs.target !== undefined && cs.target !== "DISCONNECTED");

  function run(id: string) {
    const c = command(id);
    if (!c.blocked(cs)) void c.run(ctx, cs);
  }
</script>

<div class="toolbar" role="toolbar" aria-label={$i18n("menu.debug")}>
  {#each GROUPS as group, g (g)}
    <div class="group">
      {#each group as id (id)}
        {@const c = command(id)}
        {#if !(id === "connect" && connected) && !(id === "disconnect" && !connected)
             && !(id === "continue" && isRunning(cs.target)) && !(id === "halt" && !isRunning(cs.target))}
          <button class="tool {id}" disabled={c.blocked(cs) !== null} title={tooltip(c, cs, $i18n)}
                  aria-label={$i18n(c.label)} onclick={() => run(id)}>
            <Icon name={c.icon!} />
            {#if id === "connect" || id === "openElf"}<span class="label">{$i18n(c.label)}</span>{/if}
          </button>
        {/if}
      {/each}
    </div>
  {/each}
  <span class="spacer"></span>
  {#if $dbg.lastStep}
    {@const step = $dbg.lastStep}
    {@const mode = $i18n(STEP_LABEL[step.mode])}
    {@const s = step.elapsedS.toString()}
    <span class="last mono" title={$i18n("toolbar.lastStepTip")}>
      {step.steps === null || step.runs === null ? $i18n("toolbar.lastStepTimeOnly", { mode, s })
        : $i18n("toolbar.lastStep", { mode, steps: step.steps, runs: step.runs, s })}</span>
  {/if}
  {#if $app.target?.linux}
    {@const lx = $app.target.linux}
    <span class="where mono" title={lx.elf}>{lx.comm} pid {lx.pid}{lx.tid !== null ? ` · tid ${lx.tid}` : ""}{lx.thread_comm && lx.thread_comm !== lx.comm ? ` (${lx.thread_comm})` : ""}</span>
  {/if}
  {#if $app.target?.state === "HALTED" && $app.target?.where}
    <span class="where mono" title={$app.target.where}>{$app.target.where}</span>
  {/if}
</div>

<style>
  .toolbar { display: flex; align-items: center; gap: 4px; padding: 3px 8px; background: var(--panel);
             border-bottom: 1px solid var(--line); min-height: 34px; }
  .group { display: flex; gap: 2px; padding-right: 6px; margin-right: 2px; border-right: 1px solid var(--line); }
  .group:last-of-type { border-right: none; }
  .tool { display: flex; align-items: center; gap: 5px; border: 1px solid transparent; background: none; padding: 4px 6px;
          border-radius: 4px; color: var(--text); }
  .tool:hover:not(:disabled) { background: var(--select); border-color: transparent; }
  .tool:disabled { opacity: 0.35; }
  .continue, .runToCursor { color: var(--ok); }
  .halt, .disconnect { color: var(--bad); }
  .stepOver, .stepInto, .stepOut, .stepInstruction { color: var(--accent); }
  .toggleBreakpoint { color: var(--bad); }
  .label { font-size: 12.5px; }
  .spacer { flex: 1; }
  .last { font-size: 12px; color: var(--accent); margin-right: 8px; white-space: nowrap; }
  .where { font-size: 12px; color: var(--muted); max-width: 40%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
</style>
