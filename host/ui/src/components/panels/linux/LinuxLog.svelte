<script lang="ts">
  // Linux の ftrace と journal（linux.ftrace.* / linux.log.read）。欄の値は API の引数のまま。呼び出しは親の run
  import { ftraceParams, logParams, type LogForm } from "../../../lib/linux";
  import { i18n } from "../../../lib/i18n";
  import type { LinuxRun } from "./run";

  interface Props {
    target: string;
    ftrace: string;
    log: LogForm;
    ftraceResult: Record<string, any> | null;
    logResult: Record<string, any> | null;
    run: LinuxRun;
    fmt: (v: unknown) => string;
  }
  let { target, ftrace = $bindable(), log = $bindable(), ftraceResult = $bindable(), logResult = $bindable(), run, fmt }: Props = $props();
</script>

<details>
  <summary>ftrace / journal</summary>
  <div class="bar">
    <input class="mono" bind:value={ftrace} placeholder="sched/sched_switch" aria-label="events" />
    <button class="small" onclick={async () => { ftraceResult = null; ftraceResult = (await run("linux.ftrace.enable", ftraceParams(target, ftrace), ["linux.ftrace", ftrace])) ?? null; }}>{$i18n("linux.ftraceOn")}</button>
    <button class="small" onclick={async () => { ftraceResult = null; ftraceResult = (await run("linux.ftrace.disable", ftraceParams(target, ftrace))) ?? null; }}>{$i18n("linux.ftraceOff")}</button>
  </div>
  {#if ftraceResult}<div class="mono small">{JSON.stringify(ftraceResult)}</div>{/if}
  <div class="bar">
    <input class="mono num" bind:value={log.lines} placeholder="lines" aria-label="lines" />
    <input class="mono short" bind:value={log.unit} placeholder="unit" aria-label="unit" oninput={() => { log.cursor = ""; }} />
    <input class="mono short" bind:value={log.cursor} placeholder="since_cursor" aria-label="since_cursor" />
    <button class="small" onclick={async () => { logResult = null; logResult = (await run("linux.log.read", logParams(target, log), ["linux.log", { ...log, cursor: "" }])) ?? null; }}>{$i18n("linux.logRead")}</button>
    {#if logResult?.next_cursor}<button class="small" onclick={() => { log.cursor = logResult!.next_cursor; }}>{$i18n("linux.logNext")}</button>{/if}
  </div>
  {#if logResult}
    <div class="mono small">{logResult.clock} exit_code={fmt(logResult.exit_code)} bad_lines={fmt(logResult.bad_lines)}</div>
    <div class="rows mono small">
      {#each logResult.entries as e, i (i)}<div>{e.__MONOTONIC_TIMESTAMP} {e._SYSTEMD_UNIT ?? e.SYSLOG_IDENTIFIER ?? ""} {e.MESSAGE}</div>{/each}
    </div>
    {#if logResult.stderr}<pre class="mono small bad">{logResult.stderr}</pre>{/if}
  {/if}
</details>

<style>
  .bar { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
  .short { width: auto; field-sizing: content; min-width: 7em; }
  .num { width: auto; field-sizing: content; min-width: 4em; }
  .small { font-size: 12px; }
  pre { margin: 2px 0; white-space: pre-wrap; }
  .rows { overflow: auto; }
  .bad { color: var(--bad); }
</style>
