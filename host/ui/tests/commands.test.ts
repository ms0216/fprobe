// コマンド（メニュー・ツールバー・キーボードの共通の定義）: ショートカット・使える条件・送る API
import { describe, expect, it } from "vitest";
import { COMMANDS, command, commandForKey, shortcutOf, tooltip, type CommandState } from "../src/lib/commands";
import { Debugger } from "../src/lib/debugger";
import { execution } from "../src/lib/execution";
import { t } from "../src/lib/i18n";
import { Safety } from "../src/lib/safety";
import { AppState } from "../src/lib/state";
import { UiState } from "../src/lib/ui";
import { FakeConn } from "./fake-conn";

const key = (k: string, mods: Partial<{ ctrlKey: boolean; shiftKey: boolean; altKey: boolean; metaKey: boolean }> = {}) =>
  ({ key: k, ctrlKey: false, shiftKey: false, altKey: false, metaKey: false, ...mods });

const halted: CommandState = { link: "open", target: "HALTED", elf: "/x/app.elf", selection: { file: "main.c", line: 12 } };

describe("ショートカット（VS Code の慣習、F6 は Ozone、F8 は Keil）", () => {
  it("キーをコマンドに対応させる", () => {
    const id = (k: ReturnType<typeof key>) => commandForKey(k)?.id;
    expect(id(key("F5"))).toBe("continue");
    expect(id(key("F6"))).toBe("halt");
    expect(id(key("F5", { shiftKey: true }))).toBe("disconnect");
    expect(id(key("F9"))).toBe("toggleBreakpoint");
    expect(id(key("F10"))).toBe("stepOver");
    expect(id(key("F11"))).toBe("stepInto");
    expect(id(key("F11", { shiftKey: true }))).toBe("stepOut");
    expect(id(key("F10", { ctrlKey: true }))).toBe("runToCursor");
    expect(id(key("F8"))).toBe("download");
    expect(id(key("F11", { altKey: true }))).toBe("stepInstruction");
    expect(id(key("o", { ctrlKey: true }))).toBe("openElf");
    expect(id(key("o", { metaKey: true }))).toBe("openElf");              // Mac の ⌘
    expect(id(key("x"))).toBeUndefined();
  });

  it("入力欄の中では F キーだけを取る（文字の入力を奪わない）", () => {
    expect(commandForKey(key("F10"), true)?.id).toBe("stepOver");
    expect(commandForKey(key("o", { ctrlKey: true }), true)).toBeUndefined();
    expect(shortcutOf(key("F5", { ctrlKey: true, shiftKey: true }))).toBe("Ctrl+Shift+F5");
  });

  it("ショートカットは重ならない", () => {
    const keys = COMMANDS.filter((c) => c.shortcut).map((c) => c.shortcut);
    expect(new Set(keys).size).toBe(keys.length);
  });
});

describe("使える条件とツールチップ", () => {
  it("止まっているときだけステップ、走っているときだけ Halt、行を選んでいれば Run to Cursor", () => {
    expect(command("stepOver").blocked(halted)).toBeNull();
    expect(command("halt").blocked(halted)).toBe("tip.notRunning");
    expect(command("continue").blocked({ ...halted, target: "RUNNING" })).toBe("tip.notHalted");
    expect(command("stepOver").blocked({ ...halted, target: "DISCONNECTED" })).toBe("tip.notConnected");
    expect(command("runToCursor").blocked({ ...halted, selection: null })).toBe("tip.noLine");
    expect(command("download").blocked({ ...halted, elf: null })).toBe("tip.noElf");
    expect(command("openElf").blocked({ ...halted, link: "closed" })).toBe("tip.offline");
  });

  it("ツールチップは「名前 (ショートカット) — 説明」、使えなければ理由も", () => {
    expect(tooltip(command("stepOver"), halted, t)).toBe(`Step Over (F10) — ${t("tip.stepOver")}`);
    expect(tooltip(command("halt"), halted, t)).toContain(t("tip.notRunning"));
  });
});

describe("実行すると API を呼ぶ", () => {
  function setup(answer = true) {
    const conn = new FakeConn({ "debug.step": {}, "target.resume": {}, "target.halt": {}, "debug.breakpoint.set": {},
                                "debug.breakpoint.list": { breakpoints: [], watchpoints: [] }, "debug.elf.load_demo":
                                  { path: "/demo.elf", files: [] }, "debug.source.list": { files: [] },
                                "debug.halt_risk": { elf: "/x/app.elf", matched: true, monitor_in_elf: false,
                                                     matches: [{ kind: "radio", count: 1, symbols: ["RADIO_IRQHandler"] }] } });
    const app = new AppState(conn as any);
    const dbg = new Debugger(conn as any, app);
    const ui = new UiState();
    const asked: string[] = [];
    const safety = new Safety(conn as any, app, (text) => { asked.push(text); return answer; });
    return { conn, asked, safety, ctx: { app, dbg, ui, tl: {} as any, safety } };
  }

  it("止めると壊れうるターゲットでは、止める操作の前に確かめ、「いいえ」なら API を呼ばない", async () => {
    const { conn, asked, safety, ctx } = setup(false);
    await safety.reload();
    conn.emit("target.state", { state: "RUNNING" });
    await command("halt").run(ctx, { ...halted, target: "RUNNING" });
    await command("toggleBreakpoint").run(ctx, halted);
    expect(asked.length).toBe(2);
    expect(conn.calls.map(([m]) => m)).not.toContain("target.halt");
    expect(conn.calls.map(([m]) => m)).not.toContain("debug.breakpoint.set");
    await command("continue").run(ctx, halted);                         // 続行は止めない: 聞かない
    expect(asked.length).toBe(2);
  });

  it("ステップ・Run to Cursor・ブレークポイント・続行", async () => {
    const { conn, ctx } = setup();
    await command("stepOver").run(ctx, halted);
    await command("stepOut").run(ctx, halted);
    await command("runToCursor").run(ctx, halted);
    await command("toggleBreakpoint").run(ctx, halted);
    await command("continue").run(ctx, halted);
    expect(conn.calls.map(([m, p]) => [m, p])).toEqual(expect.arrayContaining([
      ["debug.step", { mode: "over" }], ["debug.step", { mode: "out" }],
      ["debug.step", { mode: "until", location: "main.c:12" }], ["debug.breakpoint.set", { location: "main.c:12" }],
      ["target.resume", {}],
    ]));
  });

  it("命令ステップは target.step（止めると壊れうるなら確かめる）。実行の設定の割り込み・リセットの種類を渡す", async () => {
    const { conn, asked, safety, ctx } = setup(false);
    await safety.reload();
    await command("stepInstruction").run(ctx, halted);
    expect(asked.length).toBe(1);
    expect(conn.calls.map(([m]) => m)).not.toContain("target.step");
    const ok = setup();
    ok.conn.responders["target.step"] = {};
    ok.conn.responders["target.reset"] = {};
    await command("stepInstruction").run(ok.ctx, halted);
    execution.set({ maskInterrupts: false, resetKind: "system" });
    try {
      await command("stepInto").run(ok.ctx, halted);
      await command("reset").run(ok.ctx, halted);
    } finally {
      execution.set({ maskInterrupts: true, resetKind: "default" });
    }
    expect(ok.conn.calls.map(([m, p]) => [m, p])).toEqual(expect.arrayContaining([
      ["target.step", { count: 1 }], ["debug.step", { mode: "into", mask_interrupts: false }],
      ["target.reset", { halt: true, kind: "system" }],
    ]));
    expect(command("stepInstruction").blocked({ ...halted, target: "RUNNING" })).toBe("tip.notHalted");
  });

  it("チップ全体のリセット（RP2040 の救済の DP）は RP2040 に接続中だけ。target.reset kind=rescue", async () => {
    const rp = { ...halted, target: "RUNNING", part: "rp2040_core0" };
    expect(command("rescueReset").blocked(rp)).toBeNull();
    expect(command("rescueReset").blocked({ ...rp, part: "stm32f401xe" })).toBe("tip.noRescue");
    expect(command("rescueReset").blocked({ ...rp, target: "DISCONNECTED" })).toBe("tip.notConnected");
    const { conn, ctx } = setup();
    conn.responders["target.reset"] = {};
    await command("rescueReset").run(ctx, rp);
    expect(conn.calls.map(([m, p]) => [m, p])).toContainEqual(["target.reset", { kind: "rescue" }]);
  });

  it("Load Demo は開いた ELF を最近使った項目に足す。Open ELF はダイアログを開く", async () => {
    const { ctx } = setup();
    await command("loadDemo").run(ctx, halted);
    expect(ctx.ui.snapshot.recent[0]).toBe("/demo.elf");
    command("openElf").run(ctx, halted);
    expect(ctx.ui.snapshot.dialog).toBe("openElf");
  });
});
