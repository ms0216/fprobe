// コマンド: メニュー・ツールバー・キーボードが同じ定義を使う（名前・説明・ショートカット・使える条件・実行）。
// ショートカットは VS Code の慣習（F6 の Halt は Ozone、F8 の Download は Keil µVision）。Svelte に依らない。
// 仕様: docs/superpowers/specs/2026-10-18-ui-layout-design.md §2
import type { LinkState } from "./api";
import type { Debugger } from "./debugger";
import { execution, resetParams } from "./execution";
import { allowed } from "./guard";
import { t, type Key } from "./i18n";
import type { HaltAction, Safety } from "./safety";
import type { AppState } from "./state";
import type { TimelineState } from "./timeline/state";
import type { UiState } from "./ui";

export type IconName = "open" | "download" | "reset" | "continue" | "halt" | "stepOver" | "stepInto" | "stepOut"
  | "stepInstruction" | "runToCursor" | "breakpoint" | "connect" | "disconnect" | "demo" | "chip" | "capture";

export interface CommandContext {
  app: AppState;
  dbg: Debugger;
  ui: UiState;
  tl: TimelineState;
  safety: Safety;
}

/** 使える条件を決める今の状態 */
export interface CommandState {
  link: LinkState;
  target: string | undefined;                // RUNNING / HALTED / DISCONNECTED / undefined（不明）
  elf: string | null;
  selection: { file: string; line: number } | null;
  rescueDp?: number;                         // ホストが申告した救済の DP（target.status の rescue_dp。無ければ無い）
}

export interface Command {
  id: string;
  label: Key;
  tip: Key;
  shortcut?: string;                         // "Shift+F11" の形（shortcutOf と同じ書き方）
  icon?: IconName;
  /** 使えない理由（使えるなら null） */
  blocked(s: CommandState): Key | null;
  run(ctx: CommandContext, s: CommandState): unknown;
}

const online = (s: CommandState): Key | null => (s.link === "open" ? null : "tip.offline");
const connected = (s: CommandState): Key | null =>
  online(s) ?? (s.target && s.target !== "DISCONNECTED" ? null : "tip.notConnected");
const halted = (s: CommandState): Key | null => connected(s) ?? (s.target === "HALTED" ? null : "tip.notHalted");
/** 走っているか（眠って割り込みを待っている SLEEPING も走っている: 止める・観測の対象） */
export const isRunning = (state: string | undefined): boolean => state === "RUNNING" || state === "SLEEPING";
const running = (s: CommandState): Key | null => connected(s) ?? (isRunning(s.target) ? null : "tip.notRunning");
const withElf = (s: CommandState): Key | null => online(s) ?? (s.elf ? null : "tip.noElf");
const withLine = (base: (s: CommandState) => Key | null) => (s: CommandState): Key | null =>
  base(s) ?? (s.selection ? null : "tip.noLine");

/** 人の操作の前の確認（AI がつながっていて一時停止していないとき）を通してから実行する */
const guarded = (fn: (ctx: CommandContext, s: CommandState) => unknown) =>
  async (ctx: CommandContext, s: CommandState) => ((await allowed(ctx.app)) ? fn(ctx, s) : undefined);
/** コアを止める操作: 止めると壊れうるターゲットなら先に確かめる（lib/safety.ts） */
const halting = (action: HaltAction, fn: (ctx: CommandContext, s: CommandState) => unknown) =>
  guarded(async (ctx, s) => ((await ctx.safety.confirmHalt(action, t)) ? fn(ctx, s) : undefined));

export const COMMANDS: Command[] = [
  { id: "openElf", label: "cmd.openElf", tip: "tip.openElf", shortcut: "Ctrl+O", icon: "open", blocked: online,
    run: ({ ui }) => ui.open("openElf") },
  { id: "loadDemo", label: "cmd.loadDemo", tip: "tip.loadDemo", icon: "demo", blocked: online,
    run: guarded(async ({ dbg, ui }) => {
      const path = await dbg.loadDemo();
      if (path) ui.remember(path);
    }) },
  { id: "programRam", label: "cmd.programRam", tip: "tip.programRam",
    blocked: (s) => connected(s) ?? withElf(s), run: guarded(({ dbg }, s) => dbg.programRam(s.elf!)) },
  { id: "download", label: "cmd.download", tip: "tip.download", shortcut: "F8", icon: "download",
    blocked: (s) => connected(s) ?? withElf(s), run: guarded(({ app }, s) => app.act("target.flash", { path: s.elf! })) },
  { id: "reset", label: "cmd.reset", tip: "tip.reset", shortcut: "Ctrl+Shift+F5", icon: "reset", blocked: connected,
    run: guarded(({ app }) => app.act("target.reset", resetParams(execution.get()))) },
  { id: "rescueReset", label: "cmd.rescueReset", tip: "tip.rescueReset",
    blocked: (s) => connected(s) ?? (s.rescueDp != null ? null : "tip.noRescue"),
    run: guarded(({ app }) => app.act("target.reset", { kind: "rescue" })) },
  { id: "continue", label: "cmd.continue", tip: "tip.continue", shortcut: "F5", icon: "continue", blocked: halted,
    run: guarded(({ app }) => app.act("target.resume")) },
  { id: "halt", label: "cmd.halt", tip: "tip.halt", shortcut: "F6", icon: "halt", blocked: running,
    run: halting("halt", ({ app }) => app.act("target.halt")) },
  { id: "stepOver", label: "cmd.stepOver", tip: "tip.stepOver", shortcut: "F10", icon: "stepOver", blocked: halted,
    run: halting("step", ({ dbg }) => dbg.step("over")) },
  { id: "stepInto", label: "cmd.stepInto", tip: "tip.stepInto", shortcut: "F11", icon: "stepInto", blocked: halted,
    run: halting("step", ({ dbg }) => dbg.step("into")) },
  { id: "stepOut", label: "cmd.stepOut", tip: "tip.stepOut", shortcut: "Shift+F11", icon: "stepOut", blocked: halted,
    run: halting("step", ({ dbg }) => dbg.step("out")) },
  { id: "stepInstruction", label: "cmd.stepInstruction", tip: "tip.stepInstruction", shortcut: "Alt+F11",
    icon: "stepInstruction", blocked: halted, run: halting("step", ({ dbg }) => dbg.stepInstruction()) },
  { id: "runToCursor", label: "cmd.runToCursor", tip: "tip.runToCursor", shortcut: "Ctrl+F10", icon: "runToCursor",
    blocked: withLine(halted), run: halting("step", ({ dbg }, s) => dbg.runTo(s.selection!.file, s.selection!.line)) },
  { id: "toggleBreakpoint", label: "cmd.toggleBreakpoint", tip: "tip.toggleBreakpoint", shortcut: "F9",
    icon: "breakpoint", blocked: withLine(connected),
    run: halting("breakpoint", ({ dbg }, s) => dbg.toggleAt(s.selection!.file, s.selection!.line)) },
  { id: "removeAllBreakpoints", label: "cmd.removeAllBreakpoints", tip: "tip.removeAllBreakpoints",
    shortcut: "Ctrl+Shift+F9", blocked: connected, run: guarded(({ dbg }) => dbg.removeAllBreakpoints()) },
  { id: "connect", label: "cmd.connect", tip: "tip.connect", icon: "connect", blocked: online,
    run: ({ ui }) => ui.open("connect") },
  { id: "disconnect", label: "cmd.disconnect", tip: "tip.disconnect", shortcut: "Shift+F5", icon: "disconnect",
    blocked: connected, run: guarded(({ app }) => app.act("target.disconnect")) },
  { id: "loadSvd", label: "cmd.loadSvd", tip: "tip.loadSvd", icon: "chip", blocked: online,
    run: ({ ui }) => ui.open("loadSvd") },
  { id: "capture", label: "cmd.capture", tip: "tip.capture", icon: "capture", blocked: online,
    run: guarded(({ tl }) => tl.captureSaved({ deep: false })) },
];

export const command = (id: string): Command => {
  const c = COMMANDS.find((x) => x.id === id);
  if (!c) throw new Error(`コマンド ${id} は無い`);
  return c;
};

/** キーボードの出来事 → "Ctrl+Shift+F5" の形（Mac の ⌘ も Ctrl として扱う） */
export function shortcutOf(e: Pick<KeyboardEvent, "key" | "ctrlKey" | "metaKey" | "shiftKey" | "altKey">): string {
  const key = e.key.length === 1 ? e.key.toUpperCase() : e.key;
  return [e.ctrlKey || e.metaKey ? "Ctrl" : "", e.altKey ? "Alt" : "", e.shiftKey ? "Shift" : "", key]
    .filter(Boolean).join("+");
}

/** キーに当たるコマンド（無ければ undefined）。入力欄の中では F キーとショートカットの Ctrl+ だけを取る */
export function commandForKey(e: Pick<KeyboardEvent, "key" | "ctrlKey" | "metaKey" | "shiftKey" | "altKey">,
                              inField = false): Command | undefined {
  const s = shortcutOf(e);
  if (inField && !/(^|\+)F\d+$/.test(s)) return undefined;
  return COMMANDS.find((c) => c.shortcut === s);
}

/** ツールチップ: 「名前 (ショートカット) — 説明」。使えなければ理由も */
export function tooltip(c: Command, s: CommandState, t: (k: Key) => string): string {
  const head = c.shortcut ? `${t(c.label)} (${c.shortcut})` : t(c.label);
  const why = c.blocked(s);
  return `${head} — ${t(c.tip)}${why ? `\n${t(why)}` : ""}`;
}
