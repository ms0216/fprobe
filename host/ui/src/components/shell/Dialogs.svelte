<script lang="ts">
  // ダイアログ: Open ELF（この PC のファイルを選ぶ・デーモンの PC のパス・最近使った項目）、Connect、Load SVD、
  // Settings（表示言語、実行: ステップ中の割り込み・リセットの種類）、Keyboard Shortcuts、About
  import { untrack } from "svelte";
  import { COMMANDS } from "../../lib/commands";
  import { RESET_KINDS, execution, type ResetKind } from "../../lib/execution";
  import { shared } from "../../lib/context";
  import { allowed } from "../../lib/guard";
  import { LANGS, getLang, setLang, i18n, type Lang } from "../../lib/i18n";
  import { load, save } from "../../lib/prefs";

  const { app, dbg, ui } = shared();

  let path = $state(load("elf", ""));
  let device = $state(load("connect.target", "stm32f401xe"));
  let mhz = $state(load("connect.mhz", "4"));
  let svd = $state(load("svd.device", "STM32F401"));
  let lang = $state<Lang>(getLang());
  let busy = $state(false);
  let picker = $state<HTMLInputElement>();
  let dialog = $state<HTMLDialogElement>();

  const kind = $derived($ui.dialog);
  const online = $derived($app.link === "open");

  // Connect を開くたびに覚えたターゲット名を読む（Flash のパネルの「接続に使う」が置いていく）
  $effect(() => {
    if (kind === "connect") untrack(() => (device = load("connect.target", device)));
  });

  $effect(() => {
    if (!dialog) return;
    if (kind && !dialog.open) dialog.showModal();
    if (!kind && dialog.open) dialog.close();
  });

  async function openPath(p: string) {
    if (!p || !(await allowed(app))) return;
    busy = true;
    save("elf", p);
    if (await dbg.loadElf(p)) {
      ui.remember(p);
      ui.close();
    }
    busy = false;
  }

  async function upload(e: Event) {
    const file = (e.currentTarget as HTMLInputElement).files?.[0];
    if (!file || !(await allowed(app))) return;
    busy = true;
    const saved = await dbg.upload(file.name, await file.arrayBuffer());
    if (saved) {
      ui.remember(saved);
      ui.close();
    }
    busy = false;
  }

  async function connect() {
    if (!(await allowed(app))) return;
    save("connect.target", device);
    save("connect.mhz", mhz);
    busy = true;
    const done = await app.act("target.connect", { target: device, frequency_hz: Math.round(Number(mhz) * 1e6) });
    busy = false;
    if (done !== undefined) ui.close();
  }

  async function loadSvd() {
    if (!(await allowed(app))) return;
    save("svd.device", svd);
    busy = true;
    const isPath = /[\\/]|\.svd$/i.test(svd);
    const done = await app.act("target.svd.load", isPath ? { path: svd } : { device: svd });
    busy = false;
    if (done !== undefined) ui.close();
  }
</script>

<dialog bind:this={dialog} onclose={() => ui.close()} onclick={(e) => e.target === dialog && ui.close()}>
  {#if kind === "openElf"}
    <h2>{$i18n("dialog.openElf")}</h2>
    <p class="muted">{$i18n("dialog.openElfHint")}</p>
    <div class="row">
      <button class="primary" disabled={!online || busy} onclick={() => picker?.click()}>{$i18n("source.browse")}</button>
      <input type="file" accept=".elf,.axf,.out,application/octet-stream" hidden bind:this={picker} onchange={upload} />
    </div>
    <form class="row" onsubmit={(e) => { e.preventDefault(); void openPath(path); }}>
      <input class="mono grow" placeholder={$i18n("source.path")} aria-label={$i18n("source.path")} bind:value={path} />
      <button disabled={!online || !path || busy}>{$i18n("dialog.open")}</button>
    </form>
    {#if $ui.recent.length}
      <h3>{$i18n("source.recent")}</h3>
      <ul class="recent">
        {#each $ui.recent as p (p)}<li><button class="link mono" disabled={!online || busy} onclick={() => openPath(p)}>{p}</button></li>{/each}
      </ul>
    {/if}
  {:else if kind === "connect"}
    <h2>{$i18n("dialog.connect")}</h2>
    <form class="grid" onsubmit={(e) => { e.preventDefault(); void connect(); }}>
      <label for="dev">{$i18n("dialog.device")}</label>
      <input id="dev" class="mono" bind:value={device} title={$i18n("dialog.deviceHint")} />
      <label for="clk">{$i18n("dialog.clock")}</label>
      <span><input id="clk" class="num" bind:value={mhz} inputmode="decimal" /> MHz</span>
      <span></span><span class="muted small">{$i18n("dialog.deviceHint")}</span>
      <span></span><span class="row"><button class="primary" disabled={!online || !device || busy}>{$i18n("cmd.connect")}</button></span>
    </form>
  {:else if kind === "loadSvd"}
    <h2>{$i18n("dialog.loadSvd")}</h2>
    <p class="muted">{$i18n("peripherals.noSvdHint")}</p>
    <form class="row" onsubmit={(e) => { e.preventDefault(); void loadSvd(); }}>
      <input class="mono grow" aria-label={$i18n("peripherals.device")} placeholder={$i18n("peripherals.device")} bind:value={svd} />
      <button class="primary" disabled={!online || !svd || busy}>{$i18n("dialog.open")}</button>
    </form>
  {:else if kind === "settings"}
    <h2>{$i18n("dialog.settings")}</h2>
    <label class="row">{$i18n("dialog.language")}
      <select bind:value={lang} onchange={() => setLang(lang)}>
        {#each LANGS as l (l.id)}<option value={l.id}>{l.label}</option>{/each}
      </select>
    </label>
    <h3>{$i18n("dialog.execution")}</h3>
    <label class="row" title={$i18n("dialog.maskInterruptsTip")}>
      <input type="checkbox" checked={$execution.maskInterrupts}
             onchange={(e) => execution.set({ maskInterrupts: (e.currentTarget as HTMLInputElement).checked })} />
      {$i18n("dialog.maskInterrupts")}
    </label>
    <label class="row" title={$i18n("dialog.resetKindTip")}>{$i18n("dialog.resetKind")}
      <select value={$execution.resetKind}
              onchange={(e) => execution.set({ resetKind: (e.currentTarget as HTMLSelectElement).value as ResetKind })}>
        {#each RESET_KINDS as k (k)}<option value={k}>{$i18n(`reset.${k}`)}</option>{/each}
      </select>
    </label>
  {:else if kind === "shortcuts"}
    <h2>{$i18n("dialog.shortcuts")}</h2>
    <table class="keys">
      <tbody>
        {#each COMMANDS.filter((c) => c.shortcut) as c (c.id)}
          <tr><td>{$i18n(c.label)}</td><td class="mono">{c.shortcut}</td><td class="muted">{$i18n(c.tip)}</td></tr>
        {/each}
      </tbody>
    </table>
  {:else if kind === "about"}
    <h2>fprobe</h2>
    <p>{$i18n("dialog.about")}</p>
  {/if}
  <div class="row end"><button onclick={() => ui.close()}>{$i18n("common.close")}</button></div>
</dialog>

<style>
  dialog { border: 1px solid var(--line); border-radius: 10px; background: var(--panel); color: var(--text);
           padding: 16px 20px; min-width: 420px; max-width: min(720px, 92vw); box-shadow: 0 12px 40px rgba(0, 0, 0, 0.25); }
  dialog::backdrop { background: rgba(0, 0, 0, 0.3); }
  h2 { margin: 0 0 10px; font-size: 16px; }
  h3 { margin: 14px 0 6px; font-size: 13px; color: var(--muted); }
  .row { display: flex; gap: 8px; align-items: center; margin: 8px 0; }
  .row.end { justify-content: flex-end; margin-top: 16px; }
  .grow { flex: 1; }
  .grid { display: grid; grid-template-columns: auto 1fr; gap: 8px 12px; align-items: center; }
  .num { width: 70px; }
  .small { font-size: 12px; }
  .recent { list-style: none; padding: 0; margin: 0; display: flex; flex-direction: column; gap: 4px; }
  .keys td { padding: 3px 10px 3px 0; }
</style>
