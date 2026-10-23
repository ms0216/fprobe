<script lang="ts">
  // 安全の帯（ツールバーの下）: 走らせたままの致命的エラー（target.state の fatal。止めずに見張って見つけたもの）と、
  // 止めると壊れうるターゲット（debug.halt_risk）の知らせ。止めずに調べる道具・モニタ方式（Breakpoints のデバッグ方式）へ
  import { shared } from "../../lib/context";
  import { allowed } from "../../lib/guard";
  import { i18n } from "../../lib/i18n";
  import { clueText, fatalTitle, isFatal, linkText, needsWarning, reasonText, type LinkLost } from "../../lib/safety";

  const { app, safety, dock } = shared();
  let hidden = $state<string | null>(null);             // 知らせを閉じた ELF（読み直すとまた出す）

  const fatal = $derived($safety.fatal);
  const risk = $derived($safety.risk);
  const warn = $derived(needsWarning(risk, $app.target) && hidden !== risk?.elf
                        && !!$app.target?.state && $app.target.state !== "DISCONNECTED");
  const since = $derived(fatal ? new Date(fatal.since * 1000).toLocaleTimeString() : "");
  // プローブの USB が切れて開き直した（B28）。ターゲットにつなぎ直すまでデーモンが状態に載せる
  let linkHidden = $state<number | null>(null);         // 閉じた知らせ（at）
  const link = $derived($app.target?.link_lost as LinkLost | undefined);

  async function analyze() {
    if (!(await allowed(app))) return;
    await app.act("target.halt");                        // もう壊れている: 止めて故障の解析へ
    dock.show("fault");
  }
</script>

<div class="safety">
  {#if fatal}
    <div class="bar {isFatal(fatal) ? 'bad' : 'warn'}" role={isFatal(fatal) ? "alert" : "status"}>
      <strong>{isFatal(fatal) ? "⚠ " : ""}{fatalTitle(fatal, $i18n)}</strong>
      <span class="muted">{$i18n("safety.since", { time: since })}</span>
      <span class="clues">{fatal.evidence.map((c) => clueText(c, $i18n)).join(" · ")}</span>
      {#if isFatal(fatal)}<span class="hint">{$i18n("safety.fatalHint")}</span>{/if}
      <span class="spacer"></span>
      <button class="small" onclick={analyze}>{$i18n("safety.analyze")}</button>
    </div>
  {/if}
  {#if link && linkHidden !== link.at}
    <div class="bar warn" role="status">
      <strong>⚠ {linkText(link, $i18n)}</strong>
      <span class="muted">{$i18n("safety.since", { time: new Date(link.at * 1000).toLocaleTimeString() })}</span>
      <span class="spacer"></span>
      <button class="small" aria-label={$i18n("error.dismiss")} title={$i18n("error.dismiss")}
              onclick={() => (linkHidden = link.at)}>×</button>
    </div>
  {/if}
  {#if warn && risk}
    <div class="bar warn" title={risk.matches.map((r) => reasonText(r, $i18n)).join("\n")}>
      <strong>⚠ {$i18n("safety.badge")}</strong>
      <span class="clues">{risk.matches.map((r) => reasonText(r, $i18n)).join(" · ")}</span>
      <span class="hint">{$i18n(risk.monitor_in_elf ? "safety.useMonitor" : "safety.addMonitor")}</span>
      <span class="spacer"></span>
      <button class="small" onclick={() => dock.show("breakpoints")}>{$i18n("safety.debugMode")}</button>
      <button class="small" aria-label={$i18n("error.dismiss")} title={$i18n("error.dismiss")}
              onclick={() => (hidden = risk.elf)}>×</button>
    </div>
  {/if}
</div>

<style>
  .bar { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 10px; padding: 3px 10px; font-size: 12px;
         border-bottom: 1px solid var(--line); }
  .bar.bad { background: color-mix(in srgb, var(--bad) 14%, var(--panel)); color: var(--text); }
  .bar.bad strong { color: var(--bad); }
  .bar.warn { background: color-mix(in srgb, var(--warn) 10%, var(--panel)); }
  .bar.warn strong { color: var(--warn); }
  .clues { font-family: var(--mono, monospace); }
  .hint { color: var(--muted); }
  .muted { color: var(--muted); }
  .spacer { flex: 1; }
</style>
