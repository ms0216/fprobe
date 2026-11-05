<script lang="ts">
  // Linux の gdb: バッチ実行と gdbserver の起動・停止。欄の値は API の引数のまま。呼び出しは親の run
  import { gdbBatchParams, gdbServeParams, type GdbForm } from "../../../lib/linux";
  import { i18n } from "../../../lib/i18n";
  import type { LinuxRun } from "./run";

  interface Props {
    target: string;
    gdb: GdbForm;
    gdbResult: Record<string, any> | null;
    gdbStatus: Record<string, any> | null;
    serveInfo: Record<string, any> | null;
    run: LinuxRun;
    fmt: (v: unknown) => string;
  }
  let { target, gdb = $bindable(), gdbResult = $bindable(), gdbStatus, serveInfo = $bindable(), run, fmt }: Props = $props();
</script>

<details>
  <summary>gdb</summary>
  <textarea class="mono small" rows="3" bind:value={gdb.commands} placeholder={$i18n("linux.gdbCommands")} aria-label={$i18n("linux.gdbCommands")}></textarea>
  <div class="bar">
    <input class="mono short" bind:value={gdb.pid} placeholder="pid" aria-label="pid" />
    <input class="mono short" bind:value={gdb.core} placeholder="core" aria-label="core" />
    <input class="mono short" bind:value={gdb.binary} placeholder="binary" aria-label="binary" />
    <input class="mono num" bind:value={gdb.timeout} placeholder="s" aria-label="timeout_s" title="timeout_s" />
    <button class="small" onclick={async () => { gdbResult = null; gdbResult = (await run("linux.gdb.batch", gdbBatchParams(target, gdb), ["linux.gdb", gdb])) ?? null; }}>{$i18n("linux.gdbRun")}</button>
    <input class="mono num" bind:value={gdb.port} placeholder="port" aria-label="port" />
    <button class="small" onclick={async () => { serveInfo = null; serveInfo = (await run("linux.gdb.serve", gdbServeParams(target, gdb), ["linux.gdb", gdb])) ?? null; }}>{$i18n("linux.gdbServe")}</button>
    <button class="small" onclick={async () => { if ((await run("linux.gdb.stop", { params: { target } })) !== undefined) serveInfo = null; }}>{$i18n("linux.gdbStop")}</button>
  </div>
  {#if serveInfo}<div class="mono small">{serveInfo.connect}</div>{/if}
  {#if gdbStatus}<div class="mono small">{JSON.stringify(gdbStatus)}</div>{/if}
  {#if gdbResult}
    <div class="mono small">exit_code={fmt(gdbResult.exit_code)} timed_out={fmt(gdbResult.timed_out)}</div>
    <pre class="mono small">{gdbResult.stdout}</pre>
    {#if gdbResult.stderr}<pre class="mono small bad">{gdbResult.stderr}</pre>{/if}
  {/if}
</details>

<style>
  .bar { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
  .short { width: auto; field-sizing: content; min-width: 7em; }
  .num { width: auto; field-sizing: content; min-width: 4em; }
  .small { font-size: 12px; }
  pre { margin: 2px 0; white-space: pre-wrap; }
  textarea { width: 100%; box-sizing: border-box; }
  .bad { color: var(--bad); }
</style>
