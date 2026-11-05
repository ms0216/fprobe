<script lang="ts">
  // システム解析: 条件で印・止める（recording.trigger_set）。めったに起きない不具合を前後つきで記録に残す。
  // 状態（trigger）は親が recording.trigger_status で読む
  import Req from "../Req.svelte";
  import { shared } from "../../../lib/context";
  import { i18n } from "../../../lib/i18n";
  import { load, save } from "../../../lib/prefs";
  import type { RecordingTrigger } from "../../../lib/system";

  let { trigger = $bindable() }: { trigger: RecordingTrigger | null } = $props();

  const { app } = shared();

  let triggerExpr = $state(load("system.triggerExpr", "") || "");
  let triggerStop = $state(load("system.triggerStop", "0") === "1");
  // 止めるまでの秒は使う人が入れる（画面の既定を持たない。止めないときは送らない）
  let triggerPost = $state(load("system.triggerPost", "") || "");
  const postS = $derived(triggerPost.trim() === "" ? null : Number(triggerPost));
  const postOk = $derived(postS !== null && Number.isFinite(postS) && postS >= 0);
  $effect(() => {
    save("system.triggerExpr", triggerExpr);
    save("system.triggerStop", triggerStop ? "1" : "0");
    save("system.triggerPost", triggerPost);
  });
  async function armTrigger() {
    if (triggerStop && !postOk) return;
    trigger = (await app.act("recording.trigger_set", { condition: triggerExpr.trim(), action: triggerStop ? "stop" : "mark",
                                                        ...(triggerStop ? { post_s: postS! } : {}) })) ?? trigger;
  }
  async function clearTrigger() {
    await app.act("recording.trigger_clear", {});
    trigger = null;
  }
</script>

<div class="trigger-bar" title={$i18n("system.triggerTip")}>
  <span class="small">{$i18n("system.trigger")}</span>
  <input class="mono" placeholder="errors changed" bind:value={triggerExpr} disabled={trigger?.state === "armed" || trigger?.state === "fired"} />
  <label class="small"><input type="checkbox" class="check" bind:checked={triggerStop} />{$i18n("system.triggerStop")}</label>
  {#if triggerStop}<label class="small">{$i18n("system.triggerPost")}<Req /><input class="post"
    inputmode="decimal" aria-required="true" bind:value={triggerPost} /></label>{/if}
  {#if trigger?.state === "armed" || trigger?.state === "fired"}
    <span class="small" class:ok={trigger.state === "fired"}>{$i18n("system.triggerHits", { n: trigger.hits ?? 0 })}</span>
    <button onclick={clearTrigger}>{$i18n("system.triggerClear")}</button>
  {:else}
    <button disabled={!triggerExpr.trim() || (triggerStop && !postOk)} onclick={armTrigger}>{$i18n("system.triggerArm")}</button>
  {/if}
</div>

<style>
  .ok { color: var(--ok); }
  .small { font-size: 12px; padding: 2px 0; }
  .check { width: auto; min-width: 0; margin-right: 4px; }
  input { width: 100%; min-width: 5em; box-sizing: border-box; }
  .trigger-bar { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 8px; padding: 4px 8px; }
  .trigger-bar input.mono { flex: 1 1 12em; width: auto; }
  .trigger-bar input.post { width: 4em; min-width: 0; }
</style>
