<script lang="ts">
  // 試す: fprobe で「試して確かめる」ことを 1 か所で（docs/superpowers/specs/2026-10-23-ui-rebuild-design.md §3.3 の作る順の
  // 3 番目、docs/reports/2026-10-24-fprobe-value-analysis.md §5.4 の P7・P10）。故障注入（inject.run）・規則（rules.check・
  // rules.states・rules.report）・フック（hooks.*）。どれも API の応答をそのまま描き、判定は API が返したものだけ。
  // 引数の組み立て・応答から描く値は lib/verify.ts
  import { i18n } from "../../lib/i18n";
  import TabBar from "./TabBar.svelte";
  import VerifyHooks from "./VerifyHooks.svelte";
  import VerifyInject from "./VerifyInject.svelte";
  import VerifyRules from "./VerifyRules.svelte";

  type Tab = "inject" | "rules" | "hooks";
  let tab = $state<Tab>("inject");
</script>

<div class="pane verify">
  <div class="pane-bar">
    <TabBar bind:value={tab} ariaLabel={$i18n("panel.verify")} tabs={[
      { id: "inject", label: $i18n("verify.tabInject"), title: "inject.run" },
      { id: "rules", label: $i18n("verify.tabRules"), title: "rules.check · rules.states · rules.report" },
      { id: "hooks", label: $i18n("verify.tabHooks"), title: "hooks.*" },
    ]} />
  </div>
  <div class="body">
    <!-- 隠すだけ（欄の入力・結果をタブの切り替えで失わない） -->
    <div hidden={tab !== "inject"}><VerifyInject /></div>
    <div hidden={tab !== "rules"}><VerifyRules /></div>
    <div hidden={tab !== "hooks"}><VerifyHooks active={tab === "hooks"} /></div>
  </div>
</div>

<style>
  .verify { display: flex; flex-direction: column; min-height: 0; }
  .body { flex: 1; overflow: auto; padding: 6px 8px; }
  /* 3 つのタブ（VerifyInject・VerifyRules・VerifyHooks）に共通 */
  .verify :global(.vsec) { display: flex; flex-direction: column; gap: 6px; margin-bottom: 12px; min-width: 0; }
  .verify :global(h3) { font-size: 12px; color: var(--muted); font-weight: 600; margin: 2px 0 0; display: flex; gap: 8px;
                        align-items: baseline; }
  .verify :global(.bar), .verify :global(.row) { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
  .verify :global(.row label), .verify :global(.bar label) { display: inline-flex; gap: 3px; align-items: center; font-size: 12.5px; }
  .verify :global(.grow) { flex: 1; min-width: 200px; }
  .verify :global(.grow textarea), .verify :global(.grow input) { flex: 1; }
  .verify :global(.wide) { min-width: 280px; flex: 1; }
  .verify :global(textarea) { width: 100%; box-sizing: border-box; font-size: 12px; }
  .verify :global(.card) { border: 1px solid var(--line); border-radius: 6px; padding: 6px 8px; display: flex; flex-direction: column;
                           gap: 4px; }
  .verify :global(.sub) { border-left: 2px solid var(--line); padding-left: 6px; display: flex; flex-direction: column; gap: 4px; }
  .verify :global(.facts) { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; font-size: 12.5px; }
  .verify :global(.cols) { display: flex; gap: 12px; flex-wrap: wrap; align-items: flex-start; }
  .verify :global(.scroll) { overflow-x: auto; }
  .verify :global(.args) { margin: 0; padding: 6px; background: var(--bg); border: 1px solid var(--line); border-radius: 4px;
                           font-size: 11.5px; max-height: 260px; overflow: auto; white-space: pre-wrap; }
  .verify :global(.small) { font-size: 11.5px; }
  .verify :global(.ast) { color: var(--bad); margin-left: 1px; }
  .verify :global(.err) { color: var(--bad); font-size: 11.5px; }
  .verify :global(.bad) { color: var(--bad); }
  .verify :global(.verdict.ok) { color: var(--ok); }
  .verify :global(.verdict.bad) { color: var(--bad); }
  .verify :global(.verdict.other) { color: var(--warn); }
  .verify :global(tr.off) { opacity: 0.55; }
  .verify :global(.inner) { margin-top: 4px; }
  .verify :global(.nw) { white-space: nowrap; }
  .verify :global(.gap) { margin-left: 6px; }
</style>
