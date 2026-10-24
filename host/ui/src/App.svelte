<script lang="ts">
  // 画面の骨組み（Ozone・Keil µVision・IAR C-SPY・VS Code に共通の型）: 上 = メニューとデバッグのツールバー、
  // 中 = ドッキングのパネル（Source が中央、周りに Watch・Registers…、下に Timeline・Terminal…）、下 = ステータスバー。
  // 仕様: docs/superpowers/specs/2026-10-18-ui-layout-design.md
  import { onDestroy, setContext } from "svelte";
  import { DaemonConnection } from "./lib/api";
  import { sourcedApp, sourcedConn } from "./lib/source";
  import { CodeAtLink } from "./lib/codeat";
  import { commandForKey, type CommandState } from "./lib/commands";
  import { SHARED, type Shared } from "./lib/context";
  import { Debugger, focusFrame } from "./lib/debugger";
  import type { Dock } from "./lib/dock";
  import { watchFaults } from "./lib/fault";
  import { getLang, i18n } from "./lib/i18n";
  import { Safety } from "./lib/safety";
  import { ScreenSync } from "./lib/screen";
  import { AppState } from "./lib/state";
  import { onTimeline } from "./lib/timeline/events";
  import { TimelineState, type Reveal } from "./lib/timeline/state";
  import { UiState } from "./lib/ui";
  import ConfirmDialog from "./components/shell/ConfirmDialog.svelte";
  import ContextMenu from "./components/shell/ContextMenu.svelte";
  import Dialogs from "./components/shell/Dialogs.svelte";
  import DebugToolbar from "./components/shell/DebugToolbar.svelte";
  import DockArea from "./components/shell/DockArea.svelte";
  import MenuBar from "./components/shell/MenuBar.svelte";
  import SafetyBar from "./components/shell/SafetyBar.svelte";
  import StatusLine from "./components/shell/StatusLine.svelte";

  // 画面はデーモン自身が配る（開発中は Vite の代理）ので、同じ場所の /api につなぐ。トークンは #token=...
  const hash = new URLSearchParams(location.hash.slice(1));
  const token = hash.get("token") ?? undefined;
  // #layout=system: 開いたときにその配置にする（配置ごとの URL を残せる）
  const initialLayout = (["analysis", "debug", "system"] as const).find((n) => n === hash.get("layout")) ?? null;
  const conn = new DaemonConnection(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api`, {
    name: "gui", role: "human", token, topics: ["activity", "control", "clients", "target.state"],
    optionalTopics: ["ui.state", "flash.progress", "recording", "hook"],
    automated: navigator.webdriver === true,        // 自動操作のブラウザ（試験・確かめ）か。操作の記録に残す
  });
  const base = new AppState(conn);
  // 共有の部品の要求は部品の名前を source に（lib/source.ts）。窓の要求は窓ごとに（lib/dock.ts）
  const by = (source: string) => ({ conn: sourcedConn(conn, source), app: sourcedApp(base, source) });
  const app = by("shell").app;
  const dbg = new Debugger(by("debugger").conn, by("debugger").app);
  const tl = new TimelineState(by("timeline").conn, by("timeline").app);
  const ui = new UiState();
  const safety = new Safety(by("safety").conn, by("safety").app);
  // 画面の状態（視点・カーソル・段・出来事・フレーム）をデーモン（AI・別の窓）と URL にそろえる
  const screen = new ScreenSync(by("screen").conn, tl, dbg, (i) => void focusFrame(dbg, ui, i));
  // 時間軸の選択カーソル（記録の時刻）→ その時刻のコードの位置（recording.code_at）。連動する窓が使う
  const codeAt = new CodeAtLink(by("codeAt").conn, tl);
  let dockRef: Dock | null = null;
  const dock = { show: (id: string) => dockRef?.show(id), reset: () => dockRef?.reset(),
                 layout: (name: "analysis" | "debug" | "system") => dockRef?.layout(name) };
  // ビューの行 → 時間軸のその時刻、時間軸・ビュー → ソースの行・逆アセンブル・メモリ（UI/UX の調査の規則: 1 回クリック
  // = 選ぶ、ダブルクリック = 元を開く）
  const reveal = (r: Reveal) => {
    tl.reveal(r);
    dock.show("timeline");
  };
  const showAddress = async (address: number) => {
    dock.show("source");
    const at = await dbg.showAddress(address);
    if (at) ui.revealLine(at.file, at.line);
  };
  const showMemory = (address: number) => {
    ui.revealMemory(address);
    dock.show("memory");
  };
  const showApi = (method: string, params: Record<string, unknown>, favorite?: string) => {
    ui.openApi(method, params, favorite);
    dock.show("api");
  };
  const ctx: Shared = { conn: sourcedConn(conn, "shell"), app, dbg, tl, ui, safety, screen, codeAt, dock, reveal, showAddress: (a) => void showAddress(a),
                        showMemory, showApi };
  setContext(SHARED, ctx);
  const context = new Map<any, any>([[SHARED, ctx]]);
  document.documentElement.lang = getLang();

  // 止まったファイルの行を「選んだ行」にする（Run to Cursor・F9 の既定の対象）
  $effect(() => {
    const stop = $dbg.stop;
    if (stop?.file && stop.line !== undefined && !$ui.selection) ui.select(stop.file, stop.line);
  });
  // ELF を開いたら最近使った項目に
  $effect(() => {
    if ($dbg.elf) ui.remember($dbg.elf);
  });

  const cs = $derived<CommandState>({ link: $app.link, target: $app.target?.state, elf: $dbg.elf,
                                         selection: $ui.selection, rescueDp: $app.target?.rescue_dp });

  function onKey(e: KeyboardEvent) {
    if ($ui.dialog) return;
    const el = e.target as HTMLElement | null;
    const inField = !!el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.tagName === "SELECT"
                             || el.isContentEditable);
    const c = commandForKey(e, inField);
    if (!c) return;
    e.preventDefault();                          // 使えないときも、ブラウザの F5（読み直し）等には渡さない
    if (!c.blocked(cs)) void c.run(ctx, cs);
  }

  // 時間軸のダブルクリック・Enter: 出来事の元（番地 → メモリ、PC → ソース）を開く
  // 故障で止まったら Fault Analyzer を開く（IAR の Fault exception viewer と同じ）
  const stopFaults = watchFaults((m, p, o) => conn.call(m, p, o), app, () => dock.show("fault"));
  const stopOpen = onTimeline({ open: (d) => (d.kind === "memory" ? showMemory(d.address) : void showAddress(d.address)) });

  conn.start();
  onDestroy(() => {
    stopOpen();
    stopFaults();
    conn.stop();
    screen.dispose();
    codeAt.dispose();
    tl.dispose();
  });
</script>

<!-- 窓を閉じる・離れる: 自分のマウスのカーソルを共有の状態から消す（デーモンも接続が切れたら消す） -->
<svelte:window onkeydown={onKey} onpagehide={() => screen.leave()} />

<div class="shell">
  <MenuBar {cs} />
  <DebugToolbar {cs} />
  <SafetyBar />
  <main><DockArea {context} ready={(d) => { dockRef = d; if (initialLayout) d.layout(initialLayout); }} /></main>
  <StatusLine />
  <Dialogs />
  <ConfirmDialog />
  <ContextMenu />
  {#if $app.error}
    <div class="toast" role="alert">
      <span>{$app.error}</span>
      <button onclick={() => app.clearError()} aria-label={$i18n("error.dismiss")}>×</button>
    </div>
  {/if}
</div>

<style>
  .shell { display: grid; grid-template-rows: auto auto auto 1fr auto; height: 100%; }
  main { min-height: 0; position: relative; }
  .toast {
    position: fixed; right: 16px; bottom: 36px; max-width: min(560px, calc(100vw - 32px)); display: flex; gap: 8px;
    align-items: center; background: var(--panel); border: 1px solid var(--bad); color: var(--bad);
    border-radius: 8px; padding: 8px 12px; box-shadow: 0 4px 16px rgba(0, 0, 0, 0.15); z-index: 100;
  }
</style>
