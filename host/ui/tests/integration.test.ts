// 結合: 偽の機器のデーモンに、画面（human）と AI 役（ai）が同じ API でつながる
import { afterAll, beforeAll, describe, expect, inject, it, vi } from "vitest";
import { DaemonConnection, type Role } from "../src/lib/api";
import { AppState, type Snapshot } from "../src/lib/state";
import { Debugger, type DebugSnapshot } from "../src/lib/debugger";
import { TimelineState, type TimelineSnapshot } from "../src/lib/timeline/state";
import type { TimelineFrame } from "../src/lib/timeline/compose";

const url = () => `ws://127.0.0.1:${inject("daemonPort")}/api`;

function open(name: string, role: Role, topics: string[]) {
  const conn = new DaemonConnection(url(), { name, role, topics });
  const ready = new Promise<void>((ok) => conn.onLink((s) => s === "open" && ok()));
  conn.start();
  return { conn, ready };
}

describe("画面とデーモン", () => {
  let gui: DaemonConnection, ai: DaemonConnection, state: AppState;
  let snap: Snapshot;

  beforeAll(async () => {
    const g = open("gui", "human", ["activity", "control", "clients", "target.state"]);
    state = new AppState(g.conn);
    state.subscribe((s) => (snap = s));
    const a = open("claude", "ai", []);
    gui = g.conn;
    ai = a.conn;
    await Promise.all([g.ready, a.ready]);
  });

  afterAll(() => {
    gui.stop();
    ai.stop();
  });

  it("利用者の一覧が届き、AI の操作が記録として流れる", async () => {
    await vi.waitFor(() => expect(snap.clients.map((c) => c.name).sort()).toEqual(["claude", "gui"]));
    expect(state.aiWorking).toBe(true);
    await ai.call("target.connect", { target: "stm32f401xe" });
    await ai.call("target.halt");
    await vi.waitFor(() => expect(snap.activity.map((e) => e.method)).toContain("target.halt"));
    expect(snap.activity.find((e) => e.method === "target.connect")?.client).toBe("claude");
    await vi.waitFor(() => expect(snap.target?.state).toBe("HALTED"));
    expect(snap.probe?.capabilities.board).toBe("fake");
    // ステータス バーの線・速さ・名前はターゲットの状態から（操作の記録に頼らない）
    expect(snap.target).toMatchObject({ target: "stm32f401xe", frequency_hz: 4_000_000, wire: "swd" });
  });

  it("一時停止で AI の要求が保留され、画面は操作でき、再開で AI に戻る", async () => {
    await state.act("session.control.pause");
    await vi.waitFor(() => expect(snap.control.paused).toBe(true));
    let aiDone = false;
    const aiCall = ai.call("target.resume").then(() => (aiDone = true));
    await vi.waitFor(() => expect(snap.control.held).toBe(1));
    await state.act("target.reset", { halt: true });
    expect(snap.error).toBeNull();
    await vi.waitFor(() => expect(snap.target?.state).toBe("HALTED"));
    expect(aiDone).toBe(false);
    await state.act("session.control.resume");
    await aiCall;
    await vi.waitFor(() => expect(snap.control).toEqual({ paused: false, paused_by: null, held: 0 }));
    await vi.waitFor(() => expect(snap.target?.state).toBe("RUNNING"));
    const methods = snap.activity.map((e) => `${e.client}:${e.method}`);
    expect(methods.indexOf("gui:target.reset")).toBeLessThan(methods.indexOf("claude:target.resume"));
  });

  it("電圧を読める", async () => {
    await state.readVoltage();
    expect(snap.voltage?.volts).toBe(3.3);
  });
});

describe("画面のデバッガとデーモン", () => {
  const elf = new URL("../../firmware/sram_demo/sram_demo.elf", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1");
  let gui: DaemonConnection, ai: DaemonConnection, app: AppState, dbg: Debugger;
  let d: DebugSnapshot;

  beforeAll(async () => {
    const g = open("gui-debug", "human", ["activity", "control", "clients", "target.state"]);
    app = new AppState(g.conn);
    dbg = new Debugger(g.conn, app);
    dbg.subscribe((s) => (d = s));
    const a = open("claude-debug", "ai", []);
    gui = g.conn;
    ai = a.conn;
    await Promise.all([g.ready, a.ready]);
  });

  afterAll(() => {
    gui.stop();
    ai.stop();
  });

  it("AI が RAM に読み込んだ ELF のソースが見え、画面で置いたブレークポイントで止まった行が開く", async () => {
    await ai.call("target.connect", { target: "stm32f401xe" });
    await ai.call("target.program_ram", { path: elf });
    await vi.waitFor(() => expect(d.files.map((f) => f.name)).toContain("sram_demo.c"));
    await dbg.openFile("sram_demo.c");
    expect(d.source?.text).toContain("reached_milestone");
    const line = d.source!.lines[d.source!.lines.length - 1];
    await dbg.toggleBreakpoint(line);
    expect(d.breakpoints.map((b) => b.line)).toEqual([line]);
    await dbg.addWatch("counter");
    await app.act("target.resume");
    await vi.waitFor(() => expect(d.stop?.line).toBe(line), { timeout: 3000 });
    expect(d.open).toBe("sram_demo.c");
    await vi.waitFor(() => expect(d.watches[0]?.value).toBe(256));
    expect(d.registers?.pc).toBe(d.stop?.pc);
    expect(d.backtrace.length).toBeGreaterThan(0);
    await dbg.toggleBreakpoint(line);
    expect(d.breakpoints).toEqual([]);
  });
});

describe("画面の波形とデーモン", () => {
  let gui: DaemonConnection, ai: DaemonConnection, app: AppState, tl: TimelineState;
  let t: TimelineSnapshot;
  const chars = (f: TimelineFrame, ch: number) => {
    const r = f.ids.indexOf(`wave:${ch}`);
    return Array.from({ length: f.width }, (_, p) => "01x-"[f.data[(r * f.width + p) * 4]]).join("");
  };

  beforeAll(async () => {
    const g = open("gui-wave", "human", ["activity", "control", "clients", "target.state"]);
    app = new AppState(g.conn);
    tl = new TimelineState(g.conn, app);
    tl.subscribe((s) => (t = s));
    tl.setWidth(400);
    const a = open("claude-wave", "ai", []);
    gui = g.conn;
    ai = a.conn;
    await Promise.all([g.ready, a.ready]);
  });

  afterAll(() => {
    gui.stop();
    ai.stop();
    tl.dispose();
  });

  it("AI の捕捉が自動で表示され、バイナリのタイルから間引いた波形は la.view と同じ。画面からも捕捉できる", async () => {
    await ai.call("la.capture", {});
    await vi.waitFor(() => expect(t.capture?.length).toBe(8192));
    await vi.waitFor(() => expect(t.frame?.ids.filter((id) => id.startsWith("wave:")).length).toBe(10));
    const id = t.capture!.id;
    expect(chars(t.frame!, 0)).toMatch(/^x+$/);                // 時計は全体表示では画素ごとに変化
    for (let i = 0; i < 12; i++) tl.zoomAt(0, 0.5);            // 左端を中心に拡大
    await vi.waitFor(() => expect(t.frame).toMatchObject({ v0: t.view.start, v1: t.view.end }));
    expect(t.view.end - t.view.start).toBeLessThan(100);
    expect(chars(t.frame!, 0)).toMatch(/0+.*1+/);              // 拡大すると 0 と 1 の区間が見える
    tl.setView({ start: 1234, end: 6789 });
    await vi.waitFor(() => expect(t.frame).toMatchObject({ v0: 1234, v1: 6789 }));
    const view = await ai.call("la.view", { start: 1234, end: 6789, width: 400 });
    expect(Array.from({ length: 10 }, (_, r) => chars(t.frame!, r))).toEqual(view.levels);   // AI と同じ定義
    await tl.capture({ rate_hz: 25e6, pretrigger: 256, deep: false });
    await vi.waitFor(() => expect(t.capture!.id).toBeGreaterThan(id));
    expect(t.view).toEqual({ start: 0, end: 8192 });
  });
});

describe("画面の状態（ui.state）", () => {
  it("AI が置いた視点・カーソルが画面の窓へ通知され、窓の変更は AI が読める", async () => {
    const g = new DaemonConnection(url(), { name: "gui", role: "human", topics: ["activity"], optionalTopics: ["ui.state"] });
    const a = open("claude", "ai", []);
    const ready = new Promise<void>((ok) => g.onLink((s) => s === "open" && ok()));
    const seen: any[] = [];
    g.on("ui.state", (n) => seen.push(n));
    g.start();
    await Promise.all([ready, a.ready]);
    await a.conn.call("ui.state.set", { view: { source: "capture", t0: 10, t1: 500 }, cursors: { selection: 42 } });
    await vi.waitFor(() => expect(seen.at(-1)?.state.cursors.selection).toBe(42));
    expect(seen.at(-1).state.view).toEqual({ source: "capture", t0: 10, t1: 500 });
    await g.call("ui.state.set", { frame: 1, origin: "w-test" });
    expect((await a.conn.call("ui.state.get")).frame).toBe(1);
    await vi.waitFor(() => expect(seen.at(-1)?.origin).toBe("w-test"));
    await a.conn.call("ui.state.set", { clear: ["view", "cursors", "frame"] });
    g.stop();
    a.conn.stop();
  });
});
