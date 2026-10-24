import { describe, expect, it } from "vitest";
import { confirmer } from "../src/lib/confirm";
import { Debugger, frameLine, markedAddresses, type DebugSnapshot } from "../src/lib/debugger";
import { AppState } from "../src/lib/state";
import { FakeConn } from "./fake-conn";

const SOURCE = { name: "main.c", path: "/src/main.c", text: "a\nb\nc\nd\n", lines: [2, 4] };

function setup(responders: Record<string, any> = {}) {
  let bps: any[] = [];
  const conn = new FakeConn({
    "session.status": { boot: "a", device: null, control: { paused: false, paused_by: null, held: 0 }, clients: [], target: null },
    "session.activity.list": { entries: [] },
    "probe.info": { capabilities: {} },
    "debug.source.list": { files: [{ name: "main.c", path: "/src/main.c", exists: true }, { name: "gone.h", path: "/x/gone.h", exists: false }] },
    "debug.source.read": () => SOURCE,
    "debug.breakpoint.list": () => ({ breakpoints: bps, watchpoints: [] }),
    "debug.breakpoint.set": ({ location }: any) => {
      const line = Number(location.split(":")[1]);
      bps = [...bps, { address: 0x100 + line, location, file: "main.c", line, where: `f (main.c:${line})` }];
      return bps.at(-1);
    },
    "debug.breakpoint.clear": ({ location }: any) => {
      bps = bps.filter((b) => b.location !== location);
      return { breakpoints: bps, watchpoints: [] };
    },
    "target.registers.read": { r0: 1, pc: 0x104 },
    "debug.backtrace": [{ pc: 0x104, where: "f+0 (main.c:4)", note: "", function: "f", file: "main.c", line: 4 }],
    "debug.evaluate": ({ expression }: any) => {
      if (expression === "nope") throw new Error("シンボル 'nope' が無い");
      return { expression, type: "uint32_t", value: 256 };
    },
    ...responders,
  });
  const app = new AppState(conn as any);
  const dbg = new Debugger(conn as any, app);
  let snap!: DebugSnapshot;
  dbg.subscribe((s) => (snap = s));
  return { conn, app, dbg, get: () => snap };
}

const settle = async () => {
  for (let i = 0; i < 5; i++) await new Promise((r) => setTimeout(r, 0));
};

describe("Debugger", () => {
  it("つながったらソースの一覧とブレークポイントを読む", async () => {
    const { conn, get } = setup();
    conn.link!("open", {});
    await settle();
    expect(get().files.map((f) => f.name)).toEqual(["main.c", "gone.h"]);
    expect(get().breakpoints).toEqual([]);
  });

  it("止まったら、止まったファイルを開いてレジスタ・呼び出し履歴・変数を読み直す。同じ pc では読み直さない", async () => {
    const { conn, dbg, get } = setup();
    conn.link!("open", {});
    await settle();
    dbg.addWatch("counter");
    await settle();
    conn.emit("target.state", { state: "HALTED", pc: 0x104, function: "f", file: "main.c", line: 4, where: "f+0 (main.c:4)" });
    await settle();
    const s = get();
    expect(s.open).toBe("main.c");
    expect(s.source?.lines).toEqual([2, 4]);
    expect(s.stop?.line).toBe(4);
    expect(s.registers).toEqual({ r0: 1, pc: 0x104 });
    expect(s.backtrace[0].where).toMatch(/main.c:4/);
    expect(s.watches).toEqual([{ name: "counter", type: "uint32_t", value: 256 }]);
    expect(s.stale).toBe(false);
    const reads = conn.calls.filter(([m]) => m === "target.registers.read").length;
    conn.emit("target.state", { state: "HALTED", pc: 0x104, file: "main.c", line: 4, where: "f+0 (main.c:4)" });
    await settle();
    expect(conn.calls.filter(([m]) => m === "target.registers.read").length).toBe(reads);
    conn.emit("target.state", { state: "RUNNING" });
    expect(get().stale).toBe(true);
    expect(get().stop).toBeNull();
  });

  it("行を押すとブレークポイントを置き、もう一度押すと外す（命令のある行だけ）", async () => {
    const { conn, dbg, get } = setup();
    conn.link!("open", {});
    await settle();
    await dbg.openFile("main.c");
    await dbg.toggleBreakpoint(3);                 // 命令の無い行は何もしない
    expect(get().breakpoints).toEqual([]);
    await dbg.toggleBreakpoint(2);
    expect(get().breakpoints.map((b) => b.line)).toEqual([2]);
    expect(conn.calls.find(([m]) => m === "debug.breakpoint.set")?.[1]).toEqual({ location: "main.c:2" });
    await dbg.toggleBreakpoint(2);
    expect(get().breakpoints).toEqual([]);
  });

  it("逆アセンブルの命令の番地でブレークポイントを置き・外す（行で置いたものも番地で外せる）", async () => {
    let bps: any[] = [{ address: 0x102, location: "main.c:2", file: "main.c", line: 2, where: "f" }];
    const { conn, dbg, get } = setup({
      "debug.breakpoint.list": () => ({ breakpoints: bps, watchpoints: [] }),
      "debug.breakpoint.set": ({ location }: any) => {
        bps = [...bps, { address: parseInt(location, 16), location, where: location }];
        return bps.at(-1);
      },
      "debug.breakpoint.clear": ({ location }: any) => {
        bps = bps.filter((b) => b.location !== location);
        return { breakpoints: bps, watchpoints: [] };
      },
    });
    conn.emit("activity", { seq: 1, time: "", client: "claude", role: "ai", method: "debug.breakpoint.set", params: {} });
    await settle();
    expect(markedAddresses(get())).toEqual(new Set([0x102]));
    await dbg.toggleAddress(0x1a4);
    expect(conn.calls.find(([m]) => m === "debug.breakpoint.set")?.[1]).toEqual({ location: "0x000001A4" });
    expect(markedAddresses(get())).toEqual(new Set([0x102, 0x1a4]));
    await dbg.toggleAddress(0x102);
    expect(conn.calls.filter(([m]) => m === "debug.breakpoint.clear").at(-1)?.[1]).toEqual({ location: "main.c:2" });
    expect(get().breakpoints.map((b) => b.address)).toEqual([0x1a4]);
  });

  it("命令ステップは target.step を 1 命令", async () => {
    const { conn, dbg } = setup({ "target.step": {} });
    await dbg.stepInstruction();
    expect(conn.calls.at(-1)).toEqual(["target.step", { count: 1 }]);
  });

  it("ほかの利用者（AI）のブレークポイントや ELF の操作が記録に流れたら読み直す", async () => {
    const { conn, get } = setup({
      "debug.breakpoint.list": { breakpoints: [{ address: 1, location: "f", file: "main.c", line: 2, where: "f" }], watchpoints: [] },
    });
    conn.emit("activity", { seq: 1, time: "", client: "claude", role: "ai", method: "debug.breakpoint.set", params: {} });
    await settle();
    expect(get().breakpoints.map((b) => b.location)).toEqual(["f"]);
    const lists = conn.calls.filter(([m]) => m === "debug.source.list").length;
    conn.emit("activity", { seq: 2, time: "", client: "claude", role: "ai", method: "target.program_ram", params: {} });
    await settle();
    expect(conn.calls.filter(([m]) => m === "debug.source.list").length).toBe(lists + 1);
  });

  it("変数の誤りはその行に出し、外せる", async () => {
    const { conn, dbg, get } = setup();
    conn.link!("open", {});
    await settle();
    conn.emit("target.state", { state: "HALTED", pc: 0x104, where: "x" });
    await settle();
    await dbg.addWatch("nope");
    expect(get().watches[0].error).toMatch(/nope/);
    dbg.removeWatch("nope");
    expect(get().watches).toEqual([]);
  });

  it("この PC に無いソースは開かずに、無いことを示す", async () => {
    const { conn, dbg, get } = setup();
    conn.link!("open", {});
    await settle();
    await dbg.openFile("gone.h");
    expect(get().source).toBeNull();
    expect(get().open).toBe("gone.h");
    expect(conn.calls.some(([m]) => m === "debug.source.read")).toBe(false);
  });
});

describe("Debugger の順序", () => {
  it("ソースの一覧より先に止まった知らせが来ても、一覧が届いたら止まったファイルを開く", async () => {
    const { conn, get } = setup();
    conn.emit("target.state", { state: "HALTED", pc: 0x104, file: "main.c", line: 4, where: "f+0 (main.c:4)" });
    await settle();
    expect(get().open).toBeNull();
    conn.link!("open", {});
    await settle();
    expect(get().open).toBe("main.c");
    expect(get().source?.name).toBe("main.c");
  });
});

describe("Debugger（最終レビュー）", () => {
  it("同じ場所で止まり直しても（ループの中のブレークポイント）、実行の番号が変われば読み直す", async () => {
    const { conn, get } = setup();
    conn.link!("open", {});
    await settle();
    conn.emit("target.state", { state: "HALTED", pc: 0x104, file: "main.c", line: 4, where: "x", run: 1 });
    await settle();
    const reads = () => conn.calls.filter(([m]) => m === "target.registers.read").length;
    const first = reads();
    conn.emit("target.state", { state: "HALTED", pc: 0x104, file: "main.c", line: 4, where: "x", run: 2 });
    await settle();
    expect(reads()).toBe(first + 1);
    expect(get().stop?.line).toBe(4);
  });

  it("置いた番地の行が押した行とずれても（同じ番地に複数の行）、同じ行を押せば外れる", async () => {
    let bps: any[] = [];
    const { conn, dbg, get } = setup({
      "debug.breakpoint.list": () => ({ breakpoints: bps, watchpoints: [] }),
      "debug.breakpoint.set": ({ location }: any) => {
        bps = [{ address: 0x200, location, file: "main.c", line: 4, where: "f (main.c:4)" }];   // 2 行目を押すと 4 行目の番地
        return bps[0];
      },
      "debug.breakpoint.clear": ({ location }: any) => {
        bps = bps.filter((b) => b.location !== location);
        return {};
      },
    });
    conn.link!("open", {});
    await settle();
    await dbg.openFile("main.c");
    await dbg.toggleBreakpoint(2);
    expect(dbg.markedLines()).toEqual(new Set([2]));
    await dbg.toggleBreakpoint(2);
    expect(get().breakpoints).toEqual([]);
  });

  it("自動の読みの誤り（ターゲット未接続など）は画面の誤りに出さない", async () => {
    const { conn, app } = setup({
      "debug.breakpoint.list": () => { throw new Error("ターゲット未接続"); },
      "target.registers.read": () => { throw new Error("止まっていない"); },
    });
    let error: string | null = null;
    app.subscribe((s) => { if (s.error) error = s.error; });   // 一瞬でも出たら捕まえる
    conn.link!("open", {});
    await settle();
    conn.emit("target.state", { state: "HALTED", pc: 1, where: "x", run: 1 });
    await settle();
    expect(error).toBeNull();
  });

  it("ターゲットへの接続・切断の後はブレークポイントの一覧を読み直す（接続し直すと空になる）", async () => {
    const { conn } = setup();
    const lists = () => conn.calls.filter(([m]) => m === "debug.breakpoint.list").length;
    const before = lists();
    for (const method of ["target.connect", "target.disconnect", "target.program_ram"]) {
      conn.emit("activity", { seq: 1, client: "claude", role: "ai", method, time: "", params: {} });
    }
    await settle();
    expect(lists()).toBe(before + 3);
  });
});

describe("Debugger の古い応答", () => {
  it("ファイルを続けて開いたとき、遅れて返った前の応答で表示を戻さない", async () => {
    const resolvers: Record<string, (v: any) => void> = {};
    const { conn, dbg, get } = setup({
      "debug.source.list": { files: [{ name: "a.c", path: "/a.c", exists: true }, { name: "b.c", path: "/b.c", exists: true }] },
      "debug.source.read": ({ name }: any) => new Promise((r) => (resolvers[name] = r)),
    });
    conn.link!("open", {});
    await settle();
    const first = dbg.openFile("a.c");
    const second = dbg.openFile("b.c");
    await settle();
    resolvers["b.c"]({ name: "b.c", path: "/b.c", text: "b", lines: [] });
    await second;
    resolvers["a.c"]({ name: "a.c", path: "/a.c", text: "a", lines: [] });
    await first;
    expect(get().open).toBe("b.c");
    expect(get().source?.name).toBe("b.c");
  });
});

describe("Debugger の自動の読みは記録しない", () => {
  it("止まったときのレジスタ・呼び出し履歴・一覧の読みは quiet で頼む", async () => {
    const { conn, dbg } = setup();
    conn.link!("open", {});
    await settle();
    await dbg.addWatch("counter");
    conn.emit("target.state", { state: "HALTED", pc: 0x104, where: "x", run: 1 });
    await settle();
    expect(conn.quiet).toEqual(expect.arrayContaining(["target.registers.read", "debug.backtrace", "debug.breakpoint.list",
                                                       "debug.evaluate"]));
  });
});

describe("Debugger の変数の書き込み", () => {
  it("式に書いたら、ウォッチとローカル変数を読み直す（木の読み直しの合図 gen も進む）", async () => {
    let value = 1;
    const { conn, dbg, get } = setup({
      "debug.evaluate": ({ expression }: any) => ({ expression, type: "int", value }),
      "debug.frame_variables": () => ({ function: "f", arguments: [], locals: [{ name: "x", type: "int", value }] }),
      "debug.write_expression": ({ value: v }: any) => {
        value = v;
        return { expression: "x", type: "int", value };
      },
    });
    conn.link!("open", {});
    await settle();
    await dbg.addWatch("x");
    conn.emit("target.state", { state: "HALTED", pc: 0x104, where: "x", run: 1 });
    await settle();
    const gen = get().gen;
    expect(await dbg.writeExpression("x", 5)).toBe(true);
    expect(conn.calls.find(([m]) => m === "debug.write_expression")?.[1]).toEqual({ expression: "x", value: 5, frame: 0 });
    expect(get().watches[0].value).toBe(5);
    expect(get().locals?.locals[0].value).toBe(5);
    expect(get().gen).toBeGreaterThan(gen);
  });

  it("ほかの利用者が止まっている間にメモリ・変数を書いたら読み直す", async () => {
    const { conn, get } = setup({ "debug.frame_variables": { function: "f", arguments: [], locals: [] } });
    conn.link!("open", {});
    await settle();
    conn.emit("target.state", { state: "HALTED", pc: 0x104, where: "x", run: 1 });
    await settle();
    const gen = get().gen;
    conn.emit("activity", { seq: 9, time: "", client: "claude", role: "ai", method: "target.memory.write", params: {} });
    await settle();
    expect(get().gen).toBe(gen + 1);
  });

  it("構造体のメンバーの型は型ごとに 1 回だけ引く", async () => {
    const { conn, dbg } = setup({ "debug.type_info": { name: "struct S", members: [{ name: "a", type: "int" }, { name: null, type: "x" }] } });
    expect(await dbg.typeMembers("struct S")).toEqual({ a: "int" });
    expect(await dbg.typeMembers("struct S")).toEqual({ a: "int" });
    expect(conn.calls.filter(([m]) => m === "debug.type_info").length).toBe(1);
  });
});

describe("Debugger の呼び出し履歴", () => {
  it("フレームを選ぶと、そのフレームでローカル変数・ウォッチ・逆アセンブルを読み、その行のファイルを開く", async () => {
    const { conn, dbg, get } = setup({
      "debug.backtrace": [{ index: 0, pc: 0x104, where: "g+0 (main.c:4)", note: "" },
                          { index: 1, pc: 0x200, where: "f+8", note: "", function: "f", file: "/src/main.c", line: 2 }],
      "debug.frame_variables": ({ frame }: any) => ({ function: frame ? "f" : "g", arguments: [], locals: [{ name: `v${frame}`, value: frame }] }),
      "debug.disasm.around": ({ address }: any) => ({ instructions: [{ address: address ?? 0x104, mnemonic: "nop", operands: "" }] }),
    });
    conn.link!("open", {});
    await settle();
    await dbg.addWatch("x");
    conn.emit("target.state", { state: "HALTED", pc: 0x104, file: "main.c", line: 4, where: "g", run: 1 });
    await settle();
    expect(get().frame).toBe(0);
    await dbg.selectFrame(1);
    expect(get().frame).toBe(1);
    expect(get().locals?.locals[0].name).toBe("v1");
    expect(get().disasm[0].address).toBe(0x200);
    expect(conn.calls.filter(([m]) => m === "debug.evaluate").at(-1)?.[1]).toEqual({ expression: "x", frame: 1 });
    expect(get().open).toBe("main.c");
    await dbg.writeExpression("v1", 3);
    expect(conn.calls.find(([m]) => m === "debug.write_expression")?.[1].frame).toBe(1);
    // 止まり直したら止まった関数（0）に戻る
    conn.emit("target.state", { state: "HALTED", pc: 0x104, file: "main.c", line: 4, where: "g", run: 2 });
    await settle();
    expect(get().frame).toBe(0);
    expect(get().locals?.locals[0].name).toBe("v0");
  });

  it("フレームの場所はデーモンの欄 file・line だけ（where の文の形から拾わない）", () => {
    expect(frameLine({ pc: 1, where: "f+2 (dir/a.c:7)", note: "" })).toBeNull();
    expect(frameLine({ pc: 1, where: "x", note: "", file: "C:\\src\\b.c", line: 3 })).toEqual({ file: "b.c", line: 3 });
    expect(frameLine({ pc: 1, where: "0x100", note: "", file: null as any, line: null as any })).toBeNull();
  });

  it("番地の場所（showAddress）は debug.symbol.info の欄 file・line で（空白を含むパスも）", async () => {
    const { dbg } = setup({
      "debug.symbol.info": { address: 0x104, where: "f+4 (C:\\My Src\\main.c:12)", function: "f", offset: 4,
                             file: "C:\\My Src\\main.c", line: 12 },
      "debug.disasm.around": { instructions: [] },
    });
    expect(await dbg.showAddress(0x104)).toEqual({ file: "main.c", line: 12 });
  });

  it("番地の場所が分からなければ null（where の文字を解釈しない）", async () => {
    const { dbg } = setup({
      "debug.symbol.info": { address: 0x104, where: "f+4 (a.c:12)", function: "f", offset: 4, file: null, line: null },
      "debug.disasm.around": { instructions: [] },
    });
    expect(await dbg.showAddress(0x104)).toBeNull();
  });
});

describe("ライブ ウォッチ（走らせたまま読む・ELF だけで式を調べる）", () => {
  function live() {
    return setup({
      "debug.evaluate": ({ expression, live }: any) => {
        if (expression === "local_x") throw new Error("変数 'local_x' が無い（ローカル変数は止まっているときに読む）");
        return { expression, type: "uint32_t", value: live ? 7 : 256 };
      },
      "debug.expression.check": ({ expression }: any) => {
        if (expression === "1 +") throw new Error("式が途中で終わっている");
        return { expression, type: "int", live: true, needs_memory: false };
      },
    });
  }

  it("走っている間、ウォッチのパネルが開いていれば周期的に live で読む（閉じたら止める）", async () => {
    const { conn, dbg, get } = live();
    conn.link!("open", {});
    void dbg.addWatch("counter");
    conn.emit("target.state", { state: "RUNNING" });
    await settle();
    const stop = dbg.startLive(20);
    await new Promise((r) => setTimeout(r, 70));
    expect(get().watches[0]).toMatchObject({ name: "counter", value: 7, type: "uint32_t" });
    expect(get().watchMode).toBe("live");
    expect(conn.calls.filter(([m, p]) => m === "debug.evaluate" && p.live).length).toBeGreaterThanOrEqual(2);
    expect(conn.quiet).toContain("debug.evaluate");
    stop();
    await settle();
    const n = conn.calls.length;
    await new Promise((r) => setTimeout(r, 60));
    expect(conn.calls.length).toBe(n);
  });

  it("走っている間の誤り（ローカル変数）はその行に出す", async () => {
    const { conn, dbg, get } = live();
    conn.link!("open", {});
    conn.emit("target.state", { state: "RUNNING" });
    const stop = dbg.startLive(1000);
    await dbg.addWatch("local_x");
    await settle();
    expect(get().watches[0].error).toContain("止まっているとき");
    stop();
  });

  it("つないでいなければ ELF だけで式を調べ、型と式の誤りを出す", async () => {
    const { conn, dbg, get } = live();
    conn.link!("open", {});
    conn.emit("target.state", { state: "DISCONNECTED" });
    const stop = dbg.startLive(1000);
    await dbg.addWatch("count");
    await dbg.addWatch("1 +");
    await settle();
    expect(get().watches[0]).toMatchObject({ name: "count", type: "int" });
    expect(get().watches[0].value).toBeUndefined();
    expect(get().watches[1].error).toContain("途中");
    expect(get().watchMode).toBe("elf");
    expect(conn.calls.some(([m]) => m === "debug.evaluate")).toBe(false);
    stop();
  });

  it("走っている間に木のポインタを開くと live で読む", async () => {
    const { conn, dbg } = live();
    conn.link!("open", {});
    conn.emit("target.state", { state: "RUNNING" });
    await settle();
    expect((await dbg.evaluate("*p")).value).toBe(7);
  });

  it("走っている間の書き込みは watch.write（フレームを付けない）で、書いたらウォッチを読み直す", async () => {
    const { conn, dbg } = live();
    conn.responders["watch.write"] = ({ expression, value }: any) => ({ expression, value });
    conn.link!("open", {});
    await dbg.addWatch("counter");
    conn.emit("target.state", { state: "RUNNING" });
    const stop = dbg.startLive(1000);
    await settle();
    const before = conn.calls.filter(([m]) => m === "debug.evaluate").length;
    expect(await dbg.writeExpression("counter", 3)).toBe(true);
    expect(conn.calls.find(([m]) => m === "watch.write")?.[1]).toEqual({ expression: "counter", value: 3 });
    expect(conn.calls.some(([m]) => m === "debug.write_expression")).toBe(false);
    expect(conn.calls.filter(([m]) => m === "debug.evaluate").length).toBeGreaterThan(before);
    stop();
  });

  it("つないでいなければ書かない", async () => {
    const { conn, dbg } = live();
    conn.link!("open", {});
    conn.emit("target.state", { state: "DISCONNECTED" });
    await settle();
    expect(await dbg.writeExpression("counter", 3)).toBe(false);
    expect(conn.calls.some(([m]) => m === "watch.write" || m === "debug.write_expression")).toBe(false);
  });

  it("AI の作業中は確かめ、断ったら書かない（Watch・Locals の書き込みも確認を通す）", async () => {
    const { conn, dbg } = live();
    conn.link!("open", {});
    conn.emit("target.state", { state: "RUNNING" });
    await settle();
    conn.emit("clients", { clients: [{ id: 1, name: "claude", role: "ai" }] });
    const answer = new Promise<void>((done) => {
      const off = confirmer.subscribe((r) => {
        if (!r) return;
        queueMicrotask(() => {
          off();
          confirmer.answer(false);
          done();
        });
      });
    });
    const wrote = dbg.writeExpression("counter", 3);
    await answer;
    expect(await wrote).toBe(false);
    expect(conn.calls.some(([m]) => m === "watch.write")).toBe(false);
  });
});
