<script lang="ts">
  // FPGA 観測（対象の FPGA の中を JTAG で観測する。Vivado の ILA・Signal Tap に当たるが、内蔵メモリの窓ではなく
  // 値の変化を流し続け、MCU・ロジアナと同じ時間軸に並べる）: 接続（鎖の位置・USER の命令・TCK）→ 観測の回路の申告と
  // 状態 → 組の選択と記録 → 信号の定義（名前・ビット・書式・列挙。後から付けた名前も過去の記録に効く）。
  // 仕様: docs/superpowers/specs/2026-10-20-fpga-observation-ui-design.md §4
  import { errorText } from "../../lib/api";
  import { shared } from "../../lib/context";
  import {
    configToFields, editableGroups, FORMATS, formatBits, formatEnum, parseBits, parseEnum, PRESETS, signalsChanged,
    statusKey, tckToHalfPeriod, validateSignals, type Groups, type ObsStatus, type OpenConfig, type PresetName,
    type SignalDef,
  } from "../../lib/fpgaobs";
  import { i18n } from "../../lib/i18n";
  import { load, save } from "../../lib/prefs";

  const POLL_MS = 500;
  const { app, conn } = shared();

  interface Declared { id: number; mode?: "stream" | "snapshot"; version: number; groups: number; fifo_depth: number;
    clock_hz: number; sync_delay: number; sample_delay: number; lost_bits?: number }
  interface Row { name: string; bits: string; format: SignalDef["format"]; enumText: string }

  const caps = $derived($app.probe?.capabilities);
  const available = $derived(Boolean(caps?.fpga_obs));
  const sysHz = $derived(Number(caps?.sys_clock_hz) || 100e6);
  const linkOpen = $derived($app.link === "open");

  const saved = JSON.parse(load("fpgaObs.connection", "{}") || "{}");
  let irLen = $state<number>(saved.irLen ?? 4);
  let irBefore = $state<number>(saved.irBefore ?? 0);
  let irAfter = $state<number>(saved.irAfter ?? 0);
  let drBefore = $state<number>(saved.drBefore ?? 0);
  let drAfter = $state<number>(saved.drAfter ?? 0);
  let userIr = $state<string>(saved.userIr ?? "0x2");
  let tckMhz = $state<number>(saved.tckMhz ?? 10);
  let words = $state<number>(saved.words ?? 64);
  let syncPeriod = $state<number>(saved.syncPeriod ?? 20);
  let autoReopen = $state<boolean>(saved.autoReopen ?? false);     // 止まったら（対象の FPGA を書き直した等）自動で開き直す
  const tck = $derived(tckToHalfPeriod(tckMhz, sysHz));

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
    save("fpgaObs.connection", JSON.stringify({ irLen, irBefore, irAfter, drBefore, drAfter, userIr, tckMhz, words,
                                                syncPeriod, autoReopen }));
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
        ({ irLen, irBefore, irAfter, drBefore, drAfter, userIr, tckMhz, words, syncPeriod } =
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
    const ir = Number(userIr);
    if (!Number.isInteger(ir) || ir < 0) {
      error = $i18n("fpgaObs.userIr");
      return;
    }
    const r = await app.act("fpga_obs.open", {
      chain: { ir_len: irLen, ir_before: irBefore, ir_after: irAfter, dr_before: drBefore, dr_after: drAfter },
      user_ir: ir, half_period: tck!.half, words, sync_period: syncPeriod, auto_reopen: autoReopen,
    });
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
  const select = () => app.act("fpga_obs.select", { group: observeGroup });
  const record = () => app.act("fpga_obs.start", {});
  const stopRecord = () => app.act("fpga_obs.stop", {});

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
    rowErrors.push(...validateSignals(defs).map((x) => $i18n(x.key, x.params)));
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
        <span class="badge" class:bad={status.stuck} class:warn={status.overflow_seen} class:ok={status.running && !status.overflow_seen}>
          {$i18n(statusKey(status))}
        </span>
        <span class="muted mono">{$i18n("fpgaObs.scans")} {status.scans}</span>
      {/if}
      <span class="spacer"></span>
      <button disabled={!linkOpen || status?.driving || !tck} onclick={connect}>{$i18n("fpgaObs.connect")}</button>
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
      <label>{$i18n("fpgaObs.userIr")}<input class="mono" bind:value={userIr} /></label>
      <label title={$i18n("fpgaObs.tckTip")}>{$i18n("fpgaObs.tck")}
        <input type="number" min="0.001" step="0.5" bind:value={tckMhz} />
        <span class="muted">{tck ? $i18n("fpgaObs.tckActual", { mhz: tck.mhz.toFixed(3) }) : "—"}</span></label>
      <label>{$i18n("fpgaObs.words")}<input type="number" min="1" max="256" bind:value={words} /></label>
      <label>{$i18n("fpgaObs.syncPeriod")}<input type="number" min="0" max="65535" bind:value={syncPeriod} /></label>
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
