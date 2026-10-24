<script lang="ts">
  // Fault Analyzer（STM32CubeIDE）・Fault exception viewer（IAR）: 故障で止まったときの原因（CFSR・HFSR の立っている
  // ビットの意味と手がかり）、故障のアドレス（MMFAR・BFAR → Memory）、故障した命令（積まれた PC → Source・Disassembly）、
  // スタックされたレジスタ、呼び出し履歴（各フレーム → Source）。故障で止まると App が自動で開く（lib/fault.ts の
  // watchFaults）。故障していなければその旨と「故障で中断する」（ベクタ キャッチ）。
  // 故障のレジスタと積まれたレジスタは、値のダブルクリックで target.memory.write で書ける（番地と大きさは target.fault の
  // locations の事実。1 を書いて消すレジスタは write_one_to_clear）
  import { errorText } from "../../lib/api";
  import { FAULT_CATCH, VECTOR_CATCH, catchState, changesCatch, type VectorCatchState } from "../../lib/breakpoints";
  import { shared } from "../../lib/context";
  import { STACKED, headline, isFault, reportText, type FaultReport } from "../../lib/fault";
  import { allowed } from "../../lib/guard";
  import { configParam, decodeParams, markersAt, parseCount, readHistory, trailingMarkers, type Cut, type EtmConfigForm, type EtmHistory, type EtmSourceForm } from "../../lib/etm";
  import { load, save } from "../../lib/prefs";
  import { locationOf, memoryWriteCall, writeBuilt, writeOneToClear, type Location } from "../../lib/valuewrite";
  import EditValue from "./EditValue.svelte";
  import { i18n } from "../../lib/i18n";
  import Empty from "./Empty.svelte";
  import { hex32 } from "./format";

  const { app, conn, dbg, showAddress, showMemory } = shared();
  let report = $state<FaultReport | null>(null);
  let failed = $state<string | null>(null);
  let vc = $state<VectorCatchState | null>(null);
  let copied = $state(false);
  let savedTo = $state<string | null>(null);             // 「状態を保存」の保存先（Recordings に一覧）
  // 直前の命令（ETM）: トレースのバイトの置き場所はその人のブラウザに覚える（ボード・ファームウェアで決まる）
  let etmForm = $state<EtmSourceForm>(restoreEtm());
  let etmConfig = $state<EtmConfigForm>({ commitOpt1: "", vmidBytes: "", cidBytes: "" });   // 空 = trace.etm.start の ID レジスタ
  let etmCount = $state(load("fault.etmCount", ""));                // 読む命令の数は使う人が入れる（画面の既定を持たない）
  let etmHistory = $state<EtmHistory | null>(null);
  let etmError = $state<string | null>(null);
  let etmDecode = $state<{ errors: string[]; cut: Cut | null; note?: string } | null>(null);   // 解読の誤り・注記（decode の結果）
  let etmBusy = $state(false);

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

  /** 番地に書いて、止まっていれば読み直す */
  async function writeAt(at: Location, input: string): Promise<string | null> {
    const error = await writeBuilt(app, memoryWriteCall(at, input), $i18n);
    if (!error && halted) await read();
    return error;
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

  /** いまの状態（レジスタ・RAM・故障の解析）をファイルに保存する（snapshot.save。一覧と開くは Recordings） */
  async function saveState() {
    if (!(await allowed(app))) return;
    const out = await app.act("snapshot.save", {});
    savedTo = out?.path ?? null;
  }

  function restoreEtm(): EtmSourceForm {
    try {
      const v = JSON.parse(load("fault.etmSource", ""));
      if (v?.kind === "file" || v?.kind === "memory") return v;
    } catch { /* 既定 */ }
    return { kind: "memory", address: "", length: "", oldest: "" };
  }

  async function decodeEtm() {
    const built = decodeParams(etmForm);
    if ("error" in built) {
      etmError = $i18n("fault.etmBadField", { field: built.error });
      return;
    }
    const config = configParam(etmConfig);
    if ("error" in config) {
      etmError = $i18n("fault.etmBadField", { field: config.error });
      return;
    }
    const count = parseCount(etmCount);
    if (count === null) {
      etmError = $i18n("fault.etmBadField", { field: "count" });
      return;
    }
    if (!(await allowed(app))) return;
    save("fault.etmSource", JSON.stringify(etmForm));
    save("fault.etmCount", etmCount);
    etmBusy = true;
    const r = await readHistory(conn, { ...built.params, ...config.params }, count);
    etmBusy = false;
    etmHistory = r.history;
    etmError = r.error;
    etmDecode = r.history ? { errors: r.decodeErrors ?? [], cut: r.decodeErrorsCut ?? null, note: r.decodeNote } : null;
  }
</script>

<div class="pane" class:stale={!halted && report}>
  {#if report}
    <div class="pane-bar">
      <strong class:bad={faulted}>{title ?? $i18n("fault.none")}</strong>
      <span class="spacer"></span>
      {#if savedTo}<span class="muted small mono" title={savedTo}>{$i18n("snapshots.saved")}</span>{/if}
      <button class="small" disabled={!halted} title={$i18n("snapshots.saveTip")} onclick={() => void saveState()}>{$i18n("snapshots.save")}</button>
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
              <tr>
                <td class="mono muted">{c.register ?? ""}</td>
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
              {#if report.fault_address.where}<span class="muted">{report.fault_address.where}</span>{/if}</dd>
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
              {@const at = locationOf(report.frame, r)}
              <span class="muted">{r}</span><span><EditValue text={hex32(report.frame.registers[r])}
                via={at && connected ? "target.memory.write" : null}
                note={at ? $i18n("write.at", { address: hex32(at.address), size: at.size }) : ""}
                reason={at ? "" : $i18n("write.noLocation")} onwrite={(input) => writeAt(at!, input)} /></span>
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
                {#if f.note === "lr_without_cfi"}<span class="note">{$i18n("callStack.lrWithoutCfi")}</span>{/if}
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
    <details class="etm">
      <summary>{$i18n("fault.etm")}</summary>
      <form class="etm-form" onsubmit={(e) => { e.preventDefault(); void decodeEtm(); }} title={$i18n("fault.etmTip")}>
        <select value={etmForm.kind} aria-label={$i18n("fault.etmSource")}
                onchange={(e) => (etmForm = e.currentTarget.value === "file" ? { kind: "file", path: "" }
                                  : { kind: "memory", address: "", length: "", oldest: "" })}>
          <option value="memory">{$i18n("fault.etmMemory")}</option>
          <option value="file">{$i18n("fault.etmFile")}</option>
        </select>
        {#if etmForm.kind === "memory"}
          <input class="mono" bind:value={etmForm.address} placeholder={$i18n("fault.etmAddress")} aria-label={$i18n("fault.etmAddress")}
                 aria-required="true" /><span class="req" aria-hidden="true">*</span>
          <input class="mono num" bind:value={etmForm.length} placeholder={$i18n("fault.etmLength")} aria-label={$i18n("fault.etmLength")}
                 aria-required="true" /><span class="req" aria-hidden="true">*</span>
          <input class="mono num" bind:value={etmForm.oldest} placeholder={$i18n("fault.etmOldest")} aria-label={$i18n("fault.etmOldest")}
                 aria-required="true" /><span class="req" aria-hidden="true">*</span>
        {:else}
          <input class="mono" bind:value={etmForm.path} placeholder={$i18n("fault.etmPath")} aria-label={$i18n("fault.etmPath")}
                 aria-required="true" /><span class="req" aria-hidden="true">*</span>
        {/if}
        <select bind:value={etmConfig.commitOpt1} aria-label="commit_opt1" title={$i18n("fault.etmConfigTip")}>
          <option value="">{$i18n("fault.etmConfigFromStart")}</option>
          <option value="1">COMMOPT = 1</option>
          <option value="0">COMMOPT = 0</option>
        </select>
        <input class="mono num" bind:value={etmConfig.vmidBytes} placeholder="vmid_bytes" aria-label="vmid_bytes" title={$i18n("fault.etmConfigTip")} />
        <input class="mono num" bind:value={etmConfig.cidBytes} placeholder="cid_bytes" aria-label="cid_bytes" title={$i18n("fault.etmConfigTip")} />
        <label class="muted">{$i18n("fault.etmCount")}<span class="req" aria-hidden="true">*</span>
          <input class="mono num" bind:value={etmCount} inputmode="numeric" aria-required="true" /></label>
        <button class="small" disabled={etmBusy}>{$i18n("fault.etmDecode")}</button>
      </form>
      {#if etmError}<div class="bad small-text">{etmError}</div>{/if}
      {#if etmHistory}
        <div class="muted small-text">{$i18n("fault.etmTotal", { shown: etmHistory.instructions.length, total: etmHistory.total })}
          {#if etmHistory.next_address != null} · {$i18n("fault.etmNext")} <span class="mono">{hex32(etmHistory.next_address)}</span>{/if}</div>
        <table class="etm-rows mono">
          <tbody>
            {#each etmHistory.instructions as row (row.index)}
              {#each markersAt(etmHistory.markers, row.index) as m, k (k)}
                <tr class="marker"><td colspan="5">{m.kind}</td></tr>
              {/each}
              <tr>
                <td><button class="link" onclick={() => showAddress(row.address)}>{hex32(row.address)}</button></td>
                <td>{row.mnemonic} {row.operands ?? ""}</td>
                <td>{row.function ?? ""}{row.offset != null && row.function ? `+${row.offset}` : ""}</td>
                <td class="muted">{row.file ? `${row.file}:${row.line ?? ""}` : ""}</td>
                <td class="muted">{(row as any).branch ?? ""}</td>
              </tr>
            {/each}
            {#each trailingMarkers(etmHistory) as m, k (k)}
              <tr class="marker"><td colspan="5">{m.kind}</td></tr>
            {/each}
          </tbody>
        </table>
        {#each etmHistory.errors as err, k (k)}<div class="bad small-text">{err}</div>{/each}
        {#each etmDecode?.errors ?? [] as err, k (k)}<div class="bad small-text">{err}</div>{/each}
        {#if etmDecode?.cut}<div class="muted small-text">{$i18n("common.truncated", { ...etmDecode.cut })}</div>{/if}
        {#if etmDecode?.note}<div class="muted small-text">{etmDecode.note}</div>{/if}
      {/if}
    </details>
    {#if report.registers || report.halt_reasons?.length}
      <section>
        <h3>{$i18n("fault.registers")}</h3>
        {#if report.registers}
          <div class="regs mono">
            {#each Object.entries(report.registers) as [name, value] (name)}
              {@const at = locationOf(report, name)}
              {@const w1c = writeOneToClear(report, name)}
              <span class="muted" title={w1c ? $i18n("fault.w1cTip") : undefined}>{name}{#if w1c}<sup class="w1c">w1c</sup>{/if}</span>
              <span><EditValue text={value == null ? "-" : hex32(value)} initial={value == null ? "" : hex32(value)}
                via={at && connected ? "target.memory.write" : null}
                note={[at ? $i18n("write.at", { address: hex32(at.address), size: at.size }) : "", w1c ? $i18n("fault.w1cTip") : ""]
                  .filter(Boolean).join("\n")}
                reason={at ? "" : $i18n("write.noLocation")} onwrite={(input) => writeAt(at!, input)} /></span>
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
  .etm { padding: 6px 10px 8px; border-bottom: 1px solid var(--line); }
  .etm summary { font-size: 12px; font-weight: 600; color: var(--muted); cursor: pointer; }
  .etm-form { display: flex; gap: 6px; flex-wrap: wrap; align-items: center; margin: 6px 0; }
  .etm-form .num { width: 6em; }
  .etm-rows { border-collapse: collapse; font-size: 12px; }
  .etm-rows td { padding: 1px 10px 1px 0; white-space: nowrap; }
  .etm-rows tr.marker td { color: var(--warn); font-weight: 600; }
  section { padding: 6px 10px 8px; border-bottom: 1px solid var(--line); }
  h3 { font-size: 12px; font-weight: 600; color: var(--muted); margin: 0 0 4px; }
  .bad { color: var(--bad); }
  .bit { font-weight: 600; white-space: nowrap; }
  .hint { font-size: 12px; }
  .small-text { font-size: 12px; margin-bottom: 4px; }
  .kv { display: grid; grid-template-columns: max-content 1fr; gap: 3px 12px; margin: 0; align-items: baseline; }
  .kv dt { color: var(--muted); font-size: 12px; }
  .kv dd { margin: 0; display: flex; gap: 8px; flex-wrap: wrap; align-items: baseline; }
  .regs { display: grid; grid-template-columns: repeat(auto-fill, 48px 96px); gap: 2px 6px; }
  .frames { list-style: none; margin: 0; padding: 0; }
  .frames li { display: flex; gap: 8px; align-items: center; padding: 1px 0; }
  .w1c { font-size: 9px; color: var(--muted); margin-left: 2px; }
  .note { font-size: 11px; color: var(--warn); border: 1px solid currentColor; border-radius: 4px; padding: 0 4px; }
</style>
