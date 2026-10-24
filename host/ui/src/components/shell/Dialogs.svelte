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
  import { diagnose, mhzListToHz, mhzToHz, pinsText, type DiagnoseReport, type Pins } from "../../lib/diagnose";
  import { svdLoadParams } from "../../lib/peripherals";

  const { app, conn, dbg, ui } = shared();

  let path = $state(load("elf", ""));
  let device = $state(load("connect.target", ""));
  let mhz = $state(load("connect.mhz", ""));   // 使う人が入れる（品種・速さを初めから入れない）
  let diagMhz = $state(load("diagnose.mhz", ""));   // 診断で試す速さ（MHz のカンマ区切り。空なら接続の速さ 1 つ）
  let svd = $state(load("svd.device", ""));
  // デバイス名かパスかは使う人が選ぶ（文字の形で推さない）
  let svdKind = $state<"device" | "path">(load("svd.kind", "device") === "path" ? "path" : "device");
  let lang = $state<Lang>(getLang());
  let busy = $state(false);
  let picker = $state<HTMLInputElement>();
  let dialog = $state<HTMLDialogElement>();
  // 接続の診断（失敗の理由を手順ごとの事実で）。線を動かすので、人が押したときだけ
  let failed = $state(false);
  let badClock = $state(false);                      // 周波数の欄が正の数でない
  let diagnosis = $state<{ report: DiagnoseReport | null; pins: Pins | null; error: string | null } | null>(null);

  const kind = $derived($ui.dialog);
  const online = $derived($app.link === "open");

  // Connect を開くたびに覚えたターゲット名を読む（Flash のパネルの「接続に使う」が置いていく）
  $effect(() => {
    if (kind === "connect") untrack(() => {
      device = load("connect.target", device);
      failed = false;                                  // 前に開いたときの失敗・診断は持ち越さない
      diagnosis = null;
    });
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
    const hz = mhzToHz(mhz);
    badClock = hz === null;
    if (hz === null || !(await allowed(app))) return;
    save("connect.target", device);
    save("connect.mhz", mhz);
    busy = true;
    const done = await app.act("target.connect", { target: device, frequency_hz: hz });
    busy = false;
    failed = done === undefined;
    if (!failed) close();
  }

  async function runDiagnosis() {
    const speeds = mhzListToHz(diagMhz.trim() === "" ? mhz : diagMhz);
    badClock = speeds === null;
    if (speeds === null || !(await allowed(app))) return;
    save("diagnose.mhz", diagMhz);
    busy = true;
    diagnosis = await diagnose(conn, speeds);
    busy = false;
  }

  /** 診断が返した部品の候補と応答した速さを、接続の欄に入れる（接続は使う人が押す） */
  function useCandidate(name: string, hz: number | null) {
    device = name;
    if (hz !== null) mhz = String(hz / 1e6);
  }

  function close() {
    failed = false;
    diagnosis = null;
    ui.close();
  }

  async function loadSvd() {
    if (!(await allowed(app))) return;
    const params = svdLoadParams(svdKind, svd);
    if (!params) return;
    save("svd.device", svd);
    save("svd.kind", svdKind);
    busy = true;
    const done = await app.act("target.svd.load", params);
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
      <label for="dev">{$i18n("dialog.device")}<span class="req" aria-hidden="true">*</span></label>
      <input id="dev" class="mono" bind:value={device} aria-required="true" title={$i18n("dialog.deviceHint")} />
      <label for="clk">{$i18n("dialog.clock")}<span class="req" aria-hidden="true">*</span></label>
      <span><input id="clk" class="num" bind:value={mhz} inputmode="decimal" aria-required="true" /> MHz</span>
      <span></span><span class="muted small">{$i18n("dialog.deviceHint")}</span>
      <label for="dclk">{$i18n("diagnose.speeds")}</label>
      <span><input id="dclk" class="mono" bind:value={diagMhz} placeholder={mhz} title={$i18n("diagnose.speedsTip")} /> MHz</span>
      <span></span><span class="row"><button class="primary" disabled={!online || !device.trim() || !mhz.trim() || busy}>{$i18n("cmd.connect")}</button>
        <button type="button" disabled={!online || busy} title={$i18n("diagnose.tip")} onclick={() => void runDiagnosis()}>
          {$i18n("diagnose.run")}</button></span>
    </form>
    {#if badClock}<p class="bad small">{$i18n("dialog.badClock")}</p>{/if}
    {#if failed && !diagnosis}<p class="muted small">{$i18n("diagnose.afterFail")}</p>{/if}
    {#if diagnosis}
      <div class="diagnosis" aria-live="polite">
        {#if diagnosis.report}
          {@const r = diagnosis.report}
          <p class:bad={!r.reached}><strong>{r.reached ? $i18n("diagnose.reached") : $i18n("diagnose.notReached")}</strong>
            {#if r.reached}<span class="mono"> {String(r.protocol).toUpperCase()} {r.frequency_hz?.toLocaleString()} Hz</span>{/if}
            <span class="muted small"> ({$i18n("diagnose.tried")}: {r.frequencies_hz.map((f) => f.toLocaleString()).join(", ")} Hz)</span></p>
          <table class="steps">
            <tbody>
              {#each diagnosis.report.steps as step, i (i)}
                <tr class={step.status}><td class="st mono">{step.status}</td><td class="mono">{step.name}</td><td>{step.message}</td></tr>
              {/each}
            </tbody>
          </table>
          {#if r.reached && r.part?.candidates?.length}
            <div class="row"><span class="small">{$i18n("diagnose.candidates")}:</span>
              {#each r.part.candidates as c (c)}
                <button type="button" class="mono" disabled={busy} onclick={() => useCandidate(c, r.frequency_hz)}>{c}</button>
              {/each}</div>
          {/if}
        {:else if diagnosis.error}
          <p class="bad">{diagnosis.error}</p>
        {/if}
        {#if diagnosis.pins}<p class="mono small">{$i18n("diagnose.pins")}: {pinsText(diagnosis.pins)}</p>{/if}
      </div>
    {/if}
  {:else if kind === "loadSvd"}
    <h2>{$i18n("dialog.loadSvd")}</h2>
    <p class="muted">{$i18n("peripherals.noSvdHint")}</p>
    <form class="row" onsubmit={(e) => { e.preventDefault(); void loadSvd(); }}>
      <select bind:value={svdKind} aria-label={$i18n("dialog.svdKind")}>
        <option value="device">{$i18n("dialog.svdDevice")}</option>
        <option value="path">{$i18n("dialog.svdPath")}</option>
      </select>
      <input class="mono grow" aria-label={$i18n(svdKind === "path" ? "dialog.svdPath" : "dialog.svdDevice")}
             placeholder={$i18n(svdKind === "path" ? "dialog.svdPath" : "dialog.svdDevice")} bind:value={svd} />
      <button class="primary" disabled={!online || !svd.trim() || busy}>{$i18n("dialog.open")}</button>
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
  .diagnosis { margin-top: 12px; border-top: 1px solid var(--line); padding-top: 8px; }
  .steps { border-collapse: collapse; font-size: 12.5px; width: 100%; }
  .steps td { padding: 2px 8px 2px 0; vertical-align: top; }
  .steps .st { width: 3.5em; }
  .steps tr.fail .st, .bad { color: var(--bad); }
  .steps tr.ok .st { color: var(--ok); }
  .steps tr.skip { color: var(--muted); }
</style>
