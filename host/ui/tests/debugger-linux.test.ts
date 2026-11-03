// Linux のプロセスの全コア停止デバッグ（linux.halt.debug.*）を、今あるデバッガの画面がマイコンと同じに描く。
// 画面は考えない層: target.state の linux 欄・API の応答をそのまま使い、Linux かどうかで道を分けない
// （仕様 docs/superpowers/specs/2026-11-03-linux-halt-ui-design.md）
import { describe, expect, it } from "vitest";
import { Debugger, markedLines, type DebugSnapshot } from "../src/lib/debugger";
import { AppState } from "../src/lib/state";
import { FakeConn } from "./fake-conn";

const RACE = "volatile int counter;\n\nvoid add_one(int who)\n{\n    int before = counter;\n    int after = before + 1;\n"
  + "    counter = after;\n}\n";
const LINUX = { pid: 100, comm: "race", tid: 101, thread_comm: "race", threads: [{ tid: 100, comm: "race" },
  { tid: 101, comm: "race" }], stop_kind: "swbreak", registers_from: "core", pc_error: null, elf: "/pc/race_arm",
  load_bias: 0x400000 };
const STOP = { state: "HALTED", target: "linux", pc: 0x4103f0, where: "add_one+36 (race.c:7)", function: "add_one",
  file: "race.c", line: 7, halt_reason: "breakpoint", run: 3, linux: LINUX };

function setup() {
  let bps: any[] = [];
  let attached = false;
  const conn = new FakeConn({
    "session.status": { boot: "a", device: null, control: { paused: false, paused_by: null, held: 0 }, clients: [], target: null },
    "session.activity.list": { entries: [] },
    "probe.info": { capabilities: {} },
    "debug.elf.status": () => ({ path: attached ? "/pc/race_arm" : null }),
    // PC に無いソース: attach のときにデーモンが対象から取り寄せた写し（fetched）
    "debug.source.list": () => ({ files: attached ? [{ name: "race.c", path: "/tmp/race.c", exists: false,
      fetched: { target: "pi", path: "/tmp/race.c", local_path: "C:/tmp/x/race.c", bytes: RACE.length, sha256: "00" } }]
      : [], same_name: {} }),
    "debug.source.read": () => ({ name: "race.c", path: "C:/tmp/x/race.c", text: RACE, lines: [5, 6, 7],
      fetched: { target: "pi", path: "/tmp/race.c", local_path: "C:/tmp/x/race.c", bytes: RACE.length, sha256: "00" } }),
    "debug.breakpoint.list": () => ({ breakpoints: bps, watchpoints: [] }),
    "debug.breakpoint.set": ({ location }: any) => {
      const line = Number(location.split(":")[1]);
      bps = [...bps, { address: 0x4103e0 + line, location, enabled: true, kind: "sw", file: "race.c", line,
        where: `add_one (race.c:${line})` }];
      return bps.at(-1);
    },
    "debug.breakpoint.clear": ({ location }: any) => {
      bps = bps.filter((b) => b.location !== location);
      return { breakpoints: bps, watchpoints: [] };
    },
    "target.registers.read": { r0: 2, sp: 0xbeffe800, pc: 0x4103f0, cpsr: 0x10 },
    "debug.backtrace": [{ index: 0, pc: 0x4103f0, where: "add_one+36 (race.c:7)", note: "", cfa: 0xbeffe80c,
      function: "add_one", offset: 36, file: "race.c", line: 7 }],
    "debug.frame_variables": { function: "add_one", arguments: [{ name: "who", type: "int", value: { kind: "int", value: 2 } }],
      locals: [{ name: "before", type: "int", value: { kind: "int", value: 41 } }] },
    "debug.disasm.around": { instructions: [] },
    "debug.evaluate": ({ expression }: any) => ({ expression, type: "int", value: { kind: "int", value: 41 } }),
  });
  const app = new AppState(conn as any);
  const dbg = new Debugger(conn as any, app);
  let snap!: DebugSnapshot;
  dbg.subscribe((s) => (snap = s));
  return { conn, app, dbg, get: () => snap, attach: (bp?: any) => { attached = true; if (bp) bps = [bp]; } };
}

const settle = async () => {
  for (let i = 0; i < 8; i++) await new Promise((r) => setTimeout(r, 0));
};
const ai = (method: string, seq: number) => ({ seq, time: "", client: "claude", role: "ai", method, params: {} });

describe("Debugger と Linux のプロセス（linux.halt.debug.*）", () => {
  it("AI が MCP で attach して止めると、ソースの強調・ブレークポイントの印・変数・呼び出し履歴が人と同じに出る", async () => {
    const { conn, get, attach } = setup();
    conn.link!("open", {});
    await settle();
    expect(get().files).toEqual([]);
    attach({ address: 0x4103e7, location: "race.c:7", enabled: true, kind: "sw", file: "race.c", line: 7,
      where: "add_one (race.c:7)" });
    conn.emit("activity", ai("linux.halt.debug.attach", 1));      // 読み直しの合図（ソースの一覧・ブレークポイント）
    await settle();
    expect(get().files.map((f) => f.name)).toEqual(["race.c"]);
    expect(get().breakpoints.map((b) => b.location)).toEqual(["race.c:7"]);
    conn.emit("target.state", STOP);                               // AI の linux.halt.debug.run が当たった
    await settle();
    const s = get();
    expect(s.open).toBe("race.c");                                 // PC に無いソースも開く（取り寄せた写し）
    expect(s.source?.text).toBe(RACE);
    expect(s.stop?.line).toBe(7);                                  // 止まった行の強調
    expect([...markedLines(s)]).toEqual([7]);                      // ブレークポイントの印
    expect(s.registers?.pc).toBe(0x4103f0);
    expect(s.backtrace[0].function).toBe("add_one");
    expect(s.locals?.arguments[0].name).toBe("who");
    expect(s.locals?.locals[0].value).toEqual({ kind: "int", value: 41 });
  });

  it("人が行を押すと debug.breakpoint.set が飛び、もう一度押すと debug.breakpoint.clear", async () => {
    const { conn, dbg, get, attach } = setup();
    attach();
    conn.link!("open", {});
    await settle();
    conn.emit("target.state", STOP);
    await settle();
    await dbg.toggleBreakpoint(6);
    expect(conn.calls.filter(([m]) => m === "debug.breakpoint.set")).toEqual([["debug.breakpoint.set", { location: "race.c:6" }]]);
    expect([...markedLines(get())]).toEqual([6]);
    await dbg.toggleBreakpoint(6);
    expect(conn.calls.some(([m, p]) => m === "debug.breakpoint.clear" && p.location === "race.c:6")).toBe(true);
    expect([...markedLines(get())]).toEqual([]);
  });

  it("AI の linux.halt.debug.break.insert・break.remove・close で、ブレークポイントとソースを読み直す", async () => {
    const { conn } = setup();
    conn.link!("open", {});
    await settle();
    const count = (m: string) => conn.calls.filter(([x]) => x === m).length;
    const lists = count("debug.breakpoint.list");
    conn.emit("activity", ai("linux.halt.debug.break.insert", 1));
    conn.emit("activity", ai("linux.halt.debug.break.remove", 2));
    await settle();
    expect(count("debug.breakpoint.list")).toBe(lists + 2);
    const sources = count("debug.source.list");
    conn.emit("activity", ai("linux.halt.debug.close", 3));
    await settle();
    expect(count("debug.source.list")).toBe(sources + 1);
  });
});

describe("ターゲットが無くなったら（linux.halt.debug.close・linux.halt.close・target.disconnect）", () => {
  it("止まっていたときのレジスタ・呼び出し履歴・変数・逆アセンブル・ウォッチの値を残さない（今の値のように見せない）", async () => {
    const { conn, dbg, get, attach } = setup();
    attach();
    conn.link!("open", {});
    await settle();
    dbg.addWatch("counter");
    conn.emit("target.state", STOP);
    await settle();
    expect(get().registers).not.toBeNull();
    expect(get().watches[0].value).toEqual({ kind: "int", value: 41 });
    conn.emit("target.state", { state: "DISCONNECTED", run: 4 });
    await settle();
    const s = get();
    expect(s.registers).toBeNull();
    expect(s.backtrace).toEqual([]);
    expect(s.locals).toBeNull();
    expect(s.disasm).toEqual([]);
    expect(s.stale).toBe(false);
    expect(s.stop).toBeNull();
    expect(s.watches).toEqual([{ name: "counter" }]);           // 式は残し、値は消す
  });

  it("走り出しただけなら、前に止まったときの値を古い印（stale）付きで残す（今までどおり）", async () => {
    const { conn, get, attach } = setup();
    attach();
    conn.link!("open", {});
    await settle();
    conn.emit("target.state", STOP);
    await settle();
    conn.emit("target.state", { ...STOP, state: "RUNNING", run: 4 });
    await settle();
    expect(get().stale).toBe(true);
    expect(get().registers).not.toBeNull();
  });
});


describe("ELF の更新（通知 elf.changed。点検 A3）", () => {
  it("見張りが読み込み直したら、ソースの一覧を読み直す（だれの操作でもない変化も画面に出る）", async () => {
    const { conn, attach } = setup();
    conn.link!("open", {});
    await settle();
    const before = conn.calls.filter(([m]) => m === "debug.source.list").length;
    attach();
    conn.emit("elf.changed", { path: "/pc/race_arm", reloaded: true });
    await settle();
    expect(conn.calls.filter(([m]) => m === "debug.source.list").length).toBe(before + 1);
  });
});
