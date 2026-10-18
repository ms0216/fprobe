<script lang="ts">
  // Fault Analyzer（STM32CubeIDE）・Fault exception viewer（IAR）: 故障で止まったときの原因（CFSR・HFSR の立っている
  // ビットの意味と手がかり）、故障のアドレス（MMFAR・BFAR → Memory）、故障した命令（積まれた PC → Source・Disassembly）、
  // スタックされたレジスタ、呼び出し履歴（各フレーム → Source）。故障で止まると App が自動で開く（lib/fault.ts の
  // watchFaults）。故障していなければその旨と「故障で中断する」（ベクタ キャッチ）
  import { errorText } from "../../lib/api";
  import { FAULT_CATCH, VECTOR_CATCH, catchState, changesCatch, type VectorCatchState } from "../../lib/breakpoints";
  import { shared } from "../../lib/context";
  import { STACKED, VALID_BITS, causeRegister, headline, isFault, reportText, type FaultReport } from "../../lib/fault";
  import { allowed } from "../../lib/guard";
  import { i18n } from "../../lib/i18n";
  import Empty from "./Empty.svelte";
  import { hex32 } from "./format";

  const { app, conn, dbg, showAddress, showMemory } = shared();
  let report = $state<FaultReport | null>(null);
  let failed = $state<string | null>(null);
  let vc = $state<VectorCatchState | null>(null);
  let copied = $state(false);

  const online = $derived($app.link === "open");
  const connected = $derived(!!$app.target?.state && $app.target.state !== "DISCONNECTED");
  const halted = $derived($app.target?.state === "HALTED");
  const key = $derived(halted ? `${$app.target?.pc}/${$app.target?.run}` : "");
  const faulted = $derived(isFault(report));
  const title = $derived(report ? headline(report) : null);
  const label = (name: string) => VECTOR_CATCH.find((v) => v.name === name)?.label ?? name;
  const faultCatch = $derived((vc?.enabled ?? []).filter((n) => FAULT_CATCH.includes(n)));

  // 止まるたびに読む（止まり直したら読み直す）
  $effect(() => {
    if (key) void read();
  });
  // 接続したら・誰か（AI も）がベクタ キャッチを変えたら読み直す
  $effect(() => {
    if (online && connected) void readCatch();
  });
  $effect(() => conn.on("activity", (e: { method: string; params?: unknown }) => {
    if (changesCatch(e) && connected) void readCatch();
  }));

  async function read() {
    try {
      report = await conn.call("target.fault", {}, { quiet: true });
      failed = null;
    } catch (e) {
      report = null;
      failed = errorText(e);
    }
  }

  async function readCatch() {
    try {
      vc = catchState(await conn.call("target.vector_catch.get", {}, { quiet: true }), vc ?? undefined);
    } catch {
      vc = null;
    }
  }

  async function haltOnFaults() {
    if (!(await allowed(app))) return;
    const r = await app.act("target.vector_catch", { catch: [...new Set([...(vc?.enabled ?? []), ...FAULT_CATCH])] as any });
    if (r) vc = catchState(r, vc ?? undefined);
  }

  async function copy() {
    if (!report) return;
    await navigator.clipboard?.writeText(reportText(report));
    copied = true;
    setTimeout(() => (copied = false), 1500);
  }
</script>

<div class="pane" class:stale={!halted && report}>
  {#if report}
    <div class="pane-bar">
      <strong class:bad={faulted}>{title ?? $i18n("fault.none")}</strong>
      <span class="spacer"></span>
      <button class="small" title={$i18n("fault.copyTip")} onclick={copy}>{copied ? $i18n("fault.copied") : $i18n("fault.copy")}</button>
      <button class="small" disabled={!halted} onclick={read}>{$i18n("common.refresh")}</button>
    </div>
    {#if faulted}
      <section>
        <h3>{$i18n("fault.cause")}</h3>
        <table class="grid">
          <thead><tr><th>{$i18n("fault.register")}</th><th>{$i18n("fault.bit")}</th><th>{$i18n("fault.description")}</th></tr></thead>
          <tbody>
            {#each report.causes as c (c.bit)}
              <tr class:minor={VALID_BITS.has(c.bit)}>
                <td class="mono muted">{causeRegister(c.bit)}</td>
                <td class="mono bit">{c.bit}</td>
                <td>{c.meaning}{#if c.hint}<div class="muted hint">{c.hint}</div>{/if}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </section>
      <section>
        <dl class="kv">
          {#if report.exception}
            <dt>{$i18n("fault.exception")}</dt>
            <dd class="mono">{report.exception.name} <span class="muted">#{report.exception.number}</span></dd>
          {/if}
          {#if report.frame}
            <dt title={$i18n("fault.locationTip")}>{$i18n("fault.location")}</dt>
            <dd class="mono"><button class="link" onclick={() => showAddress(report!.frame!.pc.address)}>
              {report.frame.pc.where ?? hex32(report.frame.pc.address)}</button>
              <span class="muted">{hex32(report.frame.pc.address)}</span></dd>
            <dt>{$i18n("fault.caller")}</dt>
            <dd class="mono"><button class="link" onclick={() => showAddress(report!.frame!.lr.address)}>
              {report.frame.lr.where ?? hex32(report.frame.lr.address)}</button>
              <span class="muted">{hex32(report.frame.lr.address)}</span></dd>
          {/if}
          {#if report.fault_address}
            <dt title={$i18n("fault.addressTip")}>{$i18n("fault.address")}</dt>
            <dd class="mono"><button class="link" onclick={() => showMemory(report!.fault_address!.address)}>
              {hex32(report.fault_address.address)}</button>
              {#if report.fault_address.where && !report.fault_address.where.startsWith("0x")}
                <span class="muted">{report.fault_address.where}</span>{/if}</dd>
          {/if}
        </dl>
      </section>
      <section>
        <h3 title={$i18n("fault.stackedTip")}>{$i18n("fault.stacked")}</h3>
        {#if report.frame}
          <div class="muted small-text mono">
            {$i18n("fault.stackInfo", { stack: report.frame.stack, sp: hex32(report.frame.sp), exc: hex32(report.frame.exc_return) })}
            {#if report.frame.fp_context} · {$i18n("fault.fpContext")}{/if}
          </div>
          <div class="regs mono">
            {#each STACKED as r (r)}
              <span class="muted">{r}</span><span>{hex32(report.frame.registers[r])}</span>
            {/each}
          </div>
        {:else}
          <div class="muted small-text">{$i18n("fault.noFrame")}</div>
        {/if}
      </section>
      {#if $dbg.backtrace.length}
        <section>
          <h3>{$i18n("panel.callStack")}</h3>
          <ol class="frames mono">
            {#each $dbg.backtrace as f, i (i)}
              <li>
                <button class="link" onclick={() => showAddress(f.pc)}>{f.where}</button>
                {#if f.note === "exception"}<span class="note">{$i18n("callStack.exception")}</span>{/if}
                <span class="muted">{hex32(f.pc)}</span>
              </li>
            {/each}
          </ol>
        </section>
      {/if}
    {:else}
      <div class="empty">
        <span>{$i18n("fault.noneHint")}</span>
        {#if faultCatch.length}
          <span>{$i18n("fault.catchOn", { names: faultCatch.map(label).join(", ") })}</span>
        {/if}
        <div class="actions">
          <button disabled={!connected || faultCatch.length >= FAULT_CATCH.length - (vc?.unsupported.length ?? 0)}
                  title={$i18n("fault.enableCatchTip")} onclick={haltOnFaults}>{$i18n("fault.enableCatch")}</button>
        </div>
      </div>
    {/if}
    {#if report.registers || report.halt_reasons?.length}
      <section>
        <h3>{$i18n("fault.registers")}</h3>
        {#if report.registers}
          <div class="regs mono">
            {#each Object.entries(report.registers) as [name, value] (name)}
              <span class="muted">{name}</span><span>{value == null ? "-" : hex32(value)}</span>
            {/each}
          </div>
        {/if}
        <dl class="kv">
          {#if report.halt_reasons?.length}
            <dt>{$i18n("fault.haltReason")}</dt>
            <dd class="mono">{report.halt_reasons.map((h) => h.bit).join(", ")}</dd>
          {/if}
          <dt>{$i18n("fault.architecture")}</dt>
          <dd>{report.architecture}</dd>
        </dl>
      </section>
    {/if}
  {:else if failed && halted}
    <div class="empty"><strong>{$i18n("fault.notAvailable")}</strong><span class="mono">{failed}</span></div>
  {:else}
    <Empty need="halted" />
  {/if}
</div>

<style>
  section { padding: 6px 10px 8px; border-bottom: 1px solid var(--line); }
  h3 { font-size: 12px; font-weight: 600; color: var(--muted); margin: 0 0 4px; }
  .bad { color: var(--bad); }
  .bit { font-weight: 600; white-space: nowrap; }
  tr.minor td { opacity: 0.7; }
  .hint { font-size: 12px; }
  .small-text { font-size: 12px; margin-bottom: 4px; }
  .kv { display: grid; grid-template-columns: max-content 1fr; gap: 3px 12px; margin: 0; align-items: baseline; }
  .kv dt { color: var(--muted); font-size: 12px; }
  .kv dd { margin: 0; display: flex; gap: 8px; flex-wrap: wrap; align-items: baseline; }
  .regs { display: grid; grid-template-columns: repeat(auto-fill, 48px 96px); gap: 2px 6px; }
  .frames { list-style: none; margin: 0; padding: 0; }
  .frames li { display: flex; gap: 8px; align-items: center; padding: 1px 0; }
  .note { font-size: 11px; color: var(--warn); border: 1px solid currentColor; border-radius: 4px; padding: 0 4px; }
</style>
