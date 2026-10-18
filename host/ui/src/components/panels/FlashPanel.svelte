<script lang="ts">
  // Flash（J-Flash Lite の型）: データ ファイル（デーモンの PC のパス・この PC から選んで送る・読み込んだ ELF）、
  // 消去（auto / sector / chip）・ベリファイ・変更なしを省略・リセットして実行の設定、Program・Verify・Erase・Read Back、
  // 進捗（通知 flash.progress。AI の書き込みも見える）、結果（段ごとの時間・速度・ベリファイ）。詳細に Flash の領域・
  // CMSIS パック（探す・入れるはネットに出るので確認）・速度の計測
  import { onDestroy } from "svelte";
  import { shared } from "../../lib/context";
  import { base64 } from "../../lib/debugger";
  import { frequency } from "../../lib/format";
  import {
    algoParams, baseName, defaultReadPath, emptyAlgo, eraseMethodKey, formatOf, loadOptions, overall, parseNumber,
    programParams, programStages, remaining, sectorsText, seconds, size, speed, verifyParams, type AlgoForm,
    type FlashOptions, type Progress, type Stage,
  } from "../../lib/flash";
  import { allowed } from "../../lib/guard";
  import { confirmer } from "../../lib/confirm";
  import { i18n, t, type Key } from "../../lib/i18n";
  import { load, save } from "../../lib/prefs";
  import Empty from "./Empty.svelte";
  import { hex32 } from "./format";

  const { app, conn, dbg, ui } = shared();

  let path = $state(load("flash.path", ""));
  let address = $state(load("flash.address", "0x08000000"));
  let options = $state<FlashOptions>(loadOptions(load("flash.options", "{}")));
  let eraseMode = $state<"range" | "chip" | "mass">("range");
  let eraseAddress = $state("0x08000000");
  let eraseLength = $state("16K");
  let readPath = $state(load("flash.readPath", ""));
  let readAddress = $state("");
  let readLength = $state("");
  let busy = $state<string | null>(null);
  let progress = $state<Progress | null>(null);
  let note = $state<string | null>(null);              // 送った・保存した等の一行
  let invalid = $state<string | null>(null);           // 欄の誤り
  let result = $state<{ kind: "program" | "verify" | "erase" | "read"; data: any } | null>(null);
  let regions = $state<any[] | null>(null);
  let algos = $state<any[]>([]);                       // target.flash.algo.add で足したアルゴリズム
  let algo = $state<AlgoForm>(emptyAlgo());
  let packs = $state<any[] | null>(null);
  let packPath = $state(load("flash.packPath", ""));
  let targets = $state<string[]>([]);                  // 入れた・読み込んだ Pack のターゲット名（接続に使える）
  let pattern = $state("");
  let updateIndex = $state(false);
  let found = $state<any[] | null>(null);
  let benchSector = $state("");
  let bench = $state<any>(null);
  let fileInput = $state<HTMLInputElement>();

  const online = $derived($app.link === "open");
  const connected = $derived(!!$app.target?.state && $app.target.state !== "DISCONNECTED");
  const isBin = $derived(formatOf(path) === "bin");
  const stages = $derived<Stage[]>(result?.kind === "program" ? programStages(result.data) : []);
  const verify = $derived(result?.kind === "program" ? result.data?.verify : result?.kind === "verify" ? result.data : null);

  $effect(() => save("flash.path", path));
  $effect(() => save("flash.address", address));
  $effect(() => save("flash.options", JSON.stringify(options)));
  $effect(() => save("flash.readPath", readPath));
  $effect(() => save("flash.packPath", packPath));
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

  async function run(kind: string, method: Parameters<typeof app.act>[0], params: any) {
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

  async function loadRegions() {
    try {
      const r = await conn.call("target.flash.regions", {}, { quiet: true });
      regions = r.regions;
      algos = r.algorithms ?? [];
    } catch {
      regions = null;
    }
  }

  /** 外付けのアルゴリズム（.FLM）で Flash の領域を足す（未接続なら覚えておき、接続後の最初の操作で足す） */
  async function addAlgo() {
    let params;
    try {
      params = algoParams(algo);
    } catch (e) {
      invalid = (e as Error).message === "flm" ? t("flash.needFlm") : t("flash.numberHint");
      return;
    }
    const r = await run("algo", "target.flash.algo.add", params);
    if (r) {
      note = r.pending ? `${t("flash.algoAdded", { name: r.name })} · ${t("flash.algoPending")}`
                       : t("flash.algoAdded", { name: r.name });
      algo = emptyAlgo();
      await loadRegions();
    }
  }

  async function removeAlgo(name: string) {
    if (await run("algo", "target.flash.algo.remove", { name })) await loadRegions();
  }

  /** デーモンの PC の .pack ファイルのデバイスをターゲットとして登録する */
  async function loadPackFile() {
    const p = packPath.trim();
    if (!p) return;
    busy = "packs";
    try {
      if (!(await allowed(app))) return;
      const r = await app.act("target.pack.load", { path: p });
      if (r) targets = r.targets ?? [];
    } finally {
      busy = null;
    }
  }

  /** そのターゲット名で接続のダイアログを開く（ダイアログは覚えたターゲット名を読む） */
  function useTarget(name: string) {
    save("connect.target", name);
    ui.open("connect");
  }

  async function loadPacks() {
    try {
      packs = (await conn.call("target.pack.list", {}, { quiet: true })).packs;
    } catch {
      packs = null;
    }
  }

  async function findPacks() {
    if (!pattern.trim() || !(await confirmer.ask(t("flash.confirmNet", { what: pattern.trim() }),
                                                 { ok: t("flash.download") }))) return;
    busy = "packs";
    try {
      const r = await app.act("target.pack.find", { pattern: pattern.trim(), update: updateIndex });
      found = r?.parts ?? null;
    } finally {
      busy = null;
    }
  }

  async function install(part: string) {
    if (!(await allowed(app)) || !(await confirmer.ask(t("flash.confirmNet", { what: part }), { ok: t("flash.download") }))) {
      return;
    }
    busy = "packs";
    try {
      const r = await app.act("target.pack.install", { pattern: part, update: updateIndex });
      if (r) {
        targets = r.targets ?? [];
        await loadPacks();
        found = found?.map((p) => (p.part === part ? { ...p, installed: true } : p)) ?? null;
      }
    } finally {
      busy = null;
    }
  }

  async function benchmark() {
    const sector = benchSector.trim() ? parseNumber(benchSector) : null;
    if (benchSector.trim() && sector === null) {
      invalid = t("flash.addressHint");
      return;
    }
    const r = await run("benchmark", "target.flash.benchmark", sector !== null ? { program_address: sector } : {});
    if (r) bench = r;
  }

  const benchRows = (b: any): [Key, any][] => [
    ["flash.bench.ramWrite", b.ram?.write], ["flash.bench.ramRead", b.ram?.read], ["flash.bench.flashRead", b.flash_read],
    ["flash.bench.flashErase", b.flash_program?.erase], ["flash.bench.flashProgram", b.flash_program?.program],
    ["flash.bench.flashTotal", b.flash_program?.total],
  ].filter(([, r]) => r) as [Key, any][];

  /** 消去の方法の名前（知らない方法はそのまま） */
  const methodText = (m: string | undefined) => {
    const k = eraseMethodKey(m);
    return k ? $i18n(k) : m ?? "";
  };
  /** 消したセクタの数（セクタごとの消去なら数だけ、チップ全体等なら方法も） */
  const sectorsNote = (n: number, method: string | undefined) => !method || method === "sector"
    ? $i18n("flash.sectorsOnly", { n }) : $i18n("flash.sectorsNote", { n, method: methodText(method) });
  const stageNote = (s: Stage) => s.sectors !== undefined ? sectorsNote(s.sectors, s.method)
    : s.skipped ? $i18n("flash.skippedNote", { size: size(s.skipped) }) : "";
</script>

<div class="pane">
  {#if !connected}
    <Empty need="connected" />
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
      {#if isBin}
        <label for="flash-address">{$i18n("flash.address")}</label>
        <div class="row">
          <input id="flash-address" class="mono num" bind:value={address} title={$i18n("flash.addressHint")} />
          <span class="muted">{$i18n("flash.addressHint")}</span>
        </div>
      {/if}
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
          <input class="mono num" bind:value={eraseAddress} aria-label={$i18n("flash.address")} title={$i18n("flash.address")} />
          <input class="mono len" bind:value={eraseLength} aria-label={$i18n("flash.length")} title={$i18n("flash.length")} />
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
      {@const f = overall(progress, options.verify)}
      {@const left = remaining(progress, options.verify)}
      <div class="progress" role="progressbar" aria-valuenow={Math.round(f * 100)} aria-valuemin={0} aria-valuemax={100}>
        <span>{$i18n(`flash.phase.${progress.phase}` as Key)}</span>
        <span class="bar"><span style:width="{f * 100}%"></span></span>
        <span class="mono">{Math.round(f * 100)}% · {seconds(progress.elapsed_s)}</span>
        {#if left}<span class="muted mono">{$i18n("flash.remaining", { t: seconds(left) })}</span>{/if}
      </div>
    {/if}
    {#if invalid}<div class="line bad">{invalid}</div>{/if}
    {#if note}<div class="line muted">{note}</div>{/if}

    {#if result}
      <section class="result">
        {#if result.kind === "program"}
          <div class="line mono">{baseName(result.data.image?.source ?? path)} · {result.data.image?.format?.toUpperCase()} ·
            {size(result.data.image?.bytes)}</div>
          <table class="grid">
            <thead><tr><th>{$i18n("flash.stage")}</th><th>{$i18n("flash.size")}</th><th>{$i18n("flash.time")}</th>
              <th>{$i18n("flash.speed")}</th><th></th></tr></thead>
            <tbody>
              {#each stages as s (s.stage)}
                <tr class:total={s.stage === "total"}>
                  <td>{$i18n(`flash.stage.${s.stage}` as Key)}</td>
                  <td class="mono">{size(s.bytes)}</td><td class="mono">{s.stage === "ram" ? "" : seconds(s.seconds)}</td>
                  <td class="mono">{speed(s.bps)}</td><td class="muted">{stageNote(s)}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        {:else if result.kind === "erase"}
          <div class="line">
            {#if result.data.start !== undefined}
              {$i18n("flash.erasedRange", { start: hex32(result.data.start), end: hex32(result.data.end) })}
              · {sectorsNote(result.data.sectors, "sector")}
            {:else}
              {$i18n("flash.erasedAll", { method: methodText(result.data.method) })}
            {/if}
            {#if result.data.rate}· <span class="mono">{seconds(result.data.rate.seconds)} · {speed(result.data.rate.bytes_per_s)}</span>{/if}
            {#if result.data.seconds !== undefined}· <span class="mono">{seconds(result.data.seconds)}</span>{/if}
          </div>
        {:else if result.kind === "read"}
          <div class="line">{$i18n("flash.readDone", { path: result.data.path, size: size(result.data.bytes),
                                                        crc: hex32(result.data.crc32) })}
            · <span class="mono">{hex32(result.data.address)} · {seconds(result.data.read?.seconds)} · {speed(result.data.read?.bytes_per_s)}</span></div>
        {/if}
        {#if verify}
          {#if verify.match}
            <div class="line ok">{$i18n("flash.verifyOk")} · <span class="mono">{size(verify.bytes)} · {speed(verify.read?.bytes_per_s)}</span></div>
          {:else}
            <div class="line bad">{$i18n("flash.verifyBad", { n: verify.differing_bytes })}</div>
            <table class="grid">
              <thead><tr><th>{$i18n("common.address")}</th><th>{$i18n("flash.expected")}</th><th>{$i18n("flash.actual")}</th></tr></thead>
              <tbody>
                {#each verify.differences.slice(0, 16) as d (d.address)}
                  <tr><td class="mono">{hex32(d.address)}</td><td class="mono hexbytes">{d.expected}</td>
                    <td class="mono hexbytes bad">{d.actual}</td></tr>
                {/each}
              </tbody>
            </table>
            {#if verify.differences.length > 16 || verify.more_differences}
              <div class="line muted">{$i18n("flash.more", { n: verify.differences.length - 16 + (verify.more_differences ?? 0) })}</div>
            {/if}
          {/if}
        {/if}
      </section>
    {/if}

    <details ontoggle={(e) => (e.currentTarget as HTMLDetailsElement).open && loadRegions()}>
      <summary>{$i18n("flash.regions")}</summary>
      {#if regions}
        <table class="grid">
          <thead><tr><th>{$i18n("common.name")}</th><th>{$i18n("flash.start")}</th><th>{$i18n("flash.size")}</th>
            <th>{$i18n("flash.sectors")}</th><th>{$i18n("flash.algorithm")}</th><th>{$i18n("flash.boot")}</th></tr></thead>
          <tbody>
            {#each regions as r (r.name)}
              <tr class:muted={!r.usable}>
                <td class="mono">{r.name}</td><td class="mono">{hex32(r.start)}</td><td class="mono">{size(r.size)}</td>
                <td class="mono">{sectorsText(r.sectors)}</td><td class="mono">{r.algorithm}</td><td>{r.boot ? "✓" : ""}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      {/if}
      <div class="sub" title={$i18n("flash.algosTip")}>{$i18n("flash.algos")}</div>
      {#if algos.length}
        <table class="grid">
          <thead><tr><th>{$i18n("common.name")}</th><th>FLM</th><th>{$i18n("flash.start")}</th><th>{$i18n("flash.size")}</th>
            <th></th></tr></thead>
          <tbody>
            {#each algos as a (a.name)}
              <tr>
                <td class="mono">{a.name}</td><td class="mono">{baseName(a.flm)}</td>
                <td class="mono">{a.address !== null && a.address !== undefined ? hex32(a.address) : "-"}</td>
                <td class="mono">{size(a.size)}</td>
                <td><button class="small" disabled={!online || !!busy} onclick={() => removeAlgo(a.name)}>
                  {$i18n("common.remove")}</button></td>
              </tr>
            {/each}
          </tbody>
        </table>
      {/if}
      <form class="row pad" onsubmit={(e) => { e.preventDefault(); void addAlgo(); }}>
        <input class="mono grow" bind:value={algo.flm} placeholder={$i18n("flash.algoFlm")} aria-label={$i18n("flash.algoFlm")}
               spellcheck="false" />
        <input class="mono num" bind:value={algo.address} placeholder={$i18n("flash.address")} aria-label={$i18n("flash.address")} />
        <input class="mono len" bind:value={algo.size} placeholder={$i18n("flash.size")} aria-label={$i18n("flash.size")} />
        <input class="mono num" bind:value={algo.ramAddress} placeholder={$i18n("flash.ramAddress")}
               aria-label={$i18n("flash.ramAddress")} />
        <input class="mono len" bind:value={algo.ramSize} placeholder={$i18n("flash.ramSize")} aria-label={$i18n("flash.ramSize")} />
        <input class="len" bind:value={algo.name} placeholder={$i18n("common.name")} aria-label={$i18n("common.name")} />
        <button class="small" disabled={!online || !algo.flm.trim() || !!busy} title={$i18n("flash.algosTip")}>
          {$i18n("common.add")}</button>
      </form>
    </details>

    <details ontoggle={(e) => (e.currentTarget as HTMLDetailsElement).open && loadPacks()}>
      <summary title={$i18n("flash.packsTip")}>{$i18n("flash.packs")}</summary>
      {#if packs?.length}
        <ul class="packs mono">{#each packs as p (p.pack)}<li>{p.pack} <span class="muted">{p.version}</span></li>{/each}</ul>
      {:else if packs}
        <div class="line muted">{$i18n("flash.packNone")}</div>
      {/if}
      <div class="row pad">
        <input class="grow" bind:value={pattern} placeholder={$i18n("flash.packPattern")}
               onkeydown={(e) => e.key === "Enter" && findPacks()} />
        <label><input type="checkbox" bind:checked={updateIndex} /> {$i18n("flash.packUpdate")}</label>
        <button class="small" disabled={!online || !pattern.trim() || !!busy} onclick={findPacks}>{$i18n("flash.packFind")}</button>
      </div>
      {#if found}
        <table class="grid">
          <thead><tr><th>{$i18n("flash.part")}</th><th>{$i18n("flash.vendor")}</th><th>{$i18n("flash.pack")}</th>
            <th>{$i18n("flash.version")}</th><th></th></tr></thead>
          <tbody>
            {#each found as p (p.part)}
              <tr>
                <td class="mono">{p.part}</td><td>{p.vendor}</td><td class="mono">{p.pack}</td><td class="mono">{p.version}</td>
                <td>{#if p.installed}<span class="muted">{$i18n("flash.packInstalled")}</span>{:else}
                  <button class="small" disabled={!!busy} onclick={() => install(p.part)}>{$i18n("flash.packInstall")}</button>{/if}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      {/if}
      <form class="row pad" onsubmit={(e) => { e.preventDefault(); void loadPackFile(); }}>
        <input class="mono grow" bind:value={packPath} placeholder={$i18n("flash.packPath")} aria-label={$i18n("flash.packPath")}
               spellcheck="false" />
        <button class="small" disabled={!online || !packPath.trim() || !!busy} title={$i18n("flash.packLoadTip")}>
          {$i18n("flash.packLoad")}</button>
      </form>
      {#if targets.length}
        <div class="row pad targets">
          <span class="muted">{$i18n("flash.packTargets")}</span>
          {#each targets as name (name)}
            <span class="mono">{name}</span>
            <button class="small" title={$i18n("flash.useTargetTip")} onclick={() => useTarget(name)}>
              {$i18n("flash.useTarget")}</button>
          {/each}
        </div>
      {/if}
    </details>

    <details>
      <summary title={$i18n("flash.benchmarkTip")}>{$i18n("flash.benchmark")}</summary>
      <div class="row pad">
        <label>{$i18n("flash.benchSector")} <input class="mono num" bind:value={benchSector} placeholder="0x08060000" /></label>
        <button class="small" disabled={!online || !!busy} title={$i18n("flash.benchmarkTip")} onclick={benchmark}>
          {$i18n("flash.benchRun")}</button>
        {#if bench?.frequency_hz}<span class="muted mono">{$i18n("flash.frequency", { f: frequency(bench.frequency_hz) })}</span>{/if}
      </div>
      {#if bench}
        <table class="grid">
          <thead><tr><th>{$i18n("flash.stage")}</th><th>{$i18n("flash.size")}</th><th>{$i18n("flash.time")}</th>
            <th>{$i18n("flash.speed")}</th></tr></thead>
          <tbody>
            {#each benchRows(bench) as [k, r] (k)}
              <tr><td>{$i18n(k)}</td><td class="mono">{size(r.bytes)}</td><td class="mono">{seconds(r.seconds)}</td>
                <td class="mono">{speed(r.bytes_per_s)}</td></tr>
            {/each}
          </tbody>
        </table>
      {/if}
    </details>
  {/if}
</div>

<style>
  .form { display: grid; grid-template-columns: max-content 1fr; gap: 6px 10px; padding: 8px 10px; align-items: center;
          border-bottom: 1px solid var(--line); }
  .form > label { color: var(--muted); font-size: 12px; }
  .row { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; min-width: 0; }
  .row.pad { padding: 4px 10px; }
  .options label { display: inline-flex; gap: 4px; align-items: center; }
  .grow { flex: 1; min-width: 120px; }
  .num { width: 110px; }
  .len { width: 70px; }
  .progress { display: flex; gap: 8px; align-items: center; padding: 6px 10px; }
  .bar { flex: 1; height: 6px; background: var(--line); border-radius: 3px; overflow: hidden; }
  .bar span { display: block; height: 100%; background: var(--accent); transition: width 0.1s linear; }
  .line { padding: 4px 10px; }
  .result { border-bottom: 1px solid var(--line); padding-bottom: 4px; }
  tr.total td { font-weight: 600; }
  .ok { color: var(--ok); }
  .bad { color: var(--bad); }
  .hexbytes { word-break: break-all; }
  details { border-bottom: 1px solid var(--line); }
  summary { padding: 4px 10px; cursor: pointer; color: var(--muted); font-size: 12px; font-weight: 600; }
  .packs { list-style: none; margin: 0; padding: 2px 10px; }
  .sub { padding: 6px 10px 2px; color: var(--muted); font-size: 12px; }
  .targets { font-size: 12px; }
</style>
