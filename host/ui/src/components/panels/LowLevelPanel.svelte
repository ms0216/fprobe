<script lang="ts">
  // 低レベル（J-Link Commander の ReadDP / WriteAP・ピン・GPIO に当たる）: ほかの窓に出ていない書く操作を、読んだ今の値の
  // 横で書く（仕様 docs/superpowers/specs/2026-10-23-ui-rebuild-design.md §3.6）。DP・AP のレジスタ（target.dp.* /
  // target.ap.*）、プローブの CSR（probe.csr.*）、デバッグコネクタのピン（probe.pins.*）、拡張ヘッダの GPIO（gpio.*）、
  // クロストリガの出力の端子（xtrig.output_*）。値は API で読んだものだけを描き、値のダブルクリック・押すことで書く。
  // DP・AP・ピンは未接続のときに読むと線を動かす（DP を起こす）ので、読むのは「読む」を押したときだけ。
  // 書く操作は AI の作業中なら確かめる（lib/valuewrite の writeBuilt）。引数の組み立ては lib/lowlevel
  import { errorText } from "../../lib/api";
  import { shared } from "../../lib/context";
  import { i18n } from "../../lib/i18n";
  import {
    apRegisterParams, apWriteCall, bitsOf, csrReadParams, csrWriteCall, DP_REGISTERS, dpWriteCall, gpioSetCall, optionsOf,
    outputForm, outputSetCall, PINS, pinsWriteCall, type OutputForm, type OutputStatus, type Pin,
  } from "../../lib/lowlevel";
  import { performWrite, writeBuilt, type ApiCall } from "../../lib/valuewrite";
  import EditValue from "./EditValue.svelte";
  import { hex32 } from "./format";

  const { app, conn } = shared();
  const online = $derived($app.link === "open");
  const connected = $derived(!!$app.target?.state && $app.target.state !== "DISCONNECTED");

  type Cell = { value?: number; error?: string };
  const call = (method: string, params: Record<string, unknown> = {}) => conn.call(method as any, params as any, { quiet: true });
  async function cell(method: string, params: Record<string, unknown>): Promise<Cell> {
    try {
      return { value: (await call(method, params))?.value };
    } catch (e) {
      return { error: errorText(e) };
    }
  }
  const fieldError = (e: string) => $i18n(`write.error.${e}` as "write.error.value");

  // --- DP ---
  let dp = $state<Record<string, Cell>>({});
  let dpPath = $state("");
  async function readDp() {
    const next: Record<string, Cell> = {};
    for (const r of DP_REGISTERS) {
      if (!r.read) continue;
      try {
        const out = await call("target.dp.read", { register: r.name });
        next[r.name] = { value: out?.value };
        dpPath = out?.path ?? dpPath;
      } catch (e) {
        next[r.name] = { error: errorText(e) };
      }
    }
    dp = next;
  }
  async function writeDp(name: string, input: string): Promise<string | null> {
    const failed = await writeBuilt(app, dpWriteCall(name, input), $i18n);
    if (!failed && Object.keys(dp).length) await readDp();
    return failed;
  }

  // --- AP ---
  // 読む AP・レジスタ・CSR の番地と数は使う人が入れる（画面の既定を持たない）
  let apText = $state("");
  let apRegText = $state("");
  let apError = $state<string | null>(null);
  let aps = $state<{ ap: number; register: number; cell: Cell }[]>([]);
  async function readAp(e?: Event) {
    e?.preventDefault();
    const p = apRegisterParams(apText, apRegText);
    if ("error" in p) {
      apError = fieldError(p.error);
      return;
    }
    apError = null;
    await reloadAp(p.ap, p.register);
  }
  async function reloadAp(ap: number, register: number) {
    const c = await cell("target.ap.read", { ap, register });
    aps = [...aps.filter((r) => r.ap !== ap || r.register !== register), { ap, register, cell: c }];
  }
  async function writeAp(ap: number, register: number, input: string): Promise<string | null> {
    const failed = await writeBuilt(app, apWriteCall(ap, register, input), $i18n);
    if (!failed) await reloadAp(ap, register);
    return failed;
  }

  // --- CSR ---
  let csrAddress = $state("");
  let csrCount = $state("");
  let csrError = $state<string | null>(null);
  let csr = $state<{ address: number; words: number[] } | null>(null);
  async function readCsr(e?: Event) {
    e?.preventDefault();
    const p = csrReadParams(csrAddress, csrCount);
    if ("error" in p) {
      csrError = fieldError(p.error);
      return;
    }
    await loadCsr(p.address, p.count);
  }
  async function loadCsr(address: number, count: number) {
    try {
      const out = await call("probe.csr.read", { address, count });
      csr = { address, words: Array.isArray(out) ? out : [] };          // probe.csr.read は語の列を返す
      csrError = null;
    } catch (err) {
      csrError = errorText(err);
    }
  }
  async function writeCsr(address: number, input: string): Promise<string | null> {
    const failed = await writeBuilt(app, csrWriteCall(address, input), $i18n);
    if (!failed && csr) await loadCsr(csr.address, csr.words.length);
    return failed;
  }

  // --- ピン ---
  let pins = $state<Record<string, number> | null>(null);
  let pinsError = $state<string | null>(null);
  async function readPins() {
    try {
      pins = await call("probe.pins.read");
      pinsError = null;
    } catch (e) {
      pinsError = errorText(e);
    }
  }

  // --- GPIO ---
  let gpio = $state<{ drive: number; enable: number; level: number } | null>(null);
  let gpioError = $state<string | null>(null);
  async function readGpio() {
    try {
      gpio = await call("gpio.read");
      gpioError = null;
    } catch (e) {
      gpioError = errorText(e);
    }
  }

  /** 押して書く操作（ピン・GPIO・出力の端子）: 書いた後の状態を描き直す */
  async function press(c: ApiCall, after: () => Promise<void>) {
    if ((await performWrite(app, c)) !== undefined) await after();
  }

  // --- 出力の端子 ---
  let outputs = $state<{ outputs: OutputStatus[]; actions?: number; free?: number } | null>(null);
  let outputsError = $state<string | null>(null);
  let forms = $state<Record<number, OutputForm>>({});
  let formError = $state<Record<number, string>>({});
  async function readOutputs() {
    try {
      outputs = await call("xtrig.output_status");
      outputsError = null;
      forms = Object.fromEntries((outputs?.outputs ?? []).map((o) => [o.channel, forms[o.channel] ?? outputForm(o)]));
    } catch (e) {
      outputsError = errorText(e);
    }
  }
  async function setOutput(ch: number) {
    const failed = await writeBuilt(app, outputSetCall(forms[ch]), $i18n);
    formError = { ...formError, [ch]: failed ?? "" };
    if (!failed) {
      const { [ch]: _, ...rest } = forms;
      forms = rest;
      await readOutputs();
    }
  }
  const MODES = optionsOf("xtrig.output_set", "mode");
  const LEVELS = optionsOf("xtrig.output_set", "level");
  const SOURCES = optionsOf("xtrig.output_set", "sources");
  function toggleSource(ch: number, s: string, on: boolean) {
    const f = forms[ch];
    forms = { ...forms, [ch]: { ...f, sources: on ? [...f.sources, s] : f.sources.filter((x) => x !== s) } };
  }

  // FPGA の中だけを読む（線を動かさない）ものは、開いたとき・つながったときに読む
  $effect(() => {
    if (!online) return;
    void readGpio();
    void readOutputs();
  });

  const hex8 = (v: number) => `0x${v.toString(16).toUpperCase().padStart(2, "0")}`;
  const disconnectedOnly = $derived(connected ? $i18n("lowLevel.disconnectedOnly") : "");
</script>

<div class="pane lowlevel">
  <section>
    <div class="pane-bar">
      <strong>{$i18n("lowLevel.dp")}</strong>
      {#if dpPath}<span class="muted mono small">{dpPath}</span>{/if}
      <span class="spacer"></span>
      <button class="small" disabled={!online} title={$i18n("lowLevel.readDpTip")} onclick={readDp}>{$i18n("lowLevel.read")}</button>
    </div>
    <table class="grid mono">
      <tbody>
        {#each DP_REGISTERS as r (r.name)}
          {@const c = dp[r.name]}
          <tr><td class="muted">{r.name}</td>
            <td>{#if c?.error}<span class="bad" title={c.error}>{c.error}</span>
              {:else}<EditValue text={c?.value === undefined ? "—" : hex32(c.value)} initial={c?.value === undefined ? "" : hex32(c.value)}
                                via={r.write && online ? "target.dp.write" : null}
                                reason={r.write ? "" : $i18n("lowLevel.readOnly")} onwrite={(input) => writeDp(r.name, input)} />{/if}</td></tr>
        {/each}
      </tbody>
    </table>
  </section>

  <section>
    <form class="pane-bar" onsubmit={readAp}>
      <strong>{$i18n("lowLevel.ap")}</strong>
      <label>{$i18n("lowLevel.apIndex")}<span class="req" aria-hidden="true">*</span><input class="mono num" aria-required="true"
             bind:value={apText} /></label>
      <label title={$i18n("lowLevel.apRegisterTip")}>{$i18n("lowLevel.register")}<span class="req" aria-hidden="true">*</span><input
             class="mono num" aria-required="true" bind:value={apRegText} /></label>
      <button class="small" disabled={!online}>{$i18n("lowLevel.read")}</button>
      {#if apError}<span class="bad small">{apError}</span>{/if}
    </form>
    {#if aps.length}
      <table class="grid mono">
        <tbody>
          {#each aps as r (`${r.ap}/${r.register}`)}
            <tr><td class="muted">AP {r.ap} · {hex8(r.register)}</td>
              <td>{#if r.cell.error}<span class="bad" title={r.cell.error}>{r.cell.error}</span>
                {:else}<EditValue text={r.cell.value === undefined ? "—" : hex32(r.cell.value)} via={online ? "target.ap.write" : null}
                                  onwrite={(input) => writeAp(r.ap, r.register, input)} />{/if}</td>
              <td><button class="small" aria-label={$i18n("common.remove")} title={$i18n("common.remove")}
                          onclick={() => (aps = aps.filter((x) => x !== r))}>×</button></td></tr>
          {/each}
        </tbody>
      </table>
    {/if}
  </section>

  <section>
    <form class="pane-bar" onsubmit={readCsr}>
      <strong title={$i18n("lowLevel.csrTip")}>{$i18n("lowLevel.csr")}</strong>
      <label>{$i18n("lowLevel.address")}<span class="req" aria-hidden="true">*</span><input class="mono num" aria-required="true"
             bind:value={csrAddress} /></label>
      <label>{$i18n("lowLevel.words")}<span class="req" aria-hidden="true">*</span><input class="mono num" aria-required="true"
             bind:value={csrCount} /></label>
      <button class="small" disabled={!online}>{$i18n("lowLevel.read")}</button>
      {#if csrError}<span class="bad small">{csrError}</span>{/if}
    </form>
    {#if csr}
      <table class="grid mono">
        <tbody>
          {#each csr.words as w, i (i)}
            {@const at = csr.address + 4 * i}
            <tr><td class="muted">{hex32(at)}</td>
              <td><EditValue text={hex32(w)} via={online ? "probe.csr.write" : null} onwrite={(input) => writeCsr(at, input)} /></td></tr>
          {/each}
        </tbody>
      </table>
    {/if}
  </section>

  <section>
    <div class="pane-bar">
      <strong>{$i18n("lowLevel.pins")}</strong>
      {#if pins}
        {#each PINS as p (p)}
          <button class="small mono bit" class:on={pins[p] === 1} disabled={!online || connected}
                  title={disconnectedOnly || $i18n("lowLevel.pinTip", { api: "probe.pins.write" })}
                  onclick={() => press(pinsWriteCall(p as Pin, pins![p]), readPins)}>{p}={pins[p]}</button>
        {/each}
      {/if}
      {#if pinsError}<span class="bad small" title={pinsError}>{pinsError}</span>{/if}
      <span class="spacer"></span>
      <button class="small" disabled={!online} title={$i18n("lowLevel.readPinsTip")} onclick={readPins}>{$i18n("lowLevel.read")}</button>
    </div>
  </section>

  <section>
    <div class="pane-bar">
      <strong title={$i18n("lowLevel.gpioTip")}>{$i18n("lowLevel.gpio")}</strong>
      {#if gpioError}<span class="bad small" title={gpioError}>{gpioError}</span>{/if}
      <span class="spacer"></span>
      <button class="small" disabled={!online} onclick={readGpio}>{$i18n("lowLevel.read")}</button>
    </div>
    {#if gpio}
      <table class="grid mono gpio">
        <thead><tr><th></th>{#each bitsOf(0) as _, ch (ch)}<th>ch{ch}</th>{/each}</tr></thead>
        <tbody>
          {#each ["drive", "enable"] as const as field (field)}
            <tr><td class="muted">{$i18n(`lowLevel.gpio.${field}`)}</td>
              {#each bitsOf(gpio[field]) as b, ch (ch)}
                <td><button class="small bit" class:on={b === 1} disabled={!online || connected}
                            title={disconnectedOnly || $i18n("lowLevel.pinTip", { api: "gpio.set" })}
                            onclick={() => press(gpioSetCall(gpio!, ch, field), readGpio)}>{b}</button></td>
              {/each}</tr>
          {/each}
          <tr><td class="muted">{$i18n("lowLevel.gpio.level")}</td>
            {#each bitsOf(gpio.level) as b, ch (ch)}<td class="level">{b}</td>{/each}</tr>
        </tbody>
      </table>
    {/if}
  </section>

  <section>
    <div class="pane-bar">
      <strong title={$i18n("lowLevel.outputsTip")}>{$i18n("lowLevel.outputs")}</strong>
      {#if outputs}<span class="muted small">{$i18n("lowLevel.outputsFree", { free: outputs.free ?? "?", actions: outputs.actions ?? "?" })}</span>{/if}
      {#if outputsError}<span class="bad small" title={outputsError}>{outputsError}</span>{/if}
      <span class="spacer"></span>
      <button class="small" disabled={!online} onclick={readOutputs}>{$i18n("lowLevel.read")}</button>
    </div>
    {#if outputs && !outputs.outputs.length}<p class="muted small pad">{$i18n("lowLevel.noOutputs")}</p>{/if}
    {#each outputs?.outputs ?? [] as o (o.channel)}
      {@const f = forms[o.channel]}
      <div class="output">
        <div class="mono">ch{o.channel}
          <span class="muted">{o.configured ? `${o.mode} ${o.level}${(o as any).active ? ` · ${$i18n("lowLevel.active")}` : ""}` : $i18n("lowLevel.notConfigured")}</span>
          {#if (o as any).pull_up}<span class="muted">{$i18n("lowLevel.pullUp")}</span>{/if}
        </div>
        {#if f}
          <div class="form">
            <select bind:value={f.mode} aria-label="mode" aria-required="true">{#if !f.mode}<option value="" disabled></option>{/if}
              {#each MODES as m (m)}<option value={m}>{m}</option>{/each}</select><span class="req" aria-hidden="true">*</span>
            <select bind:value={f.level} aria-label="level" aria-required="true">{#if !f.level}<option value="" disabled></option>{/if}
              {#each LEVELS as l (l)}<option value={l}>{l}</option>{/each}</select><span class="req" aria-hidden="true">*</span>
            {#each SOURCES as s (s)}
              <label class="mono"><input type="checkbox" checked={f.sources.includes(s)}
                                         onchange={(e) => toggleSource(o.channel, s, e.currentTarget.checked)} />{s}</label>
            {/each}
            <label title={$i18n("lowLevel.patternTip")}>{$i18n("lowLevel.pattern")}
              <input class="mono num" placeholder="mask" bind:value={f.mask} />
              <input class="mono num" placeholder="value" bind:value={f.value} />
              <label class="mono"><input type="checkbox" bind:checked={f.any} />any</label></label>
            {#if f.mode === "pulse"}
              <label>{$i18n("lowLevel.pulseUs")}<input class="mono num" bind:value={f.pulseUs} /></label>
            {/if}
            <label><input type="checkbox" bind:checked={f.enable} />{$i18n("lowLevel.enable")}</label>
            <button class="small" disabled={!online} title={$i18n("lowLevel.setTip", { api: "xtrig.output_set" })}
                    onclick={() => setOutput(o.channel)}>{$i18n("lowLevel.set")}</button>
            {#if o.configured}
              <button class="small" disabled={!online}
                      onclick={() => press({ method: (o as any).enabled ? "xtrig.output_disable" : "xtrig.output_enable",
                                             params: { channel: o.channel } }, readOutputs)}>
                {$i18n((o as any).enabled ? "lowLevel.disable" : "lowLevel.enable")}</button>
              <button class="small" disabled={!online} title={$i18n("lowLevel.releaseTip")}
                      onclick={() => press({ method: "xtrig.output_release", params: { channel: o.channel } }, readOutputs)}>
                {$i18n("lowLevel.release")}</button>
            {/if}
            {#if formError[o.channel]}<span class="bad small">{formError[o.channel]}</span>{/if}
          </div>
        {/if}
      </div>
    {/each}
  </section>
</div>

<style>
  .lowlevel { overflow: auto; }
  section { border-bottom: 1px solid var(--line); padding-bottom: 4px; }
  .grid { width: auto; min-width: 280px; margin: 0 8px; }
  .num { width: 7em; }
  label { display: inline-flex; gap: 3px; align-items: center; font-size: 12px; }
  .small { font-size: 12px; }
  .pad { padding: 2px 8px; margin: 0; }
  .bad { color: var(--bad); }
  .bit { min-width: 2em; }
  .bit.on { border-color: var(--accent); color: var(--accent); }
  .gpio td, .gpio th { text-align: center; padding: 1px 3px; }
  .level { color: var(--muted); }
  .output { padding: 4px 8px; font-size: 12px; }
  .form { display: flex; flex-wrap: wrap; gap: 4px 8px; align-items: center; margin-top: 3px; }
</style>
