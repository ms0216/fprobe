<script lang="ts">
  // 呼び出し履歴・ローカル変数の窓の「時刻に連動」（recording.code_at）: 記録には止まったときの呼び出し履歴・変数の
  // 値が残らないので、時間軸で選んだ時刻については、記録にある事実（その時刻に止まっていたならその場所、そうでなければ
  // 直前・直後の PC のサンプルの場所）と、値が記録に無いことを示す。ターゲットの今の値（下の表）とは分けて出す
  import { haltedAt, sampleText, signedDuration } from "../../lib/codeat";
  import { shared } from "../../lib/context";
  import { i18n } from "../../lib/i18n";
  import { formatDuration, timeText } from "../../lib/time";
  import CodeAtToggle from "./CodeAtToggle.svelte";

  let { panel }: { panel: "callStack" | "locals" } = $props();
  const { tl, codeAt } = shared();
  const origins = tl.origins;

  let linked = $state(true);
  const atTime = $derived(linked && $tl.source === "recording" ? $codeAt : null);
  const at = $derived(atTime?.result ?? null);
  const halt = $derived(haltedAt(at));
  const where = $derived(halt ? `${halt.where ?? (halt.pc !== undefined ? $i18n("codeAt.pc", { pc: `0x${(halt.pc >>> 0).toString(16)}` }) : "—")}`
                                + (halt.halt_reason ? ` ${halt.halt_reason}` : "") : "");
</script>

{#if $tl.source === "recording"}
  <div class="codeat-note">
    <div class="head">
      <CodeAtToggle {panel} bind:linked />
      {#if atTime && atTime.t !== null}
        <span class="muted">{$i18n("codeAt.at", { time: timeText($origins, { source: "recording", t: atTime.t }) ?? `t = ${atTime.t}` })}</span>
        {#if atTime.loading}<span class="muted">{$i18n("codeAt.loading")}</span>{/if}
      {/if}
    </div>
    {#if atTime && atTime.t !== null}
      {#if atTime.error}
        <p class="bad">{atTime.error}</p>
      {:else if at}
        {#if halt}
          <p title={$i18n("codeAt.haltedTip")}>
            {$i18n(panel === "callStack" ? "codeAt.haltedNoStack" : "codeAt.haltedNoLocals", { where })}
            <span class="muted mono">{signedDuration(halt.dt_s, (x) => formatDuration(x))}</span>
          </p>
        {:else}
          <p class="muted">{$i18n(panel === "callStack" ? "codeAt.noStack" : "codeAt.noLocals")}</p>
        {/if}
        {#if panel === "callStack"}
          <ul class="samples">
            {#each [["before", at.pc_before], ["after", at.pc_after]] as const as [kind, s] (kind)}
              <li title={$i18n(kind === "before" ? "codeAt.beforeTip" : "codeAt.afterTip")}>
                <span class="muted">{$i18n(kind === "before" ? "codeAt.before" : "codeAt.after")}</span>
                {#if s}<span class="mono">{sampleText($i18n, s)}</span>
                  <span class="muted mono">{signedDuration(s.dt_s, (x) => formatDuration(x))}</span>
                {:else}<span class="muted">{$i18n("codeAt.none")}</span>{/if}
              </li>
            {/each}
          </ul>
        {/if}
      {/if}
    {/if}
  </div>
{/if}

<style>
  .codeat-note { border-bottom: 1px solid var(--line); padding: 4px 8px; font-size: 12px; display: flex;
                 flex-direction: column; gap: 2px; }
  .head { display: flex; gap: 10px; align-items: baseline; flex-wrap: wrap; }
  p { margin: 0; }
  .bad { color: var(--bad); }
  .samples { list-style: none; margin: 0; padding: 0; }
  .samples li { display: flex; gap: 6px; align-items: baseline; }
</style>
