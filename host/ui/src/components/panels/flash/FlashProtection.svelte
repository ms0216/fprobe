<script lang="ts">
  // Flash の保護（オプションバイト・保護の解除）: 名前ごとの値と説明。全消去・保護をかける書き込みは、人が明示して許す
  import { confirmer } from "../../../lib/confirm";
  import { i18n, t } from "../../../lib/i18n";
  import { changedFields } from "../../../lib/protection";
  import type { Result } from "../../../lib/api";
  import type { FlashRun } from "../../../lib/flash";
  import { hex32 } from "../format";

  // ob・obEdit・obResult は親が持つ（接続の有無で部品が作り直されても表と結果を失わないため）
  let { online, connected, busy, run, onInvalid, ob = $bindable(null), obEdit = $bindable({}), obResult = $bindable(null) }: {
    online: boolean; connected: boolean; busy: string | null; run: FlashRun; onInvalid: (message: string) => void;
    ob: Result<"target.option_bytes.read"> | null; obEdit: Record<string, string>; obResult: any;
  } = $props();

  let allowMassErase = $state(false);
  let allowProtect = $state(false);

  async function readOptionBytes() {
    const r = await run("protect", "target.option_bytes.read", {});
    if (r) {
      ob = r;
      obEdit = Object.fromEntries(Object.entries(r.fields ?? {}).map(([k, v]) => [k, String(v)]));
    }
  }

  async function writeOptionBytes() {
    const built = changedFields(ob?.fields ?? {}, obEdit);
    if ("error" in built) {
      onInvalid(built.error === "nothing" ? t("protect.nothing") : t("protect.badField", { field: built.error }));
      return;
    }
    const params = { fields: built.fields, confirm_mass_erase: allowMassErase, confirm_protect: allowProtect };
    if (!(await confirmer.ask(t("protect.confirmWrite", { params: JSON.stringify(params) }),
                              { ok: t("protect.write"), danger: true }))) return;
    const r = await run("protect", "target.option_bytes.write", params);
    allowMassErase = allowProtect = false;               // 危ない変更を許す印は 1 回ごと
    if (r) {
      obResult = r;
      await readOptionBytes();                            // 書いた後の値（表の値を新しく）
    }
  }

  async function unlock() {
    if (!(await confirmer.ask(t("protect.confirmUnlock"), { ok: t("protect.unlock"), danger: true }))) return;
    const r = await run("protect", "target.unlock", { confirm_mass_erase: allowMassErase });
    allowMassErase = allowProtect = false;
    if (r) obResult = r;
  }
</script>

<details>
  <summary title={$i18n("protect.tip")}>{$i18n("protect.title")}</summary>
  <div class="row pad">
    <button class="small" disabled={!online || !connected || !!busy} onclick={() => void readOptionBytes()}>{$i18n("protect.read")}</button>
    <label title={$i18n("protect.allowMassEraseTip")}><input type="checkbox" bind:checked={allowMassErase} />
      {$i18n("protect.allowMassErase")}</label>
    <label title={$i18n("protect.allowProtectTip")}><input type="checkbox" bind:checked={allowProtect} />
      {$i18n("protect.allowProtect")}</label>
    <button class="small danger" disabled={!online || !!busy} title={$i18n("protect.unlockTip")}
            onclick={() => void unlock()}>{$i18n("protect.unlock")}</button>
  </div>
  {#if ob}
    <div class="line mono">{ob.part?.name ?? ""} {ob.family ?? ""} · RDP {ob.rdp_level ?? "?"} · {hex32(ob.address ?? 0)} = {ob.raw != null ? hex32(ob.raw) : ""}</div>
    <table class="grid">
      <thead><tr><th>{$i18n("common.name")}</th><th>{$i18n("protect.value")}</th><th>{$i18n("protect.description")}</th></tr></thead>
      <tbody>
        {#each Object.keys(ob.fields ?? {}) as name (name)}
          <tr>
            <td class="mono">{name}</td>
            <td><input class="mono num" bind:value={obEdit[name]} aria-label={name} /></td>
            <td class="muted">{ob.descriptions?.[name] ?? ""}</td>
          </tr>
        {/each}
      </tbody>
    </table>
    <div class="row pad">
      <button class="small danger" disabled={!online || !connected || !!busy} onclick={() => void writeOptionBytes()}>{$i18n("protect.write")}</button>
    </div>
  {/if}
  {#if obResult}<pre class="mono line">{JSON.stringify(obResult, null, 2)}</pre>{/if}
</details>

<style>
  .row { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; min-width: 0; }
  .row.pad { padding: 4px 10px; }
  .num { width: 110px; }
  .line { padding: 4px 10px; }
  details { border-bottom: 1px solid var(--line); }
  summary { padding: 4px 10px; cursor: pointer; color: var(--muted); font-size: 12px; font-weight: 600; }
</style>
