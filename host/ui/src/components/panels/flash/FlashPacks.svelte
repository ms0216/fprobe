<script lang="ts">
  // CMSIS パック: 入っているパック、探す・入れる（ネットに出るので確認）、PC の .pack ファイルを読む、ターゲット名で接続を開く
  import { shared } from "../../../lib/context";
  import type { Result } from "../../../lib/api";
  import { allowed } from "../../../lib/guard";
  import { confirmer } from "../../../lib/confirm";
  import { i18n, t } from "../../../lib/i18n";
  import { load, save } from "../../../lib/prefs";

  let { online, busy = $bindable() }: { online: boolean; busy: string | null } = $props();

  const { app, conn, ui } = shared();

  let packs = $state<Result<"target.pack.list">["packs"] | null>(null);
  let packPath = $state(load("flash.packPath", ""));
  let targets = $state<string[]>([]);                  // 入れた・読み込んだ Pack のターゲット名（接続に使える）
  let pattern = $state("");
  let updateIndex = $state(false);
  let found = $state<Result<"target.pack.find">["parts"] | null>(null);

  $effect(() => save("flash.packPath", packPath));

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
</script>

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

<style>
  .row { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; min-width: 0; }
  .row.pad { padding: 4px 10px; }
  .grow { flex: 1; min-width: 120px; }
  .line { padding: 4px 10px; }
  details { border-bottom: 1px solid var(--line); }
  summary { padding: 4px 10px; cursor: pointer; color: var(--muted); font-size: 12px; font-weight: 600; }
  .packs { list-style: none; margin: 0; padding: 2px 10px; }
  .targets { font-size: 12px; }
</style>
