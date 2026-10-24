<script lang="ts">
  // FPGA 観測（対象の FPGA の中を JTAG で観測する。Vivado の ILA・Signal Tap に当たるが、内蔵メモリの窓ではなく
  // 値の変化を流し続け、MCU・ロジアナと同じ時間軸に並べる）: 接続（鎖の位置・USER の命令・TCK）→ 観測の回路の申告と
  // 状態 → 組の選択と記録 → 信号の定義（名前・ビット・書式・列挙。後から付けた名前も過去の記録に効く）。
  // 観測の回路のレジスタ CONTROL・SELECT は fpga_obs.register_read で読んで描き、値のダブルクリックで
  // fpga_obs.register_write で書く（記録型だけ。標本型にはレジスタが無い）。
  // 仕様: docs/superpowers/specs/2026-10-20-fpga-observation-ui-design.md §4
  import { errorText } from "../../lib/api";
  import { shared } from "../../lib/context";
  import {
    configToFields, editableGroups, FORMATS, openParams, formatBits, formatEnum, OBS_REGISTERS, parseBits, parseEnum, PRESETS,
    registerWriteCall, signalsChanged,
    statusKeys, tckToHalfPeriod, type Groups, type ObsStatus, type OpenConfig, type PresetName,
    type SignalDef,
  } from "../../lib/fpgaobs";
  import { i18n } from "../../lib/i18n";
  import { load, save } from "../../lib/prefs";
  import { writeBuilt } from "../../lib/valuewrite";
  import EditValue from "./EditValue.svelte";

  const POLL_MS = 500;
  const { app, conn } = shared();

  interface Declared { id: number; mode?: "stream" | "snapshot"; version: number; groups: number; fifo_depth: number;
    clock_hz: number; sync_delay: number; sample_delay: number; lost_bits?: number }
  interface Row { name: string; bits: string; format: SignalDef["format"]; enumText: string }

  const caps = $derived($app.probe?.capabilities);
  const available = $derived(Boolean(caps?.fpga_obs));
  // プローブが申告したシステムのクロック。無ければ null（仮定しない: TCK を MHz で入れられず、半周期を直接入れる）
  const sysHz = $derived(Number(caps?.sys_clock_hz) > 0 ? Number(caps!.sys_clock_hz) : null);
  const linkOpen = $derived($app.link === "open");

  const saved = JSON.parse(load("fpgaObs.connection", "{}") || "{}");
  // チェーンの欄は空から（空 = 送らない）
  let irLen = $state<number | null>(saved.irLen ?? null);
  let irBefore = $state<number | null>(saved.irBefore ?? null);
  let irAfter = $state<number | null>(saved.irAfter ?? null);
  let drBefore = $state<number | null>(saved.drBefore ?? null);
  let drAfter = $state<number | null>(saved.drAfter ?? null);
  // User IR・TCK・語数・印の周期も空から（画面の既定を持たない。回路の型の設定はボタンで入れる）
  let userIr = $state<string>(saved.userIr ?? "");
  let tckMhz = $state<number | null>(saved.tckMhz ?? null);
  let halfPeriod = $state<number | null>(saved.halfPeriod ?? null);
  let words = $state<number | null>(saved.words ?? null);
  let syncPeriod = $state<number | null>(saved.syncPeriod ?? null);
  let idScans = $state<number | null>(saved.idScans ?? null);           // 空 = 送らない（上限なし）
  let autoReopen = $state<boolean>(saved.autoReopen ?? false);     // 止まったら（対象の FPGA を書き直した等）自動で開き直す
  const tck = $derived(sysHz ? tckToHalfPeriod(tckMhz ?? NaN, sysHz) : null);
  const params = $derived(openParams({ irLen, irBefore, irAfter, drBefore, drAfter, userIr, tckMhz, halfPeriod, words,
                                       syncPeriod, idScans, autoReopen }, sysHz));

  let declared = $state<Declared | null>(null);
  let status = $state<(ObsStatus & { scans: number; id: number; config?: OpenConfig | null;
                        declared?: Declared | null; auto_reopen?: boolean; reopened?: number }) | null>(null);
  let shownConfig = "";                        // 欄に写した開いた設定（変わったときだけ写す。編集を上書きしない）
  let error = $state<string | null>(null);
  let observeGroup = $state(0);                // 観測する組（観測の回路の SELECT）
  let editGroup = $state(0);                   // 信号の表で編集する組（観測とは別。過去の記録の組にも名前を付けられる）
  let newGroup = $state<number | null>(null);
  let groups = $state<Record<string, Row[]>>({});
  let rowErrors = $state<string[]>([]);

  $effect(() => {
    save("fpgaObs.connection", JSON.stringify({ irLen, irBefore, irAfter, drBefore, drAfter, userIr, tckMhz, halfPeriod,
                                                words, syncPeriod, idScans, autoReopen }));
  });

  // 状態を周期的に読む（つながっていて、観測の口のある構成のとき）
  $effect(() => {
    if (!linkOpen || !available) return;
    void readStatus();
    const timer = setInterval(() => void readStatus(), POLL_MS);
    return () => clearInterval(timer);
  });

  async function readStatus() {
    try {
      status = await conn.call("fpga_obs.status", {}, { quiet: true });
      // 開いた設定（別の窓・AI・前の画面が開いたものも）を欄と申告に写す: 見えている設定 = 動いている設定
      const key = JSON.stringify(status?.config ?? null);
      if (status?.config && key !== shownConfig) {
        ({ irLen, irBefore, irAfter, drBefore, drAfter, userIr, tckMhz, halfPeriod, words, syncPeriod, idScans } =
          configToFields(status.config, sysHz));
        declared = status.declared ?? declared;
        autoReopen = Boolean(status.auto_reopen);
      }
      shownConfig = key;
    } catch (e) {
      error = errorText(e);
    }
  }

  // 信号の定義（画面の状態・開いている記録の定義。記録の定義を優先して見せる）。プローブにつながっていなくても
  // 読み書きできる（開いている記録に後から名前を付ける）。デーモンにつながったとき・別の窓・AI が変えたら読み直す
  $effect(() => {
    if (!linkOpen) return;
    void readSignals();
    return conn.on("ui.state", (n: { changed?: string[] }) => {
      if (signalsChanged(n)) void readSignals();
    });
  });

  async function readSignals() {
    try {
      const r = await conn.call("fpga_obs.signals_get", {}, { quiet: true });
      const defs: { groups?: Record<string, SignalDef[]> } = r?.recording ?? r?.signals ?? {};
      groups = Object.fromEntries(Object.entries(defs.groups ?? {}).map(([g, sigs]) => [g, sigs.map(toRow)]));
    } catch (e) {
      error = errorText(e);
    }
  }

  const toRow = (s: SignalDef): Row => ({ name: s.name, bits: formatBits(s.lsb, s.width), format: s.format,
                                          enumText: formatEnum(s.enum ?? {}) });

  // 観測の回路の型に合う設定（標本型・1 クロック版は遅い TCK と 1 語）
  function applyPreset(name: PresetName) {
    ({ tckMhz, words, syncPeriod } = PRESETS[name]);
  }

  async function connect() {
    error = null;
    if (!params) {
      error = $i18n("fpgaObs.userIr");
      return;
    }
    const r = await app.act("fpga_obs.open", params);
    declared = r ?? null;
    await readStatus();
  }

  async function disconnect() {
    await app.act("fpga_obs.close", {});
    declared = null;
    await readStatus();
  }

  // 標本型（obs_snap_core）はレジスタを持たず、いつも組 0 を標本にする
  const snapshot = $derived(declared?.mode === "snapshot");
  const select = () => app.act("fpga_obs.select", { group: observeGroup }).then(readRegisters);
  const record = () => app.act("fpga_obs.start", {}).then(readRegisters);
  const stopRecord = () => app.act("fpga_obs.stop", {}).then(readRegisters);

  // 観測の回路のレジスタ（CONTROL・SELECT）: 動かしている間、記録型なら読む（開いた・選んだ・書いた後）
  let registers = $state<Record<number, { value?: number; error?: string }>>({});
  const hasRegisters = $derived(Boolean(status?.driving) && !snapshot);
  $effect(() => {
    if (hasRegisters) void readRegisters();
    else registers = {};
  });
  async function readRegisters() {
    if (!hasRegisters) return;
    const next: typeof registers = {};
    for (const r of OBS_REGISTERS) {
      try {
        next[r.addr] = { value: (await conn.call("fpga_obs.register_read", { addr: r.addr }, { quiet: true }))?.value };
      } catch (e) {
        next[r.addr] = { error: errorText(e) };
      }
    }
    registers = next;
  }
  async function writeRegister(addr: number, input: string): Promise<string | null> {
    const failed = await writeBuilt(app, registerWriteCall(addr, input), $i18n);
    if (!failed) await readRegisters();
    return failed;
  }
  const hex16 = (v: number) => `0x${v.toString(16).toUpperCase().padStart(4, "0")}`;

  // 信号の表の編集（編集する組）
  const rows = $derived(groups[String(editGroup)] ?? []);
  const editable = $derived(editableGroups(Object.keys(groups), declared?.groups ?? null, editGroup));
  function setRows(next: Row[]) {
    groups = { ...groups, [String(editGroup)]: next };
  }
  function addGroup() {
    if (newGroup !== null && Number.isInteger(newGroup) && newGroup >= 0 && newGroup <= 255) editGroup = newGroup;
    newGroup = null;
  }
  const addRow = () => setRows([...rows, { name: "", bits: "0", format: "hex", enumText: "" }]);
  const removeRow = (i: number) => setRows(rows.filter((_, k) => k !== i));
  const updateRow = (i: number, patch: Partial<Row>) => setRows(rows.map((r, k) => (k === i ? { ...r, ...patch } : r)));

  async function apply() {
    rowErrors = [];
    const defs: Groups = {};
    for (const [g, list] of Object.entries(groups)) {
      defs[g] = [];
      for (const r of list) {
        const bits = parseBits(r.bits);
        if (!bits) { rowErrors.push(`${r.name || "?"}: ${$i18n("fpgaObs.badBits")}`); continue; }
        const e = parseEnum(r.enumText);
        if (e === null) { rowErrors.push(`${r.name || "?"}: ${$i18n("fpgaObs.badEnum")}`); continue; }
        defs[g].push({ name: r.name.trim(), ...bits, format: r.format, enum: e });
      }
    }
    if (rowErrors.length) return;
    await app.act("fpga_obs.signals_set", { signals: { groups: defs } });
    await readSignals();
  }
</script>

<div class="pane fpga-obs">
  {#if !available}
    <div class="empty"><strong>{$i18n("fpgaObs.noFeature")}</strong></div>
  {:else}
    <div class="pane-bar">
      <strong>{$i18n("fpgaObs.connection")}</strong>
      {#if status}
        {#each statusKeys(status) as k (k)}
          <span class="badge" class:bad={k === "fpgaObs.state.stuck"} class:warn={k === "fpgaObs.state.overflow"}
                class:ok={k === "fpgaObs.state.running"}>{$i18n(k)}</span>
        {/each}
        <span class="muted mono">{$i18n("fpgaObs.scans")} {status.scans}</span>
      {/if}
      <span class="spacer"></span>
      <button disabled={!linkOpen || status?.driving || !params} onclick={connect}>{$i18n("fpgaObs.connect")}</button>
      <button disabled={!linkOpen || !status?.driving} onclick={disconnect}>{$i18n("fpgaObs.disconnect")}</button>
    </div>
    {#if error}<div class="note bad">{error}</div>{/if}
    {#if status?.stuck}
      <div class="note bad">{$i18n("fpgaObs.stuckHint", { id: `0x${status.id.toString(16).toUpperCase().padStart(8, "0")}` })}</div>
    {/if}

    <div class="form">
      <span class="muted">{$i18n("fpgaObs.preset")}</span>
      <button title={$i18n("fpgaObs.presetStreamTip")} onclick={() => applyPreset("stream")}>{$i18n("system.mode.streamShort")}</button>
      <button title={$i18n("fpgaObs.presetSnapshotTip")} onclick={() => applyPreset("snapshot")}>{$i18n("system.mode.snapshotShort")}</button>
    </div>
    <div class="form" title={$i18n("fpgaObs.chainTip")}>
      <label>{$i18n("fpgaObs.irLen")}<input type="number" min="1" max="32" bind:value={irLen} /></label>
      <label>{$i18n("fpgaObs.irBefore")}<input type="number" min="0" max="63" bind:value={irBefore} /></label>
      <label>{$i18n("fpgaObs.irAfter")}<input type="number" min="0" max="63" bind:value={irAfter} /></label>
      <label>{$i18n("fpgaObs.drBefore")}<input type="number" min="0" max="7" bind:value={drBefore} /></label>
      <label>{$i18n("fpgaObs.drAfter")}<input type="number" min="0" max="7" bind:value={drAfter} /></label>
      <label>{$i18n("fpgaObs.userIr")}<span class="req" aria-hidden="true">*</span><input class="mono" aria-required="true"
             bind:value={userIr} /></label>
      {#if sysHz}
        <label title={$i18n("fpgaObs.tckTip")}>{$i18n("fpgaObs.tck")}<span class="req" aria-hidden="true">*</span>
          <input type="number" min="0.001" step="0.5" aria-required="true" bind:value={tckMhz} />
          <span class="muted">{tck ? $i18n("fpgaObs.tckActual", { mhz: tck.mhz.toFixed(3) }) : "—"}</span></label>
      {:else}
        <!-- プローブがシステムのクロックを申告しない: MHz に換算できないので、半周期（sys の周期数）を直接 -->
        <label title={$i18n("fpgaObs.halfPeriodTip")}>{$i18n("fpgaObs.halfPeriod")}<span class="req" aria-hidden="true">*</span>
          <input type="number" min="2" max="65535" step="1" aria-required="true" bind:value={halfPeriod} /></label>
      {/if}
      <label>{$i18n("fpgaObs.words")}<span class="req" aria-hidden="true">*</span><input type="number" min="1" max="256"
             aria-required="true" bind:value={words} /></label>
      <label>{$i18n("fpgaObs.syncPeriod")}<span class="req" aria-hidden="true">*</span><input type="number" min="0" max="65535"
             aria-required="true" bind:value={syncPeriod} /></label>
      <label title={$i18n("fpgaObs.idScansTip")}>{$i18n("fpgaObs.idScans")}<input type="number" min="0" max="255"
             placeholder="—" bind:value={idScans} /></label>
      {#if status?.stuck && status.id_timeout}<span class="bad">{$i18n("fpgaObs.idTimeout")}</span>{/if}
      <label title={$i18n("fpgaObs.autoReopenTip")}><input type="checkbox" class="check" bind:checked={autoReopen} />
        {$i18n("fpgaObs.autoReopen")}{#if status?.reopened}<span class="muted"> {$i18n("fpgaObs.reopened", { n: status.reopened })}</span>{/if}</label>
    </div>

    {#if declared}
      <div class="pane-bar sub"><strong>{$i18n("fpgaObs.declared")}</strong></div>
      <div class="stats mono">
        <span>{$i18n("fpgaObs.mode")} {$i18n(declared.mode === "snapshot" ? "system.mode.snapshot" : "system.mode.stream")}</span>
        <span>{$i18n("fpgaObs.id")} 0x{declared.id.toString(16).toUpperCase()}</span>
        <span>{$i18n("fpgaObs.version")} {declared.version}</span>
        <span>{$i18n("fpgaObs.groups")} {declared.groups}</span>
        <span>{$i18n("fpgaObs.fifo")} {declared.fifo_depth}</span>
        <span>{$i18n("fpgaObs.clock")} {declared.clock_hz ? `${(declared.clock_hz / 1e6).toFixed(3)} MHz` : "—"}</span>
        <span>{$i18n("fpgaObs.delays")} {declared.sync_delay} / {declared.sample_delay}</span>
      </div>
    {/if}

    {#if hasRegisters}
      <div class="pane-bar sub">
        <strong title={$i18n("fpgaObs.registersTip")}>{$i18n("fpgaObs.registers")}</strong>
        {#each OBS_REGISTERS as r (r.addr)}
          {@const reg = registers[r.addr]}
          <span class="mono"><span class="muted">{r.name} ({r.addr})</span>
            {#if reg?.error}<span class="bad" title={reg.error}>!</span>
            {:else}<EditValue text={reg?.value === undefined ? "—" : hex16(reg.value)}
                              via={reg?.value === undefined ? null : "fpga_obs.register_write"}
                              onwrite={(input) => writeRegister(r.addr, input)} />{/if}</span>
        {/each}
        <span class="spacer"></span>
        <button class="small" onclick={readRegisters}>{$i18n("common.refresh")}</button>
      </div>
    {/if}

    <div class="pane-bar sub">
      <label>{$i18n("fpgaObs.group")}
        <select bind:value={observeGroup} onchange={select} disabled={(!status?.running && !declared) || snapshot}>
          {#each Array.from({ length: Math.max(1, declared?.groups ?? 1) }, (_, k) => k) as g (g)}
            <option value={g}>{g}</option>
          {/each}
        </select>
      </label>
      <button disabled={!status?.driving || snapshot} title={$i18n("fpgaObs.recordTip")} onclick={record}>{$i18n("fpgaObs.record")}</button>
      <button disabled={!status?.driving || snapshot} onclick={stopRecord}>{$i18n("fpgaObs.stopRecord")}</button>
    </div>

  {/if}

  <div class="pane-bar sub" title={$i18n("fpgaObs.signalsTip")}>
    <strong>{$i18n("fpgaObs.signals")}</strong>
    <label>{$i18n("fpgaObs.group")}
      <select bind:value={editGroup}>{#each editable as g (g)}<option value={g}>{g}</option>{/each}</select>
    </label>
    <input class="narrow" type="number" min="0" max="255" placeholder="+" bind:value={newGroup} onchange={addGroup} />
    <span class="spacer"></span>
    <button onclick={addRow}>{$i18n("fpgaObs.addSignal")}</button>
    <button onclick={apply}>{$i18n("fpgaObs.save")}</button>
  </div>
  {#each rowErrors as e, k (k)}<div class="note bad">{e}</div>{/each}
    <table class="grid">
      <thead><tr><th>{$i18n("fpgaObs.name")}</th><th>{$i18n("fpgaObs.bits")}</th><th>{$i18n("fpgaObs.format")}</th>
        <th title={$i18n("fpgaObs.enumTip")}>{$i18n("fpgaObs.enum")}</th><th></th></tr></thead>
      <tbody>
        {#each rows as r, i (i)}
          <tr>
            <td><input value={r.name} oninput={(e) => updateRow(i, { name: e.currentTarget.value })} /></td>
            <td><input class="mono narrow" value={r.bits} oninput={(e) => updateRow(i, { bits: e.currentTarget.value })} /></td>
            <td><select value={r.format} onchange={(e) => updateRow(i, { format: e.currentTarget.value as Row["format"] })}>
              {#each FORMATS as f (f)}<option value={f}>{f}</option>{/each}</select></td>
            <td><input placeholder={$i18n("fpgaObs.enumTip")} disabled={r.format !== "enum"} value={r.enumText}
                       oninput={(e) => updateRow(i, { enumText: e.currentTarget.value })} /></td>
            <td><button onclick={() => removeRow(i)}>{$i18n("fpgaObs.remove")}</button></td>
          </tr>
        {/each}
      </tbody>
    </table>
</div>

<style>
  .form { display: flex; flex-wrap: wrap; gap: 4px 12px; padding: 6px 8px; font-size: 12.5px; }
  .form label { display: flex; align-items: center; gap: 4px; }
  .form input[type="number"] { width: 6em; }
  .stats { display: flex; flex-wrap: wrap; gap: 4px 16px; padding: 4px 8px; font-size: 12.5px; }
  .badge { font-size: 11px; border: 1px solid currentColor; border-radius: 8px; padding: 0 6px; }
  .ok { color: var(--ok); }
  .warn { color: var(--warn); }
  .bad { color: var(--bad); }
  .note { padding: 3px 8px; font-size: 12px; }
  .sub { border-top: 1px solid var(--line); }
  .narrow { width: 5em; }
</style>
