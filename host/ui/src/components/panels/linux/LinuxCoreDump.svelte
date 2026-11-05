<script lang="ts">
  // Linux の core dump（linux.core.*）。欄の値は API の引数のまま。呼び出しは親の run
  import { coreFetchParams, coreLimitParams, corePatternParams, coreStatusParams, type CoreForm } from "../../../lib/linux";
  import { i18n } from "../../../lib/i18n";
  import type { LinuxRun } from "./run";

  interface Props {
    target: string;
    core: CoreForm;
    coreResult: Record<string, any> | null;
    run: LinuxRun;
  }
  let { target, core = $bindable(), coreResult = $bindable(), run }: Props = $props();
</script>

<details>
  <summary>core dump</summary>
  <div class="bar">
    <input class="mono short" bind:value={core.pid} placeholder="pid" aria-label="pid" />
    <button class="small" onclick={async () => { coreResult = null; coreResult = (await run("linux.core.status", coreStatusParams(target, core), ["linux.core", core])) ?? null; }}>{$i18n("linux.coreStatus")}</button>
    <input class="mono short" bind:value={core.soft} placeholder="soft" aria-label="soft" title={$i18n("linux.coreLimitHint")} />
    <input class="mono short" bind:value={core.hard} placeholder="hard" aria-label="hard" title={$i18n("linux.coreLimitHint")} />
    <button class="small" onclick={async () => { coreResult = null; coreResult = (await run("linux.core.limit_set", coreLimitParams(target, core), ["linux.core", core])) ?? null; }}>{$i18n("linux.coreLimit")}</button>
  </div>
  <div class="bar">
    <input class="mono" bind:value={core.pattern} placeholder="core_pattern" aria-label="core_pattern" title={$i18n("linux.corePatternHint")} />
    <button class="small" onclick={async () => { coreResult = null; coreResult = (await run("linux.core.pattern_set", corePatternParams(target, core), ["linux.core", core])) ?? null; }}>{$i18n("linux.corePattern")}</button>
  </div>
  <div class="bar">
    <input class="mono" bind:value={core.path} placeholder="path" aria-label="path" />
    <input class="mono" bind:value={core.localPath} placeholder="local_path" aria-label="local_path" title={$i18n("linux.coreLocalHint")} />
    <button class="small" onclick={async () => { coreResult = null; coreResult = (await run("linux.core.fetch", coreFetchParams(target, core), ["linux.core", core])) ?? null; }}>{$i18n("linux.coreFetch")}</button>
  </div>
  {#if coreResult}<pre class="mono small">{JSON.stringify(coreResult, null, 1)}</pre>{/if}
</details>

<style>
  .bar { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
  .short { width: auto; field-sizing: content; min-width: 7em; }
  .small { font-size: 12px; }
  pre { margin: 2px 0; white-space: pre-wrap; }
</style>
