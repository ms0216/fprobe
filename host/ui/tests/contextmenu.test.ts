// 右クリックのメニュー（lib/contextmenu.ts）: 項目 = API の呼び出し、使えない項目は理由付きで残す
import { get } from "svelte/store";
import { describe, expect, it } from "vitest";
import { breakpointMenu, closeMenu, contextMenu, lineMenu, openMenu, type MenuEntry, type MenuItem } from "../src/lib/contextmenu";
import type { Breakpoint } from "../src/lib/debugger";

const items = (entries: MenuEntry[]) => entries.filter((e): e is MenuItem => e !== "sep");
const bp = (more: Partial<Breakpoint> = {}): Breakpoint =>
  ({ address: 0x100, location: "main.c:10", where: "main+4", file: "main.c", line: 10, ...more } as Breakpoint);

describe("ソースの行のメニュー", () => {
  const act = { toggle: () => "toggle", enable: (on: boolean) => `enable ${on}`, runTo: () => "runTo" };

  it("ブレークポイントが無い行: 置く・ここまで実行（有効・無効は出さない）", () => {
    const m = items(lineMenu(undefined, null, null, act));
    expect(m.map((i) => [i.label, i.api])).toEqual([["menu.bp.set", "debug.breakpoint.set"], ["cmd.runToCursor", "debug.step"]]);
  });

  it("ある行: 外す・無効にする（無効なら有効にする）。使えない理由はそのまま", () => {
    const m = items(lineMenu(bp(), null, "tip.notHalted", act));
    expect(m.map((i) => i.label)).toEqual(["menu.bp.remove", "menu.bp.disable", "cmd.runToCursor"]);
    expect(m[1].run()).toBe("enable false");
    expect(m[2].blocked).toBe("tip.notHalted");
    expect(items(lineMenu(bp({ enabled: false }), null, null, act))[1]).toMatchObject({ label: "menu.bp.enable" });
  });
});

describe("ブレークポイントの表のメニュー", () => {
  const act = { enable: (on: boolean) => on, remove: () => "remove", edit: () => "edit", source: null, removeAll: () => "all" };

  it("有効・無効・条件・外す・ソースへ・すべて外す。ファイルが分からなければソースへは理由付き", () => {
    const m = items(breakpointMenu(bp({ enabled: false }), null, act));
    expect(m.map((i) => i.label)).toEqual(["menu.bp.enable", "menu.bp.edit", "menu.bp.remove", "menu.bp.source",
                                           "cmd.removeAllBreakpoints"]);
    expect(m[0].run()).toBe(true);
    expect(m[3].blocked).toBe("menu.bp.noSource");
  });

  it("道具が置いたものは条件の欄を開かない。つながっていなければ機器に触る項目に理由", () => {
    expect(items(breakpointMenu(bp({ tool: "heap.trace" }), null, act))[1].blocked).toBe("menu.bp.byTool");
    const off = items(breakpointMenu(bp(), "tip.offline", act));
    expect(off.filter((i) => i.api).every((i) => i.blocked === "tip.offline")).toBe(true);
  });
});

describe("開いているメニュー", () => {
  it("右クリックの位置で開き、ブラウザのメニューは出さない。閉じると空", () => {
    let prevented = false;
    const e = { clientX: 5, clientY: 7, preventDefault: () => (prevented = true), stopPropagation: () => {} } as unknown as MouseEvent;
    openMenu(e, ["sep"]);
    expect(get(contextMenu)).toEqual({ x: 5, y: 7, entries: ["sep"] });
    expect(prevented).toBe(true);
    closeMenu();
    expect(get(contextMenu)).toBeNull();
  });
});
