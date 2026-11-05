<script lang="ts">
  // クロストリガーの行き先「出力の端子」: 端子ごとの状態・方式・条件の一覧と、有効・無効・解放の操作（xtrig.output_*）。
  // 読みと操作の呼び出しは親。表示だけ
  import type { Avail, OutputPin } from "../../../lib/crosstrigger";
  import { i18n } from "../../../lib/i18n";
  import { capText, hex32 } from "../../../lib/xtlabels";

  interface Props {
    avail: Avail;
    outputs: { outputs: OutputPin[]; actions: number; free: number } | null;
    readError: string | undefined;
    onpress: (method: string, params: Record<string, unknown>) => void;
    onopen: () => void;
  }
  let { avail, outputs, readError, onpress, onopen }: Props = $props();
</script>

<div class="card" class:gone={!avail.ok}>
  <h4>{$i18n("xt.actOutputs")} <span class="mono small muted">xtrig.output_*</span>
    {#if outputs}<span class="muted small">{$i18n("lowLevel.outputsFree", { free: outputs.free, actions: outputs.actions })}</span>{/if}</h4>
  {#if !avail.ok}<div class="reason small">{$i18n("xt.absent", { cap: capText(avail) })}</div>{:else}
    {#if outputs}
      <table class="grid">
        <thead><tr><th>ch</th><th>{$i18n("xt.state")}</th><th>mode</th><th>level</th><th>{$i18n("xt.condition")}</th><th></th></tr></thead>
        <tbody>
          {#each outputs.outputs as o (o.channel)}
            <tr>
              <td class="mono">ch{o.channel}{#if o.pull_up}<span class="muted small gap" title={$i18n("xt.pullUpTip")}>pull-up</span>{/if}</td>
              <td>{!o.configured ? $i18n("xt.outFree") : o.active ? $i18n("xt.outActive") : o.enabled ? $i18n("xt.outEnabled")
                   : $i18n("xt.outDisabled")}</td>
              <td class="mono">{[o.mode ?? "", o.pulse_us ? `${o.pulse_us.min.toPrecision(3)}–${o.pulse_us.max.toPrecision(3)} µs` : ""].join(" ")}</td>
              <td class="mono">{o.level ?? ""}</td>
              <td class="mono small">{#if o.configured}{(o.sources ?? []).join(" | ")}{#if o.pattern?.mask}
                {(o.sources ?? []).length ? " | " : ""}pins {hex32(o.pattern.value)}/{hex32(o.pattern.mask)}{o.pattern.any ? " any" : ""}{/if}{/if}</td>
              <td class="nw">{#if o.configured}
                <button class="small" onclick={() => onpress(o.enabled ? "xtrig.output_disable" : "xtrig.output_enable", { channel: o.channel })}>
                  {o.enabled ? $i18n("lowLevel.disable") : $i18n("lowLevel.enable")}</button>
                <button class="small" title={$i18n("lowLevel.releaseTip")}
                        onclick={() => onpress("xtrig.output_release", { channel: o.channel })}>{$i18n("lowLevel.release")}</button>
              {/if}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    {:else if readError}<div class="error">{readError}</div>{/if}
    <div class="bar"><button class="small" onclick={onopen}>{$i18n("xt.openLowLevel")}</button></div>
  {/if}
</div>

<style>
  h4 { font-size: 12.5px; font-weight: 600; margin: 0; display: flex; gap: 6px; align-items: baseline; flex-wrap: wrap; }
  .card { border: 1px solid var(--line); border-radius: 6px; padding: 6px 8px; display: flex; flex-direction: column; gap: 4px; }
  .gone { opacity: 0.55; }
  .reason { color: var(--muted); font-style: italic; }
  .bar { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
  .small { font-size: 11.5px; }
  .error { color: var(--bad); font-size: 12px; }
  .nw { white-space: nowrap; }
  .gap { margin-left: 4px; }
</style>
