<script lang="ts">
  // お気に入り: 人が自分で並べた、よく使う呼び出し（メソッドと引数）のボタン（段階 B。
  // docs/superpowers/specs/2026-10-22-ui-stage-b-design.md §2）。確かめは API パネルと同じ。足すのは API パネルから
  import { callApi } from "../../lib/apicall";
  import { METHODS } from "../../lib/apispec";
  import { shared } from "../../lib/context";
  import { checkFavorite, favorites, type Favorite } from "../../lib/favorites";
  import { i18n } from "../../lib/i18n";

  const { app, conn, showApi, dock } = shared();

  let busy = $state<string | null>(null);
  let results = $state<Record<string, { ok: boolean; text: string }>>({});
  let editing = $state<string | null>(null);
  let label = $state("");

  async function run(f: Favorite) {
    const checked = checkFavorite(f, METHODS);
    if ("problem" in checked) return;
    busy = f.id;
    try {
      const r = await callApi(checked.method, checked.params, { conn, app });
      if (r) results[f.id] = r;
    } finally {
      busy = null;
    }
  }

  function startRename(f: Favorite) {
    editing = f.id;
    label = f.label;
  }

  function finishRename(f: Favorite) {
    if (editing !== f.id) return;
    const text = label.trim();
    if (text) favorites.rename(f.id, text);
    editing = null;
  }
</script>

<div class="pane favorites">
  {#each $favorites as f, i (f.id)}
    {@const checked = checkFavorite(f, METHODS)}
    <div class="fav">
      <div class="line">
        {#if editing === f.id}
          <!-- svelte-ignore a11y_autofocus -->
          <input class="name" bind:value={label} autofocus aria-label={$i18n("favorites.rename")}
                 onblur={() => finishRename(f)}
                 onkeydown={(e) => { if (e.key === "Enter") finishRename(f); if (e.key === "Escape") editing = null; }} />
        {:else}
          <button class="run" class:danger={"method" in checked && checked.method.irreversible}
                  disabled={"problem" in checked || busy === f.id} title={`${f.method} ${JSON.stringify(f.params)}`}
                  onclick={() => void run(f)}>{f.label}</button>
        {/if}
        <span class="muted mono method">{f.method}</span>
        <span class="spacer"></span>
        <button class="small" title={$i18n("favorites.rename")} onclick={() => startRename(f)}>✎</button>
        <button class="small" title={$i18n("favorites.up")} disabled={i === 0} onclick={() => favorites.move(f.id, -1)}>↑</button>
        <button class="small" title={$i18n("favorites.down")} disabled={i === $favorites.length - 1}
                onclick={() => favorites.move(f.id, 1)}>↓</button>
        <button class="small" title={$i18n("favorites.edit")} onclick={() => showApi(f.method, f.params, f.id)}>⋯</button>
        <button class="small" title={$i18n("favorites.remove")} onclick={() => favorites.remove(f.id)}>✕</button>
      </div>
      {#if "problem" in checked}
        <div class="err">{$i18n(checked.problem === "missing" ? "favorites.missing" : "favorites.badParams")}</div>
      {:else if results[f.id]}
        <pre class="mono" class:err={!results[f.id].ok} aria-live="polite">{results[f.id].text}</pre>
      {/if}
    </div>
  {:else}
    <div class="muted empty">{$i18n("favorites.none")}
      <button class="small" onclick={() => dock.show("api")}>{$i18n("panel.api")}</button></div>
  {/each}
</div>

<style>
  .favorites { overflow: auto; padding: 4px 6px; }
  .fav { border-bottom: 1px solid var(--line); padding: 4px 0; }
  .line { display: flex; gap: 6px; align-items: center; }
  .run { font-weight: 600; }
  .run.danger { color: var(--bad); }
  .method { font-size: 11.5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .spacer { flex: 1; }
  .name { flex: 0 1 240px; }
  .err { color: var(--bad); font-size: 12px; }
  pre { white-space: pre-wrap; word-break: break-all; font-size: 12px; margin: 4px 0; max-height: 200px; overflow: auto; }
  .empty { padding: 8px; }
</style>
