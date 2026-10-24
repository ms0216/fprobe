<script lang="ts">
  // 止める前の警告の条件（debug.halt_risk.set）と走らせたままの見張りの条件（target.liveness.set）の欄。AI が API で
  // 渡せる条件を、人が同じ場所で見て変える（一対一）。欄は API が返す今の条件（debug.halt_risk・target.liveness.get）で
  // 描き、画面は既定の名前・既定の PC の数を持たない。誰か（AI も）が変えたら Safety が読み直し、欄も描き直す
  import { allowed } from "../../lib/guard";
  import { shared } from "../../lib/context";
  import { i18n } from "../../lib/i18n";
  import { load, save } from "../../lib/prefs";
  import {
    haltRiskForm, haltRiskParams, livenessForm, livenessParams, reasonText, type HaltRiskForm, type LivenessForm,
  } from "../../lib/safety";

  const { app, safety } = shared();
  const online = $derived($app.link === "open");
  const risk = $derived($safety.risk);
  const conditions = $derived($safety.haltConditions);       // 今の条件（ELF に依らない）
  const live = $derived($safety.liveness);

  // 欄（編集中）。API の値が変わったときだけ描き直す（ELF の読み直しで条件が同じなら、編集中の欄を消さない）
  let hrForm = $state<HaltRiskForm | null>(null);
  let lvForm = $state<LivenessForm | null>(null);
  let hrKey = "";
  let lvKey = "";
  let lvError = $state<string | null>(null);
  $effect(() => {
    const key = JSON.stringify(conditions);
    if (key !== hrKey) {
      hrKey = key;
      hrForm = haltRiskForm(conditions);
    }
  });
  $effect(() => {
    const key = JSON.stringify(live);
    if (key !== lvKey) {
      lvKey = key;
      lvForm = livenessForm(live);
      lvError = null;
    }
  });

  let open = $state<Record<string, boolean>>(sections());
  function sections(): Record<string, boolean> {
    try {
      return JSON.parse(load("breakpoints.safety", "{}"));
    } catch {
      return {};
    }
  }
  function toggleSection(name: string, e: Event) {
    open = { ...open, [name]: (e.currentTarget as HTMLDetailsElement).open };
    save("breakpoints.safety", JSON.stringify(open));
  }

  async function applyHalt() {
    if (!hrForm || !(await allowed(app))) return;
    if ((await app.act("debug.halt_risk.set", haltRiskParams(hrForm))) !== undefined) await safety.reload();
  }

  async function applyLiveness() {
    if (!lvForm) return;
    const r = livenessParams(lvForm);
    if ("error" in r) {
      lvError = $i18n("lv.badPcSamples");
      return;
    }
    lvError = null;
    if (!(await allowed(app))) return;
    if ((await app.act("target.liveness.set", r.params)) !== undefined) await safety.reloadLiveness();
  }
</script>

<details open={open.halt} ontoggle={(e) => toggleSection("halt", e)}>
  <summary title={$i18n("hr.tip")}>{$i18n("hr.title")}
    {#if conditions}<span class="count">{$i18n("hr.count", { names: conditions.names.length, patterns: conditions.patterns.length })}</span>{/if}</summary>
  {#if hrForm}
    <div class="fields">
      <label for="hr-names">{$i18n("hr.names")}</label>
      <textarea id="hr-names" class="mono" rows="3" spellcheck="false" bind:value={hrForm.names}></textarea>
      <label for="hr-patterns">{$i18n("hr.patterns")}</label>
      <textarea id="hr-patterns" class="mono" rows="3" spellcheck="false" bind:value={hrForm.patterns}></textarea>
      <span></span>
      <span class="row">
        <button class="small primary" disabled={!online} onclick={applyHalt}>{$i18n("cond.apply")}</button>
        <button class="small" onclick={() => (hrForm = haltRiskForm(conditions))}>{$i18n("cond.revert")}</button>
      </span>
    </div>
    <div class="stats mono muted">
      {#if risk}{risk.matches.length ? $i18n("hr.matches", { list: risk.matches.map((m) => reasonText(m, $i18n)).join(" · ") })
                                     : $i18n("hr.noMatch")}{:else}{$i18n("hr.needElf")}{/if}
    </div>
  {:else}
    <div class="empty"><span>{$i18n("hr.needElf")}</span></div>
  {/if}
</details>

<details open={open.liveness} ontoggle={(e) => toggleSection("liveness", e)}>
  <summary title={$i18n("lv.tip")}>{$i18n("lv.title")}
    {#if live}<span class="count">{$i18n("lv.count", { functions: live.functions.length, patterns: live.patterns.length,
                                                      pc_samples: live.pc_samples ?? "-" })}</span>{/if}</summary>
  {#if live && lvForm}
    <div class="fields">
      <label for="lv-functions">{$i18n("lv.functions")}</label>
      <textarea id="lv-functions" class="mono" rows="3" spellcheck="false" bind:value={lvForm.functions}></textarea>
      <label for="lv-patterns">{$i18n("lv.patterns")}</label>
      <textarea id="lv-patterns" class="mono" rows="3" spellcheck="false" bind:value={lvForm.patterns}></textarea>
      <label for="lv-pc">{$i18n("lv.pcSamples")}</label>
      <input id="lv-pc" class="mono pc" bind:value={lvForm.pcSamples} placeholder={$i18n("lv.pcSamplesHint")} />
      <span></span>
      <span class="row">
        <button class="small primary" disabled={!online} onclick={applyLiveness}>{$i18n("cond.apply")}</button>
        <button class="small" onclick={() => { lvForm = livenessForm(live); lvError = null; }}>{$i18n("cond.revert")}</button>
        {#if lvError}<span class="bad">{lvError}</span>{/if}
      </span>
    </div>
  {:else}
    <div class="empty"><span>{$i18n("lv.unavailable")}</span></div>
  {/if}
</details>

<style>
  details { border-top: 1px solid var(--line); }
  summary { padding: 4px 10px; cursor: pointer; color: var(--muted); font-size: 12px; font-weight: 600; }
  .count { font-weight: 400; margin-left: 4px; }
  .fields { display: grid; grid-template-columns: max-content 1fr; gap: 4px 8px; align-items: start; padding: 4px 10px; }
  .fields label { font-size: 12px; color: var(--muted); padding-top: 2px; }
  textarea { resize: vertical; min-height: 3em; }
  input.pc { width: 12ch; }
  .row { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
  .stats { padding: 2px 10px 6px; font-size: 11.5px; }
  .bad { color: var(--bad); }
</style>
