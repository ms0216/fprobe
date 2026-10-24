// 右クリックのメニュー（docs/superpowers/specs/2026-10-23-ui-rebuild-design.md §3.5、
// docs/reports/2026-10-24-competitor-ui-research.md 第 4 部）。Svelte に依らない。
// - 項目 = API の呼び出し。項目に API 名を出す（人と AI の一対一を見せる）。
// - 値の種類ごとに決めた厳選の一覧（同じ値はどの窓でも同じ並び）。画面は判断しない: 使えない項目は理由を付けて灰色に
//   する（隠さない）。
// - 開いているメニューは 1 つ（contextMenu。App の ContextMenu.svelte が描く）。
import { writable } from "svelte/store";
import type { Breakpoint } from "./debugger";
import type { Key } from "./i18n";

export interface MenuItem {
  label: Key;
  params?: Record<string, string | number>;  // 文言の差し込み
  api?: string;                              // 呼ぶ API（項目の脇に小さく出す）
  blocked?: Key | null;                      // 使えない理由（使えるなら null）
  shortcut?: string;
  run: () => unknown;
}

export type MenuEntry = MenuItem | "sep";

export interface OpenMenu {
  x: number;
  y: number;
  entries: MenuEntry[];
}

export const contextMenu = writable<OpenMenu | null>(null);

/** 右クリックの位置にメニューを開く（ブラウザのメニューは出さない） */
export function openMenu(e: MouseEvent, entries: MenuEntry[]): void {
  e.preventDefault();
  e.stopPropagation();
  contextMenu.set({ x: e.clientX, y: e.clientY, entries });
}

export function closeMenu(): void {
  contextMenu.set(null);
}

/** ソースの行のメニューで呼ぶ操作（呼び手が人の操作の確認を通して渡す） */
export interface LineActions {
  toggle: () => unknown;                     // debug.breakpoint.set / clear
  enable: (enabled: boolean) => unknown;     // debug.breakpoint.enable
  runTo: () => unknown;                      // debug.step（until・location）
}

/** ソースの行（調査 4.2 (c)）: ブレークポイント（置く / 外す）・有効 / 無効・ここまで実行。bp = その行のブレーク
 *  ポイント。blockedBreak・blockedRun = コマンドの使えない理由（commands.ts の toggleBreakpoint・runToCursor と同じ） */
export function lineMenu(bp: Breakpoint | undefined, blockedBreak: Key | null, blockedRun: Key | null,
                         act: LineActions): MenuEntry[] {
  return [
    { label: bp ? "menu.bp.remove" : "menu.bp.set", api: bp ? "debug.breakpoint.clear" : "debug.breakpoint.set",
      blocked: blockedBreak, shortcut: "F9", run: act.toggle },
    ...(bp ? [enableItem(bp, blockedBreak, act.enable)] : []),
    "sep",
    { label: "cmd.runToCursor", api: "debug.step", blocked: blockedRun, shortcut: "Ctrl+F10", run: act.runTo },
  ];
}

/** ブレークポイントの表のメニューで呼ぶ操作 */
export interface BreakpointActions {
  enable: (enabled: boolean) => unknown;     // debug.breakpoint.enable
  remove: () => unknown;                     // debug.breakpoint.clear（location）
  edit: () => unknown;                       // 条件・ログの欄を開く（置き直しは debug.breakpoint.set）
  source: (() => unknown) | null;            // ソースの行へ（ファイル・行が分からなければ null）
  removeAll: () => unknown;                  // debug.breakpoint.clear（location 省略）
}

/** ブレークポイント（調査 4.2 (h)）: 有効 / 無効・条件とログ・外す・ソースへ・すべて外す。blocked = 機器に触る操作の
 *  使えない理由。道具が置いたもの（tool）は道具が持つので、条件の欄は開かない */
export function breakpointMenu(bp: Breakpoint, blocked: Key | null, act: BreakpointActions): MenuEntry[] {
  return [
    enableItem(bp, blocked, act.enable),
    { label: "menu.bp.edit", api: "debug.breakpoint.set", blocked: blocked ?? (bp.tool ? "menu.bp.byTool" : null), run: act.edit },
    { label: "menu.bp.remove", api: "debug.breakpoint.clear", blocked, run: act.remove },
    "sep",
    { label: "menu.bp.source", blocked: act.source ? null : "menu.bp.noSource", run: act.source ?? (() => undefined) },
    "sep",
    { label: "cmd.removeAllBreakpoints", api: "debug.breakpoint.clear", blocked, shortcut: "Ctrl+Shift+F9", run: act.removeAll },
  ];
}

function enableItem(bp: Breakpoint, blocked: Key | null, enable: (enabled: boolean) => unknown): MenuItem {
  const on = bp.enabled !== false;
  return { label: on ? "menu.bp.disable" : "menu.bp.enable", api: "debug.breakpoint.enable", blocked,
           run: () => enable(!on) };
}
