<script lang="ts">
  // ステータスバー（下）: デーモンとの接続・プローブ・ターゲットの状態・インターフェイスの速度・CPU、右に AI の作業と
  // 一時停止（fprobe 独自: 人と AI が同じプローブを使う調停）
  import { shared } from "../../lib/context";
  import { frequency } from "../../lib/format";
  import { haltReasonKey, linkOf } from "../../lib/target";
  import { i18n, type Key } from "../../lib/i18n";
  import { measuredVolts } from "../../lib/power";
  import { isFatal } from "../../lib/safety";

  const { app, ui } = shared();

  const probe = $derived($app.probe?.capabilities?.board
    ? `${$app.probe.capabilities.board}${$app.device ? ` · ${$app.device.link.toUpperCase()} ${$app.device.address ?? ""}` : ""}`
    : $app.device ? `${$app.device.link.toUpperCase()} ${$app.device.address ?? ""}` : "—");
  const targetState = $derived($app.target?.state as string | undefined);
  const stateKey = $derived<Key>(targetState === "RUNNING" || targetState === "SLEEPING" || targetState === "HALTED"
    || targetState === "DISCONNECTED" ? `status.target.${targetState}` : "status.target.unknown");
  // 線（SWD / JTAG）・速度・ターゲット名: ターゲットの状態から（デーモンがつないでいる先を毎回出す。操作の記録は
  // 上限で古い方が落ち、ほかの利用者の接続も混じるので頼らない）
  const link = $derived(linkOf($app.target));
  const haltKey = $derived(haltReasonKey($app.target));
  const working = $derived($app.clients.some((c) => c.role === "ai"));
</script>

<footer class="status" class:halted={targetState === "HALTED"} class:running={targetState === "RUNNING" || targetState === "SLEEPING"}>
  <span class="item link {$app.link}">● {$i18n(`status.daemon.${$app.link}`)}</span>
  <span class="item" title={$i18n("status.probe")}>{probe}</span>
  {#if $app.voltage}
    {@const volts = measuredVolts($app.voltage as any)}
    <span class="item" title={volts === null ? $i18n("power.notLiveTip") : ""}>{$i18n("status.voltage")}
      {volts === null ? $i18n("power.notLive") : `${volts.toFixed(2)} V`}</span>
  {/if}
  <span class="item state">{$i18n(stateKey)}{haltKey ? ` · ${$i18n(haltKey)}` : ""}{targetState === "HALTED" && $app.target?.function ? ` · ${$app.target.function}()` : ""}
    {#if $app.target?.fatal}
      {#if isFatal($app.target.fatal)}<span class="fatal">· {$i18n("status.target.fatal")}</span>
      {:else}<span class="tick">· {$i18n("status.target.tickStopped")}</span>{/if}
    {/if}</span>
  {#if link?.speed}<span class="item">{link.wire} {frequency(link.speed)}</span>{/if}
  {#if link?.cpu}<span class="item">{link.cpu}</span>{/if}
  <span class="spacer"></span>
  <span class="item" title={$app.clients.map((c) => `${c.name} (${c.role})`).join("\n")}>{$i18n("status.clients", { n: $app.clients.length })}</span>
  {#if $app.control.paused}
    <span class="item paused">{$i18n("status.ai.paused", { who: $app.control.paused_by ?? "" })}</span>
  {:else}
    <span class="item">{working ? $i18n("status.ai.working") : $i18n("status.ai.idle")}</span>
  {/if}
  <span class="item held" class:some={$app.control.held > 0} title={$i18n("status.heldTip")}>{$i18n("status.held", { n: $app.control.held })}</span>
  {#if $app.control.paused}
    <button class="item btn" disabled={$app.link !== "open"} onclick={() => app.act("session.control.resume")}>{$i18n("status.ai.resume")}</button>
  {:else}
    <button class="item btn" disabled={$app.link !== "open"} title={$i18n("status.ai.pauseTip")}
            onclick={() => app.act("session.control.pause")}>{$i18n("status.ai.pause")}</button>
  {/if}
  <button class="item btn" title={$i18n("status.lang")} onclick={() => ui.open("settings")}>⚙</button>
</footer>

<style>
  .fatal { color: var(--bad); font-weight: 600; }
  .tick { color: var(--warn); }
  .status { display: flex; align-items: center; gap: 2px; height: 24px; padding: 0 6px; font-size: 12px;
            background: var(--accent); color: #fff; }
  .status.running { background: color-mix(in srgb, var(--ok) 80%, #000); }
  .status.halted { background: color-mix(in srgb, var(--warn) 85%, #000); }
  .item { padding: 0 8px; white-space: nowrap; }
  .spacer { flex: 1; }
  .link.closed { color: #ffd0d0; }
  .paused { font-weight: 600; }
  .held.some { font-weight: 700; }
  .btn { border: none; background: rgba(255, 255, 255, 0.15); color: #fff; border-radius: 3px; padding: 0 8px; height: 18px;
         font-size: 12px; }
  .btn:hover:not(:disabled) { background: rgba(255, 255, 255, 0.3); border: none; }
</style>
