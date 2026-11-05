<script lang="ts">
  // Flash（J-Flash Lite の型）: データ ファイル（デーモンの PC のパス・この PC から選んで送る・読み込んだ ELF）、
  // 消去（auto / sector / chip）・ベリファイ・変更なしを省略・リセットして実行の設定、Program・Verify・Erase・Read Back、
  // 進捗（通知 flash.progress。AI の書き込みも見える）、結果（段ごとの時間・速度・ベリファイ）。詳細に Flash の領域・
  // CMSIS パック（探す・入れるはネットに出るので確認）・速度の計測。詳細の各節は panels/flash/ の部品
  import { onDestroy } from "svelte";
  import Req from "./Req.svelte";
  import { shared } from "../../lib/context";
  import { base64 } from "../../lib/debugger";
  import {
    defaultReadPath, loadOptions, parseNumber, programParams, seconds, size, verifyParams,
    type FlashOptions, type FlashResultState, type Progress,
  } from "../../lib/flash";
  import type { Methods, Params, Result } from "../../lib/api";
  import { allowed } from "../../lib/guard";
  import { confirmer } from "../../lib/confirm";
  import { i18n, t, type Key } from "../../lib/i18n";
  import { load, save } from "../../lib/prefs";
  import Empty from "./Empty.svelte";
  import { hex32 } from "./format";
  import FlashBench from "./flash/FlashBench.svelte";
  import FlashPacks from "./flash/FlashPacks.svelte";
  import FlashProtection from "./flash/FlashProtection.svelte";
  import FlashRegions from "./flash/FlashRegions.svelte";
  import FlashResult from "./flash/FlashResult.svelte";

  const { app, conn, dbg } = shared();

  let path = $state(load("flash.path", ""));
  // 前の版は ELF・HEX のときも欄を隠して値を覚えていた: 隠れていた値を黙って送らないよう、覚える名前を変え空から始める
  let address = $state(load("flash.loadAddress", ""));
  let options = $state<FlashOptions>(loadOptions(load("flash.options", "{}")));
  let eraseMode = $state<"range" | "chip" | "mass">("range");
  // 消す範囲は使う人が入れる（特定の品種の番地・長さを初めから入れない）
  let eraseAddress = $state("");
  let eraseLength = $state("");
  let readPath = $state(load("flash.readPath", ""));
  let readAddress = $state("");
  let readLength = $state("");
  let busy = $state<string | null>(null);
  // 保護の部品の表と結果は、接続の有無で部品が作り直されても残すためここに持つ
  let ob = $state<Result<"target.option_bytes.read"> | null>(null);
  let obEdit = $state<Record<string, string>>({});
  let obResult = $state<any>(null);
  let progress = $state<Progress | null>(null);
  let note = $state<string | null>(null);              // 送った・保存した等の一行
  let invalid = $state<string | null>(null);           // 欄の誤り
  let result = $state<FlashResultState | null>(null);
  let fileInput = $state<HTMLInputElement>();

  const online = $derived($app.link === "open");
  const connected = $derived(!!$app.target?.state && $app.target.state !== "DISCONNECTED");

  $effect(() => save("flash.path", path));
  $effect(() => save("flash.loadAddress", address));
  $effect(() => save("flash.options", JSON.stringify(options)));
  $effect(() => save("flash.readPath", readPath));
  // 初めて開いたとき: パスが空なら読み込んでいる ELF
  $effect(() => {
    if (!path && $dbg.elf) path = $dbg.elf;
  });
  // 進捗（誰の操作でも）。終わりの 1 が届いたら少し後に消す
  let clearTimer: ReturnType<typeof setTimeout> | undefined;
  const stopProgress = conn.on("flash.progress", (p: Progress) => {
    progress = p;
    clearTimeout(clearTimer);
    if (p.fraction >= 1 && (p.operation !== "program" || p.phase === (options.verify ? "verify" : "program"))) {
      clearTimer = setTimeout(() => (progress = null), 1500);
    }
  });
  onDestroy(() => {
    stopProgress();
    clearTimeout(clearTimer);
  });

  async function run<M extends keyof Methods>(kind: string, method: M, params: Params<M>): Promise<Result<M> | undefined> {
    if (!(await allowed(app))) return undefined;
    busy = kind;
    note = invalid = null;
    progress = null;
    try {
      return await app.act(method, params);
    } finally {
      busy = null;
    }
  }

  async function program() {
    let params;
    try {
      params = programParams(path.trim(), address, options);
    } catch {
      invalid = t("flash.addressHint");
      return;
    }
    const r = await run("program", "target.flash", params);
    if (r) result = { kind: "program", data: r };
  }

  async function verifyNow() {
    let params;
    try {
      params = verifyParams(path.trim(), address);
    } catch {
      invalid = t("flash.addressHint");
      return;
    }
    const r = await run("verify", "target.flash.verify", params);
    if (r) result = { kind: "verify", data: r };
  }

  async function erase() {
    const start = parseNumber(eraseAddress), length = parseNumber(eraseLength);
    if (eraseMode === "range" && (start === null || length === null)) {
      invalid = t("flash.addressHint");
      return;
    }
    const what = eraseMode === "range" ? `${hex32(start!)} + ${size(length)}`
      : t(eraseMode === "chip" ? "flash.eraseChip" : "flash.eraseMass");
    if (!(await confirmer.ask(t("flash.confirmErase", { what }), { ok: t("flash.erase"), danger: true }))) return;
    const params = eraseMode === "range" ? { mode: eraseMode, address: start, length } : { mode: eraseMode };
    const r = await run("erase", "target.flash.erase", params);
    if (r) result = { kind: "erase", data: r };
  }

  async function readBack() {
    const target = readPath.trim() || defaultReadPath($dbg.elf);
    const at = readAddress.trim() ? parseNumber(readAddress) : null, n = readLength.trim() ? parseNumber(readLength) : null;
    if ((readAddress.trim() && at === null) || (readLength.trim() && n === null)) {
      invalid = t("flash.addressHint");
      return;
    }
    const r = await run("read", "target.flash.read", { path: target, address: at, length: n });
    if (r) {
      result = { kind: "read", data: r };
      readPath = target;
    }
  }

  async function upload(e: Event) {
    const file = (e.currentTarget as HTMLInputElement).files?.[0];
    (e.currentTarget as HTMLInputElement).value = "";
    if (!file || !(await allowed(app))) return;
    const r = await app.act("target.flash.upload", { name: file.name, data_base64: base64(await file.arrayBuffer()) });
    if (r) {
      path = r.path;
      note = t("flash.uploaded", { name: file.name, size: size(r.bytes) });
    }
  }

  const setInvalid = (message: string) => (invalid = message);
  const setNote = (message: string) => (note = message);
</script>

<div class="pane">
  {#if !connected}
    <Empty need="connected" />
    <div class="form">
      <FlashProtection {online} {connected} {busy} {run} onInvalid={setInvalid} bind:ob bind:obEdit bind:obResult />
    </div>
  {:else}
    <div class="form">
      <label for="flash-path">{$i18n("flash.file")}</label>
      <div class="row">
        <input id="flash-path" class="mono grow" bind:value={path} placeholder={$i18n("flash.filePlaceholder")} spellcheck="false" />
        <button class="small" title={$i18n("flash.browseTip")} onclick={() => fileInput?.click()}>{$i18n("flash.browse")}</button>
        <input type="file" hidden accept=".elf,.axf,.out,.hex,.ihex,.ihx,.bin" bind:this={fileInput} onchange={upload} />
        <button class="small" disabled={!$dbg.elf || path === $dbg.elf} title={$i18n("flash.useElfTip", { path: $dbg.elf ?? "-" })}
                onclick={() => (path = $dbg.elf ?? path)}>{$i18n("flash.useElf")}</button>
      </div>
      <!-- 番地の欄は常に出す（形式を拡張子で推さない。BIN か否かはデーモンが中身で決める） -->
      <label for="flash-address">{$i18n("flash.address")}</label>
      <div class="row">
        <input id="flash-address" class="mono num" bind:value={address} title={$i18n("flash.addressHint")} />
        <span class="muted">{$i18n("flash.addressHint")}</span>
      </div>
      <span></span>
      <div class="row options">
        <label title={$i18n("flash.eraseTip")}>{$i18n("flash.erase")}
          <select bind:value={options.erase}>
            <option value="auto">{$i18n("flash.eraseAuto")}</option>
            <option value="sector">{$i18n("flash.eraseSector")}</option>
            <option value="chip">{$i18n("flash.eraseChip")}</option>
          </select></label>
        <label title={$i18n("flash.verifyTip")}><input type="checkbox" bind:checked={options.verify} /> {$i18n("flash.verify")}</label>
        <label title={$i18n("flash.smartTip")}><input type="checkbox" bind:checked={options.smart} /> {$i18n("flash.smart")}</label>
        <label title={$i18n("flash.resetTip")}><input type="checkbox" bind:checked={options.reset} /> {$i18n("flash.reset")}</label>
      </div>
      <span></span>
      <div class="row">
        <button class="primary" disabled={!online || !path.trim() || !!busy} title={$i18n("flash.programTip")}
                onclick={program}>{$i18n("flash.program")}</button>
        <button disabled={!online || !path.trim() || !!busy} title={$i18n("flash.verifyTip")}
                onclick={verifyNow}>{$i18n("flash.verify")}</button>
        {#if !path.trim()}<span class="muted">{$i18n("flash.noFile")}</span>{/if}
      </div>
      <label for="flash-erase">{$i18n("flash.erase")}</label>
      <div class="row">
        <select id="flash-erase" bind:value={eraseMode}>
          <option value="range">{$i18n("flash.eraseRange")}</option>
          <option value="chip">{$i18n("flash.eraseChip")}</option>
          <option value="mass">{$i18n("flash.eraseMass")}</option>
        </select>
        {#if eraseMode === "range"}
          <input class="mono num" bind:value={eraseAddress} aria-label={$i18n("flash.address")} aria-required="true"
                 placeholder={$i18n("flash.address")} title={$i18n("flash.address")} /><Req />
          <input class="mono len" bind:value={eraseLength} aria-label={$i18n("flash.length")} aria-required="true"
                 placeholder={$i18n("flash.length")} title={$i18n("flash.length")} /><Req />
        {/if}
        <button disabled={!online || !!busy} onclick={erase}>{$i18n("flash.eraseNow")}</button>
      </div>
      <label for="flash-read" title={$i18n("flash.readBackTip")}>{$i18n("flash.readBack")}</label>
      <div class="row">
        <input id="flash-read" class="mono grow" bind:value={readPath} placeholder={defaultReadPath($dbg.elf)}
               aria-label={$i18n("flash.saveTo")} title={$i18n("flash.saveTo")} spellcheck="false" />
        <input class="mono num" bind:value={readAddress} placeholder={$i18n("flash.address")} aria-label={$i18n("flash.address")} />
        <input class="mono len" bind:value={readLength} placeholder={$i18n("flash.length")} aria-label={$i18n("flash.length")} />
        <button disabled={!online || !!busy} title={$i18n("flash.readBackTip")} onclick={readBack}>{$i18n("flash.readBack")}</button>
      </div>
    </div>

    {#if progress}
      {@const f = progress.fraction}
      <!-- 段（phase）と段の中の進み・経過は通知の値のまま（段を合成しない・残りの時間を見積もらない） -->
      <div class="progress" role="progressbar" aria-valuenow={Math.round(f * 100)} aria-valuemin={0} aria-valuemax={100}>
        <span>{$i18n(`flash.phase.${progress.phase}` as Key)}</span>
        <span class="bar"><span style:width="{f * 100}%"></span></span>
        <span class="mono">{Math.round(f * 100)}% · {seconds(progress.elapsed_s)}</span>
      </div>
    {/if}
    {#if invalid}<div class="line bad">{invalid}</div>{/if}
    {#if note}<div class="line muted">{note}</div>{/if}

    {#if result}<FlashResult {result} {path} />{/if}

    <FlashRegions {online} {busy} {run} onInvalid={setInvalid} onNote={setNote} />

    <FlashProtection {online} {connected} {busy} {run} onInvalid={setInvalid} bind:ob bind:obEdit bind:obResult />

    <FlashPacks {online} bind:busy />

    <FlashBench {online} {busy} {run} onInvalid={setInvalid} />
  {/if}
</div>

<style>
  .form { display: grid; grid-template-columns: max-content 1fr; gap: 6px 10px; padding: 8px 10px; align-items: center;
          border-bottom: 1px solid var(--line); }
  .form > label { color: var(--muted); font-size: 12px; }
  .row { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; min-width: 0; }
  .options label { display: inline-flex; gap: 4px; align-items: center; }
  .grow { flex: 1; min-width: 120px; }
  .num { width: 110px; }
  .len { width: 70px; }
  .progress { display: flex; gap: 8px; align-items: center; padding: 6px 10px; }
  .bar { flex: 1; height: 6px; background: var(--line); border-radius: 3px; overflow: hidden; }
  .bar span { display: block; height: 100%; background: var(--accent); transition: width 0.1s linear; }
  .line { padding: 4px 10px; }
  .bad { color: var(--bad); }
</style>
