<script lang="ts">
  // API: AI ができる操作（MCP の道具と同じ範囲）を、人も画面から引数を入れて呼べる（画面に全 API を入れる、段階 A。
  // docs/superpowers/specs/2026-10-22-ui-all-apis-design.md）。欄は仕様（host/api/fprobe_api.json）の引数の型から作る。
  // 呼んだ操作は利用者 human として Activity と記録に残る。read は確かめない、control は確かめ、元に戻せない操作は赤
  import { callApi } from "../../lib/apicall";
  import { METHODS } from "../../lib/apispec";
  import { buildParams, fieldsOf, valuesOf, type ApiMethod,
           type FieldError, type FieldValues } from "../../lib/apiform";
  import { shared } from "../../lib/context";
  import { favorites } from "../../lib/favorites";
  import { i18n, t, type Key } from "../../lib/i18n";

  const { app, conn, ui, dock } = shared();

  let query = $state("");
  let chosen = $state<ApiMethod | null>(null);
  let values = $state<FieldValues>({});
  let unrestored = $state<string[]>([]);           // Activity の引数で要約されていて入れなかった欄
  let errors = $state<Record<string, FieldError>>({});
  let busy = $state(false);
  let result = $state<{ method: string; ok: boolean; text: string } | null>(null);
  let favoriteId = $state<string | null>(null);   // お気に入りの「API で開く」から: 直したら置き換えられる

  const fields = $derived(chosen ? fieldsOf(chosen.params) : []);
  const groups = $derived.by(() => {
    const q = query.trim().toLowerCase();
    const hit = METHODS.filter((m) => !q || m.name.toLowerCase().includes(q) || m.summary.toLowerCase().includes(q));
    const out = new Map<string, ApiMethod[]>();
    for (const m of hit) {
      const head = m.name.split(".")[0];
      out.set(head, [...(out.get(head) ?? []), m]);
    }
    return [...out];
  });

  // Activity の行から「API で開く」（同じ呼び出しを人がやり直す。要約されて型が合わない値は入れず、その欄を知らせる）
  $effect(() => {
    if (!$ui.api) return;
    const want = ui.takeApi();
    if (!want) return;
    const m = METHODS.find((x) => x.name === want.method);
    if (!m) return;
    const restored = valuesOf(fieldsOf(m.params), want.params);
    pick(m, restored.values);
    unrestored = restored.unrestored;
    favoriteId = want.favorite ?? null;
  });

  function pick(m: ApiMethod, initial: FieldValues = {}) {
    chosen = m;
    values = initial;
    errors = {};
    unrestored = [];
    result = null;
    favoriteId = null;
  }

  function setNull(name: string, on: boolean) {
    values[name] = on ? null : "";
  }

  // 言語の切り替えに追う: 表（$i18n）を引数で受け、テンプレートが表の変化で描き直す
  const errorMessage = (tr: typeof t, e: FieldError): string => tr(`api.err.${e.code}` as Key, { limit: String(e.limit ?? "") });

  /** 欄から引数を組み立てる（誤りは欄に出して null） */
  function params(): Record<string, unknown> | null {
    const built = buildParams(fields, values);
    if ("errors" in built) {
      errors = built.errors;
      return null;
    }
    errors = {};
    return built.params;
  }

  async function call() {
    const m = chosen;                                  // 呼んでいる間にメソッドを替えても、結果は呼んだメソッドに付ける
    const p = m && params();
    if (!m || !p) return;
    busy = true;
    try {
      const r = await callApi(m, p, { conn, app });
      if (r) result = { method: m.name, ...r };
    } finally {
      busy = false;
    }
  }

  function updateFavorite() {
    const p = chosen && params();
    if (!chosen || !p || !favoriteId) return;
    favorites.update(favoriteId, chosen.name, p);
    dock.show("favorites");
  }

  function addFavorite() {
    const p = chosen && params();
    if (!chosen || !p) return;
    favorites.add(chosen.name, p);
    dock.show("favorites");
  }

  const optionsOf = (f: { kind: string; options?: unknown[] }) => (f.kind === "boolean" ? [true, false] : (f.options ?? []));

  const placeholder = (tr: typeof t, f: { default?: unknown; kind: string }) =>
    f.default !== undefined ? tr("api.defaultIs", { value: JSON.stringify(f.default) })
      : f.kind === "json" ? tr("api.jsonHint") : "";
</script>

<div class="pane api">
  <div class="list">
    <input class="search" type="search" bind:value={query} placeholder={$i18n("api.search")}
           aria-label={$i18n("api.search")} />
    <div class="tree">
      {#each groups as [head, items] (head)}
        <details open={!!query.trim() || items.some((m) => m.name === chosen?.name)}>
          <summary class="mono">{head} <span class="muted">{items.length}</span></summary>
          {#each items as m (m.name)}
            <button class="item mono" class:selected={chosen?.name === m.name} title={m.summary}
                    onclick={() => pick(m)}>
              <span class="name">{m.name}</span>
              {#if m.irreversible}<span class="tag danger">{$i18n("api.irreversible")}</span>
              {:else if m.kind === "control"}<span class="tag">{$i18n("api.control")}</span>{/if}
            </button>
          {/each}
        </details>
      {:else}
        <div class="muted empty">{$i18n("api.none")}</div>
      {/each}
    </div>
  </div>
  <div class="form">
    {#if chosen}
      <div class="head">
        <strong class="mono">{chosen.name}</strong>
        <span class="tag" class:danger={chosen.irreversible}>
          {$i18n(chosen.irreversible ? "api.irreversible" : chosen.kind === "control" ? "api.control" : "api.read")}</span>
      </div>
      <p class="summary">{chosen.summary}</p>
      {#if fields.length === 0}<p class="muted">{$i18n("api.noParams")}</p>{/if}
      {#if unrestored.length}
        <p class="err">{$i18n("api.unrestored", { names: unrestored.join(", ") })}</p>
      {/if}
      {#each fields as f (f.name)}
        <div class="field">
          <label class="label mono" for={`api-${f.name}`} title={f.description ?? ""}>{f.name}{#if f.required}<span
            class="req" title={$i18n("api.requiredTip")}>*</span>{/if}</label>
          <div class="input">
            {#if values[f.name] === null}
              <input id={`api-${f.name}`} class="mono" value="null" disabled />
            {:else if f.kind === "boolean" || f.kind === "enum"}
              <select id={`api-${f.name}`} bind:value={values[f.name]} aria-invalid={!!errors[f.name]}>
                <option value="">{$i18n("api.unset")}</option>
                {#each optionsOf(f) as o (String(o))}
                  <option value={String(o)}>{String(o)}</option>
                {/each}
              </select>
            {:else if f.kind === "json" || f.kind === "string"}
              <textarea id={`api-${f.name}`} class="mono" rows={f.kind === "json" ? 2 : 1} bind:value={values[f.name]}
                        placeholder={placeholder($i18n, f)} aria-invalid={!!errors[f.name]}></textarea>
            {:else}
              <input id={`api-${f.name}`} class="mono" bind:value={values[f.name]} placeholder={placeholder($i18n, f)}
                     aria-invalid={!!errors[f.name]} />
            {/if}
            {#if f.nullable}
              <label class="null"><input type="checkbox" checked={values[f.name] === null}
                onchange={(e) => setNull(f.name, e.currentTarget.checked)} /> {$i18n("api.sendNull")}</label>
            {/if}
          </div>
          {#if f.default !== undefined}<span class="muted desc">{placeholder($i18n, f)}</span>{/if}
          {#if f.description}<span class="muted desc">{f.description}</span>{/if}
          {#if errors[f.name]}<span class="err desc" role="alert">{errorMessage($i18n, errors[f.name])}</span>{/if}
        </div>
      {/each}
      <div class="actions">
        <button class:danger={chosen.irreversible} disabled={busy} onclick={() => void call()}>
          {busy ? $i18n("api.calling") : $i18n("api.call")}</button>
        <button onclick={addFavorite} title={$i18n("favorites.addTip")}>{$i18n("favorites.add")}</button>
        {#if favoriteId}<button onclick={updateFavorite}>{$i18n("favorites.update")}</button>{/if}
      </div>
      {#if result && result.method === chosen.name}
        <div class="muted" aria-live="polite">{result.ok ? $i18n("api.result") : $i18n("api.error")}</div>
        <pre class="mono" class:err={!result.ok}>{result.text}</pre>
      {/if}
    {:else}
      <p class="muted empty">{$i18n("api.pick")}</p>
    {/if}
  </div>
</div>

<style>
  .api { display: grid; grid-template-columns: minmax(200px, 300px) 1fr; min-height: 0; }
  .list { display: flex; flex-direction: column; border-right: 1px solid var(--line); min-height: 0; }
  .search { margin: 4px; }
  .tree { flex: 1; overflow: auto; padding: 0 4px 4px; }
  summary { cursor: default; padding: 2px 4px; font-size: 12.5px; }
  .item {
    display: flex; gap: 6px; align-items: center; width: 100%; text-align: left; padding: 1px 4px 1px 16px;
    font-size: 12px; border: none; background: none; border-radius: 4px;
  }
  .item:hover { background: var(--select); }
  .item.selected { background: var(--select); outline: 1px solid var(--accent); }
  .item .name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; }
  .tag { font-size: 10.5px; border: 1px solid var(--line); border-radius: 3px; padding: 0 4px; color: var(--muted, inherit); }
  .tag.danger { color: var(--bad); border-color: var(--bad); }
  .form { overflow: auto; padding: 6px 10px; min-height: 0; }
  .head { display: flex; gap: 8px; align-items: center; }
  .summary { margin: 4px 0 8px; white-space: pre-wrap; }
  .field { display: grid; grid-template-columns: 160px 1fr; gap: 2px 8px; margin: 4px 0; align-items: start; }
  .field .desc { grid-column: 2; font-size: 11.5px; }
  .input { display: flex; gap: 6px; align-items: start; }
  .input > input, .input > select, .input > textarea { flex: 1; min-width: 0; }
  .null { white-space: nowrap; font-size: 11.5px; }
  .label { padding-top: 3px; overflow: hidden; text-overflow: ellipsis; }
  .req { color: var(--bad); margin-left: 2px; }
  .err { color: var(--bad); }
  .actions { margin: 8px 0; }
  button.danger { color: var(--bad); }
  pre { white-space: pre-wrap; word-break: break-all; font-size: 12px; margin: 4px 0; }
  .empty { padding: 8px; }
</style>
