<script lang="ts">
  // メニュー（File / View / Debug / Target / Help）。項目はコマンド（lib/commands.ts）・パネル・その他の操作
  import { command, tooltip, type CommandState } from "../../lib/commands";
  import { shared } from "../../lib/context";
  import { allowed } from "../../lib/guard";
  import { i18n, type Key } from "../../lib/i18n";
  import { PANELS } from "../panels/registry";

  let { cs }: { cs: CommandState } = $props();
  const ctx = shared();
  const { app, dbg, ui, dock } = ctx;

  type Item = { cmd: string } | { panel: string; title: Key } | { label: Key; run: () => void } | { recent: true } | "sep";

  /** View メニューのパネル: まとまり（デバッガの窓・トレース・解析・道具）ごとに区切る */
  function viewItems(): Item[] {
    const out: Item[] = [];
    PANELS.forEach((p, i) => {
      if (i > 0 && PANELS[i - 1].group !== p.group) out.push("sep");
      out.push({ panel: p.id, title: p.title });
    });
    return out;
  }
  const MENUS: { label: Key; items: Item[] }[] = [
    { label: "menu.file", items: [{ cmd: "openElf" }, { recent: true }, { cmd: "loadDemo" }, "sep", { cmd: "programRam" },
                                  "sep", { label: "menu.settings", run: () => ui.open("settings") }] },
    { label: "menu.view", items: [...viewItems(), "sep", { label: "menu.resetLayout", run: () => dock.reset() }] },
    { label: "menu.debug", items: [{ cmd: "continue" }, { cmd: "halt" }, "sep", { cmd: "stepOver" }, { cmd: "stepInto" },
                                   { cmd: "stepOut" }, { cmd: "stepInstruction" }, { cmd: "runToCursor" }, "sep", { cmd: "toggleBreakpoint" },
                                   { cmd: "removeAllBreakpoints" }, "sep", { cmd: "reset" }] },
    { label: "menu.target", items: [{ cmd: "connect" }, { cmd: "disconnect" }, "sep", { cmd: "download" },
                                    { cmd: "loadSvd" }, "sep", { cmd: "capture" }] },
    { label: "menu.help", items: [{ label: "menu.shortcuts", run: () => ui.open("shortcuts") },
                                  { label: "menu.about", run: () => ui.open("about") }] },
  ];

  let open = $state<number | null>(null);

  function run(item: Item) {
    open = null;
    if (item === "sep") return;
    if ("cmd" in item) {
      const c = command(item.cmd);
      if (!c.blocked(cs)) void c.run(ctx, cs);
    } else if ("panel" in item) dock.show(item.panel);
    else if ("run" in item) item.run();
  }

  async function openRecent(path: string) {
    open = null;
    if ((await allowed(app)) && (await dbg.loadElf(path))) ui.remember(path);
  }

  function onKey(e: KeyboardEvent) {
    if (e.key === "Escape") open = null;
  }
</script>

<svelte:window onkeydown={onKey} onclick={(e) => { if (!(e.target as HTMLElement).closest?.(".menubar")) open = null; }} />

<nav class="menubar" aria-label="menu">
  <span class="brand">fprobe</span>
  {#each MENUS as menu, i (menu.label)}
    <div class="menu">
      <button class="top" class:open={open === i} aria-haspopup="menu" aria-expanded={open === i}
              onclick={() => (open = open === i ? null : i)} onmouseenter={() => open !== null && (open = i)}>
        {$i18n(menu.label)}</button>
      {#if open === i}
        <div class="drop" role="menu">
          {#each menu.items as item, k (k)}
            {#if item === "sep"}
              <hr />
            {:else if "cmd" in item}
              {@const c = command(item.cmd)}
              <button role="menuitem" disabled={c.blocked(cs) !== null} title={tooltip(c, cs, $i18n)} onclick={() => run(item)}>
                <span>{$i18n(c.label)}</span><span class="key">{c.shortcut ?? ""}</span></button>
            {:else if "panel" in item}
              <button role="menuitem" onclick={() => run(item)}><span>{$i18n(item.title)}</span><span class="key"></span></button>
            {:else if "recent" in item}
              <div class="sub">
                <span class="muted">{$i18n("menu.recent")}</span>
                {#each $ui.recent as path (path)}
                  <button role="menuitem" class="mono path" title={path} disabled={cs.link !== "open"}
                          onclick={() => openRecent(path)}>{path}</button>
                {:else}
                  <span class="muted none">{$i18n("menu.noRecent")}</span>
                {/each}
              </div>
            {:else}
              <button role="menuitem" onclick={() => run(item)}><span>{$i18n(item.label)}</span><span class="key"></span></button>
            {/if}
          {/each}
        </div>
      {/if}
    </div>
  {/each}
</nav>

<style>
  .menubar { display: flex; align-items: center; gap: 2px; padding: 0 8px; height: 30px; background: var(--panel);
             border-bottom: 1px solid var(--line); font-size: 13px; }
  .brand { font-weight: 700; margin-right: 10px; }
  .menu { position: relative; }
  .top { border: none; background: none; padding: 3px 9px; border-radius: 4px; }
  .top:hover, .top.open { background: var(--select); }
  .drop { position: absolute; top: 100%; left: 0; z-index: 50; min-width: 260px; background: var(--panel);
          border: 1px solid var(--line); border-radius: 6px; padding: 4px; box-shadow: 0 6px 20px rgba(0, 0, 0, 0.18);
          display: flex; flex-direction: column; }
  .drop button { display: flex; justify-content: space-between; gap: 24px; border: none; background: none;
                 padding: 4px 10px; border-radius: 4px; text-align: left; }
  .drop button:hover:not(:disabled) { background: var(--select); }
  .key { color: var(--muted); font-size: 12px; }
  hr { border: none; border-top: 1px solid var(--line); margin: 4px 2px; width: auto; }
  .sub { display: flex; flex-direction: column; padding: 2px 0; }
  .sub > .muted { padding: 2px 10px; font-size: 12px; }
  .path { max-width: 420px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; display: block !important; }
  .none { padding: 2px 10px; }
</style>
