// 画面の状態の同期（lib/screen.ts）: URL のハッシュ・デーモンの ui.state との往復
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { InlineDecimator } from "../src/lib/timeline/decimator";
import { decodeFrame } from "../src/lib/timeline/frame";
import { Debugger } from "../src/lib/debugger";
import { ScreenSync, configShowing, emptyScreen, fromHash, toHash, type Place } from "../src/lib/screen";
import { AppState } from "../src/lib/state";
import { TimelineState } from "../src/lib/timeline/state";
import { FakeConn } from "./fake-conn";

const settle = async (ms = 0) => {
  await new Promise((r) => setTimeout(r, ms));
  for (let i = 0; i < 5; i++) await new Promise((r) => setTimeout(r, 0));
};

function setup(responders: Record<string, any> = {}, hash = "") {
  const conn = new FakeConn({
    "ui.state.get": { rev: 3, view: null, cursors: { selection: 40, mouse: null, reference: 10 }, rows: null, event: null, frame: 0 },
    "ui.state.set": (p: any) => ({ rev: 4, ...emptyScreen(), ...p, changed: [] }),
    "debug.backtrace": [0, 1, 2].map((index) => ({ index, pc: 0x100 + index, where: `f${index}`, note: "" })),
    ...responders,
  });
  const app = new AppState(conn as any);
  const tl = new TimelineState(conn as any, app);
  const dbg = new Debugger(conn as any, app);
  let url = hash;
  const place: Place = { hash: () => url, replace: (h) => (url = h) };
  const frames: number[] = [];
  const sync = new ScreenSync(conn as any, tl, dbg, (i) => {
    frames.push(i);
    void dbg.selectFrame(i);
  }, place, 5);
  return { conn, tl, dbg, sync, frames, url: () => url };
}

describe("URL のハッシュ", () => {
  it("視点・カーソル・段・出来事・フレームを載せ、ほかの項目（token）は保つ", () => {
    const d = { ...emptyScreen(), view: { source: "recording" as const, t0: 100, t1: 900 },
                cursors: { selection: 150, mouse: 7, reference: 120 }, rows: ["wave:0", "uart"],
                event: { stream: "uart", t: 160 }, frame: 2 };
    const hash = toHash("#token=abc", d);
    expect(hash).toBe("#token=abc&v=recording:100-900&sel=150&ref=120&rows=wave:0,uart&ev=uart@160&f=2");
    expect(fromHash(hash)).toEqual({ view: d.view, cursors: { selection: 150, mouse: null, reference: 120 },
                                     rows: ["wave:0", "uart"], event: { stream: "uart", t: 160 }, frame: 2 });
    expect(toHash("#token=abc", emptyScreen())).toBe("#token=abc");
    expect(fromHash("#v=capture:9-3&f=-1")).toEqual({});                      // 壊れた値は使わない
  });
});

describe("画面の状態の同期", () => {
  it("つながったらデーモンの状態を読んで当てる（カーソル）", async () => {
    const { conn, tl } = setup();
    conn.link!("open", {});
    await settle();
    expect(conn.calls.some(([m]) => m === "ui.state.get")).toBe(true);
    expect(tl.snapshot.selection).toBe(40);
    expect(tl.snapshot.reference).toBe(10);
  });

  it("手元の変化は少し待ってまとめて送る（origin 付き）。マウスだけなら記録に残さない", async () => {
    const { conn, tl, sync } = setup();
    conn.link!("open", {});
    await settle();
    tl.select(55);
    tl.setReference(20);
    await settle(20);
    const sets = conn.calls.filter(([m]) => m === "ui.state.set");
    expect(sets).toHaveLength(1);
    expect(sets[0][1]).toEqual({ cursors: { selection: 55, reference: 20 }, origin: sync.origin });
    expect(conn.quiet).not.toContain("ui.state.set");
    sync.setMouse(70);
    await settle(20);
    expect(conn.calls.filter(([m]) => m === "ui.state.set").at(-1)?.[1]).toEqual({ cursors: { mouse: 70 }, origin: sync.origin });
    expect(conn.quiet).toContain("ui.state.set");
  });

  it("データの読み直し（記録・捕捉が変わった）による視点の変化は記録に残さず、人が動かした視点は残す", async () => {
    const { conn, tl } = setup();
    conn.link!("open", {});
    await settle();
    conn.quiet.length = 0;
    (tl as any).set({ source: "recording", recording: "rec-2", extent: [0, 100], view: { start: 0, end: 100 } });
    await settle(20);
    expect(conn.calls.filter(([m]) => m === "ui.state.set").at(-1)?.[1].view).toEqual({ source: "recording", t0: 0, t1: 100 });
    expect(conn.quiet).toEqual(["ui.state.set"]);
    tl.setView({ start: 10, end: 50 });
    await settle(20);
    expect(conn.calls.filter(([m]) => m === "ui.state.set").at(-1)?.[1].view).toEqual({ source: "recording", t0: 10, t1: 50 });
    expect(conn.quiet).toEqual(["ui.state.set"]);                       // 人の操作は記録に残す
  });

  it("ほかの窓・AI の変化を当て、自分の変化の通知は読み飛ばす。フレームは止まっていれば移る", async () => {
    const { conn, tl, sync, dbg, frames } = setup();
    conn.link!("open", {});
    await settle();
    const state = { ...emptyScreen(), cursors: { selection: 99, mouse: null, reference: null }, frame: 1,
                    event: { stream: "uart", t: 99, kind: "line", text: "ok", row: "uart" } };
    conn.emit("ui.state", { rev: 5, origin: sync.origin, changed: ["cursors"], state });
    expect(tl.snapshot.selection).toBe(40);                                  // 自分の通知
    conn.emit("ui.state", { rev: 6, origin: null, changed: ["cursors", "event", "frame"], state });
    expect(tl.snapshot.selection).toBe(99);
    expect(tl.snapshot.selected?.stream).toBe("uart");
    expect(frames).toEqual([]);                                               // 止まっていない
    conn.emit("target.state", { state: "HALTED", pc: 1, where: "x", run: 1 });
    await settle();
    expect(dbg.snapshot.stop).not.toBeNull();
    conn.emit("ui.state", { rev: 7, origin: "other", changed: ["frame"], state: { ...state, frame: 2 } });
    expect(frames).toEqual([2]);
    await settle(20);
    expect(conn.calls.filter(([m]) => m === "ui.state.set").map(([, p]) => p)).toEqual([]);  // 当てた変化は送り返さない
  });

  it("URL に状態があれば、開いたときにそれを当ててデーモンへ送る。URL も手元に合わせて書き換える", async () => {
    const { conn, tl, sync, url } = setup({}, "#token=t&sel=77&f=0");
    conn.link!("open", {});
    await settle();
    expect(tl.snapshot.selection).toBe(77);
    expect(conn.calls.find(([m]) => m === "ui.state.set")?.[1])
      .toEqual({ cursors: { selection: 77, mouse: null, reference: null }, frame: 0, origin: sync.origin });
    tl.setReference(5);
    await settle(400);
    expect(url()).toBe("#token=t&sel=77&ref=5");
  });

  it("ほかの窓・AI のマウスのカーソルを時間軸へ（自分のものは除く。カーソルだけの変化では視点を当て直さない）", async () => {
    const { conn, tl, sync } = setup();
    conn.link!("open", {});
    await settle();
    const before = tl.snapshot.selection;
    conn.emit("ui.state", {
      rev: 9, origin: null, changed: ["pointers"], state: { ...emptyScreen(), cursors: { selection: 999, mouse: 70, reference: null } },
      pointers: [{ key: sync.origin, t: 1, source: "capture", name: "gui", role: "human", time: 0 },
                 { key: "client:7", t: 70, source: "recording", name: "claude", role: "ai", time: 0 }],
    });
    expect(tl.snapshot.pointers).toEqual([{ t: 70, source: "recording", label: "claude", role: "ai" }]);
    expect(tl.snapshot.selection).toBe(before);
  });

  it("切れたらほかの窓・AI のカーソルを消し、窓を閉じるときは自分のカーソルを消して送る", async () => {
    const { conn, tl, sync } = setup();
    conn.link!("open", {});
    await settle();
    conn.emit("ui.state", { rev: 9, origin: null, changed: ["pointers"], state: emptyScreen(),
                            pointers: [{ key: "client:7", t: 70, source: "recording", name: "claude", role: "ai", time: 0 }] });
    expect(tl.snapshot.pointers).toHaveLength(1);
    conn.link!("closed");
    expect(tl.snapshot.pointers).toEqual([]);
    conn.link!("open", {});
    await settle();
    sync.setMouse(33);
    await settle(20);
    conn.calls.length = 0;
    conn.quiet.length = 0;
    sync.leave();
    expect(conn.calls).toEqual([["ui.state.set", { cursors: { mouse: null }, origin: sync.origin }]]);
    expect(conn.quiet).toEqual(["ui.state.set"]);
    sync.leave();                                                     // もう無ければ送らない
    expect(conn.calls).toHaveLength(1);
  });

  it("伸びている記録の、画面がまだ読んでいない端の視点が届いたら、追いついてから当てる（追従で上書きしない）", async () => {
    // 2026-10-21 の実機: 記録中に「波形を捕捉」が記録の端の近くの視点を送ると、画面の範囲（1 秒ごとに読む）がまだ
    // 届かず捨て、画面の「全体・追従」の視点を送り返して上書きしていた
    const bytes = readFileSync(resolve(import.meta.dirname, "fixtures", "recording-raw.bin"));
    const raw = () => decodeFrame(bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength) as ArrayBuffer).result;
    let span: [number, number] = [0, 19999];
    const conn = new FakeConn({
      "ui.state.get": { rev: 3, view: null, cursors: { selection: null, mouse: null, reference: null }, rows: null, event: null, frame: 0 },
      "ui.state.set": (p: any) => ({ rev: 4, ...emptyScreen(), ...p, changed: [] }),
      "session.status": { boot: "a", device: null, control: { paused: false, paused_by: null, held: 0 }, clients: [], target: null },
      "session.activity.list": { entries: [] },
      "probe.info": { capabilities: {} },
      "recording.status": () => ({ recording: true, name: "rec-1", span, timebase_hz: 1e6 }),
      "timeline.tiles": () => raw(),
    });
    const app = new AppState(conn as any);
    const tl = new TimelineState(conn as any, app, new InlineDecimator(), 10);
    const dbg = new Debugger(conn as any, app);
    const place: Place = { hash: () => "", replace: () => {} };
    const sync = new ScreenSync(conn as any, tl, dbg, () => {}, place, 5);
    tl.setWidth(500);
    conn.link!("open", {});
    await settle();
    tl.setSource("recording");
    await settle(30);
    expect(tl.snapshot.extent).toEqual([0, 20000]);
    span = [0, 39999];                                              // 記録が伸びた（画面はまだ 20000 まで）
    const state = { ...emptyScreen(), view: { source: "recording" as const, t0: 30000, t1: 30200 },
                    cursors: { selection: 30100, mouse: null, reference: null } };
    const before = conn.calls.length;
    conn.emit("ui.state", { rev: 5, origin: "other", changed: ["view", "cursors"], state });
    await settle(60);
    expect(tl.snapshot.view).toEqual({ start: 30000, end: 30200 });
    const sent = conn.calls.slice(before).filter(([m]) => m === "ui.state.set").map(([, p]) => p as any);
    expect(sent.some((p) => p.view && p.view.t0 !== 30000)).toBe(false);   // 古い視点を送り返さない
    void sync;
  });

  it("ui.state の無いデーモンでは URL だけ", async () => {
    const { conn, tl, url } = setup({ "ui.state.get": () => { throw new Error("ui.state.get は無い"); } });
    conn.link!("open", {});
    await settle();
    tl.select(12);
    await settle(400);
    expect(conn.calls.filter(([m]) => m === "ui.state.set")).toHaveLength(0);
    expect(url()).toBe("#sel=12");
  });

  it("開く段だけを見せる構成（並び・畳みは保つ）", () => {
    const t: any = {
      config: { groups: ["logic"], rows: {}, hidden: ["wave:3"], shown: [], folded: ["uart"] },
      groups: [{ id: "logic", rows: [{ id: "wave:0" }, { id: "wave:1" }, { id: "wave:3" }] },
               { id: "uart", rows: [{ id: "uart.err", hiddenByDefault: true }] }],
    };
    expect(configShowing(t, ["wave:0", "wave:3", "uart.err"])).toEqual(
      { groups: ["logic"], rows: {}, hidden: ["wave:1"], shown: ["uart.err"], folded: ["uart"] });
  });
});
