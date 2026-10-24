<script lang="ts">
  // Peripherals: SVD の周辺の木。周辺を開くとレジスタを読み、フィールドに解読する（読むと状態が変わるものは読まない）。
  // フィールドは SVD の定義に合わせて書ける（1 ビット = チェックボックス、値の名前 = 選択、ほか = 数。Enter で書く）。
  // レジスタの値はダブルクリックで全体を書く（16 進）。読むと状態が変わるレジスタは ⚠ と確認
  import { errorText } from "../../lib/api";
  import { tick } from "svelte";
  import { shared } from "../../lib/context";
  import { onCalls } from "../../lib/notify";
  import { allowed } from "../../lib/guard";
  import { confirmer } from "../../lib/confirm";
  import { i18n } from "../../lib/i18n";
  import { fieldControl, parseField, parseRegister, type FieldDef, type RegisterDef } from "../../lib/peripherals";

  const { app, conn, ui } = shared();
  let list = $state<any[] | null>(null);
  let device = $state("");
  let filter = $state("");
  let open = $state<string | null>(null);
  let regs = $state<any>(null);
  let defs = $state<Record<string, RegisterDef>>({});        // 開いた周辺のレジスタの定義（path → 定義）
  let editingReg = $state<{ path: string; text: string } | null>(null);
  let error = $state<string | null>(null);

  const online = $derived($app.link === "open");
  const connected = $derived(!!$app.target?.state && $app.target.state !== "DISCONNECTED");
  const halted = $derived($app.target?.state === "HALTED");
  const key = $derived(halted ? `${$app.target?.pc}/${$app.target?.run}` : "");
  const shown = $derived((list ?? []).filter((p) => !filter || `${p.name} ${p.group ?? ""}`.toUpperCase().includes(filter.toUpperCase())));

  $effect(() => {
    if (online) void refresh();
  });
  $effect(() => {
    if (key && open) void readPeripheral(open);
  });
  // メニューの Load SVD… で読んだら一覧を読み直す
  $effect(() => {
    if ($ui.dialog === null && online) void refresh();
  });
  // ほかの利用者（AI も）が周辺レジスタ・メモリに書いたら読み直す
  $effect(() => onCalls(conn, ["target.peripheral.write", "target.memory.write"], () => {
    if (open && connected) void readPeripheral(open);
  }));

  async function refresh() {
    try {
      const r = await conn.call("target.svd.peripherals", {}, { quiet: true });
      device = r.device;
      list = r.peripherals;
    } catch {
      list = null;
    }
  }

  async function readPeripheral(name: string) {
    try {
      regs = await conn.call("target.peripheral.read", { path: name }, { quiet: true });
      error = null;
    } catch (e) {
      regs = null;
      error = errorText(e);
    }
  }

  async function loadDefs(name: string) {
    try {
      const r = await conn.call("target.svd.registers", { peripheral: name }, { quiet: true });
      defs = Object.fromEntries((r.registers as RegisterDef[]).map((d) => [d.path, d]));
    } catch {
      defs = {};
    }
  }

  async function toggle(name: string) {
    open = open === name ? null : name;
    regs = null;
    defs = {};
    error = null;
    if (open) {
      await loadDefs(open);
      if (connected) await readPeripheral(open);
    }
  }

  /** 書く（fields か value）。読むと状態が変わるレジスタは確認して allow_side_effects */
  async function write(path: string, change: { fields?: Record<string, number | string>; value?: number }) {
    const def = defs[path];
    let allow = false;
    if (def?.read_side_effects && change.fields) {
      if (!(await confirmer.ask($i18n("peripherals.sideEffectConfirm", { path }), { ok: $i18n("peripherals.writeAnyway"),
                                                                                   danger: true }))) return;
      allow = true;
    }
    if (!(await allowed(app))) return;
    const done = await app.act("target.peripheral.write", { path, ...change, ...(allow ? { allow_side_effects: true } : {}) });
    if (done !== undefined && open) await readPeripheral(open);
  }

  function fieldDef(path: string, name: string): FieldDef | undefined {
    return defs[path]?.fields.find((f) => f.name === name);
  }

  function commitNumber(path: string, f: FieldDef, el: HTMLInputElement, current: number) {
    const v = parseField(el.value, f.bit_width);
    if (v === null) {
      error = $i18n("peripherals.badValue", { field: f.name, bits: f.bit_width });
      el.value = String(current);
      return;
    }
    if (v !== current) void write(path, { fields: { [f.name]: v } });
  }

  async function commitRegister() {
    if (!editingReg) return;
    const { path, text } = editingReg;
    editingReg = null;
    // 大きさは読んだ値に API が添えた SVD の大きさ（無ければ書かない。32 ビットとみなさない）
    const size = sizeOf(path);
    if (size === null) return;
    const v = parseRegister(text, size);
    if (v === null) {
      error = $i18n("peripherals.badRegister", { path, bits: size });
      return;
    }
    await write(path, { value: v });
  }

  function onRegKey(e: KeyboardEvent) {
    if (e.key === "Enter") {
      e.preventDefault();
      void commitRegister();
    } else if (e.key === "Escape") {
      e.preventDefault();
      editingReg = null;
    }
  }

  const focus = (el: HTMLInputElement) => {
    void tick().then(() => {
      el.focus();
      el.select();
    });
  };
  const hex = (n: number, digits = 8) => `0x${(n >>> 0).toString(16).toUpperCase().padStart(digits, "0")}`;
  /** レジスタの大きさ（ビット）: 読んだ値に API が添えたもの（target.peripheral.read の size）。無ければ null */
  function sizeOf(path: string): number | null {
    const r = (regs?.registers ?? []).find((x: any) => x.path === path);
    return typeof r?.size === "number" && r.size > 0 ? r.size : null;
  }
  /** 値の 16 進（大きさが分かれば桁をそろえる。分からなければ桁を足さない） */
  const regHex = (value: number, size: number | null) => (size === null ? hex(value, 1) : hex(value, Math.ceil(size / 4)));
</script>

<div class="pane">
  {#if list}
    <div class="pane-bar">
      <span class="mono">{device}</span>
      <input placeholder={$i18n("peripherals.filter")} aria-label={$i18n("peripherals.filter")} bind:value={filter} />
      {#if error}<span class="bad" title={error}>{error}</span>{/if}
    </div>
    <ul class="tree mono">
      {#each shown as p (p.name)}
        <li>
          <button class="node" class:open={open === p.name} title={p.description ?? ""} onclick={() => toggle(p.name)}>
            <span class="caret">{open === p.name ? "▾" : "▸"}</span>{p.name}<span class="muted">{hex(p.base_address)}</span>
          </button>
          {#if open === p.name && regs}
            <ul>
              {#each regs.registers as r (r.path)}
                {@const def = defs[r.path]}
                <li class="reg">
                  <span title={def?.description ?? ""}>{r.path.split(".").pop()}
                    {#if def?.read_side_effects}<span class="warn" title={$i18n("peripherals.sideEffectTip")}>⚠</span>{/if}</span>
                  {#if editingReg && editingReg.path === r.path}
                    <input class="mono regedit" aria-label={$i18n("peripherals.editRegister")} bind:value={editingReg.text}
                           use:focus onkeydown={onRegKey} onblur={() => (editingReg = null)} />
                  {:else}
                    {@const size = sizeOf(r.path)}
                    {@const editable = connected && def?.access !== "read-only" && size !== null}
                    <!-- svelte-ignore a11y_no_static_element_interactions -->
                    <span class="v" class:can-edit={editable} title={$i18n("peripherals.editRegisterTip")}
                          ondblclick={() => editable && (editingReg = { path: r.path, text: regHex(r.value, size) })}>
                      {regHex(r.value, size)}</span>
                  {/if}
                  <span class="muted">{hex(r.address)}</span>
                  <ul class="fields">
                    {#each r.fields as f (f.name)}
                      {@const fd = fieldDef(r.path, f.name)}
                      {@const control = connected ? fieldControl(fd, def) : "readonly"}
                      <li title={fd?.description ?? ""}>
                        <span>{f.name}</span><span class="muted">{f.bits}</span>
                        {#if control === "check"}
                          <label class="fv"><input type="checkbox" checked={f.value === 1} aria-label={f.name}
                                 onchange={(e) => write(r.path, { fields: { [f.name]: (e.currentTarget as HTMLInputElement).checked ? 1 : 0 } })} />
                            {f.value}</label>
                        {:else if control === "select" && fd}
                          <select class="fv" aria-label={f.name} value={String(f.value)}
                                  onchange={(e) => write(r.path, { fields: { [f.name]: Number((e.currentTarget as HTMLSelectElement).value) } })}>
                            {#if !fd.enums.some((x) => x.value === f.value)}<option value={String(f.value)}>{f.value}</option>{/if}
                            {#each fd.enums.filter((x) => x.value !== null) as x (x.name)}
                              <option value={String(x.value)}>{x.name} ({x.value})</option>
                            {/each}
                          </select>
                        {:else if control === "number" && fd}
                          <input class="fv num mono" aria-label={f.name} value={String(f.value)} title={$i18n("peripherals.fieldTip", { bits: fd.bit_width })}
                                 onchange={(e) => commitNumber(r.path, fd, e.currentTarget as HTMLInputElement, f.value)}
                                 onkeydown={(e) => e.key === "Enter" && (e.currentTarget as HTMLInputElement).blur()} />
                        {:else}
                          <span class="v fv">{f.enum ?? f.value}</span>
                        {/if}
                      </li>
                    {/each}
                  </ul>
                </li>
              {/each}
              {#if regs.skipped?.length}
                <li class="muted">{$i18n("peripherals.skipped", { names: regs.skipped.map((s: any) => s.path ?? s).join(", ") })}</li>
              {/if}
            </ul>
          {/if}
        </li>
      {/each}
    </ul>
  {:else}
    <div class="empty">
      <strong>{$i18n("peripherals.noSvd")}</strong>
      <span>{$i18n("peripherals.noSvdHint")}</span>
      <div class="actions">
        <button disabled={!online} title={$i18n("tip.loadSvd")} onclick={async () => (await allowed(app)) && ui.open("loadSvd")}>
          {$i18n("cmd.loadSvd")}</button>
      </div>
    </div>
  {/if}
</div>

<style>
  .tree, .tree ul { list-style: none; margin: 0; padding: 0; }
  .tree ul { padding-left: 18px; }
  .node { display: flex; gap: 6px; width: 100%; border: none; border-radius: 0; background: none; padding: 2px 8px; text-align: left; }
  .node:hover { background: var(--select); }
  .node .muted { margin-left: auto; }
  .caret { width: 10px; color: var(--muted); }
  .reg { display: grid; grid-template-columns: 1fr auto auto; gap: 2px 12px; padding: 2px 8px; align-items: center; }
  .reg .fields { grid-column: 1 / -1; }
  .fields li { display: grid; grid-template-columns: 1fr 70px 150px; gap: 12px; font-size: 12px; padding: 1px 8px; align-items: center; }
  .v { color: var(--accent); }
  .can-edit { cursor: text; }
  .fv { justify-self: start; }
  select.fv, input.num { font-size: 12px; padding: 0 4px; max-width: 150px; }
  input.num { width: 90px; }
  .regedit { width: 110px; padding: 0 4px; font-size: 12px; }
  .warn { color: var(--warn); margin-left: 4px; }
  .bad { color: var(--bad); font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 50%; }
  input:not([type]) { flex: 1; min-width: 80px; }
</style>
