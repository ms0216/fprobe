import { describe, expect, it } from "vitest";
import { RpcError } from "../src/lib/api";
import { AppState, type Snapshot } from "../src/lib/state";
import { FakeConn } from "./fake-conn";

const entry = (seq: number, extra: object = {}) => ({ seq, time: "2026-10-17T12:00:00.000", client: "claude",
  role: "ai", method: "target.halt", params: {}, result: { state: "HALTED" }, seconds: 0.01, ...extra });

function setup(responders: Record<string, any> = {}) {
  const conn = new FakeConn({
    "session.status": { device: { link: "usb", address: "0001" }, control: { paused: false, paused_by: null, held: 0 },
      clients: [{ id: 1, name: "gui", role: "human" }, { id: 2, name: "claude", role: "ai" }],
      target: { state: "RUNNING" } },
    "probe.info": { channels: 10 },
    "session.activity.list": ({ before }: any) => ({ entries: before ? [] : [entry(1), entry(2)] }),
    ...responders,
  });
  const state = new AppState(conn as any);
  let snap!: Snapshot;
  state.subscribe((s) => (snap = s));
  return { conn, state, get: () => snap };
}

const settle = () => new Promise((r) => setTimeout(r, 0));

describe("AppState", () => {
  it("つながったら状態・プローブ・記録を読む", async () => {
    const { conn, get } = setup();
    conn.link!("open", {});
    await settle();
    const s = get();
    expect(s.link).toBe("open");
    expect(s.device).toEqual({ link: "usb", address: "0001" });
    expect(s.clients.map((c) => c.name)).toEqual(["gui", "claude"]);
    expect(s.target).toEqual({ state: "RUNNING" });
    expect(s.probe).toEqual({ channels: 10 });
    expect(s.activity.map((e) => e.seq)).toEqual([1, 2]);
  });

  it("プローブの誤りがあっても他は読め、誤りを示す", async () => {
    const { conn, get } = setup({ "probe.info": () => { throw new RpcError(-32020, "USB が無い"); } });
    conn.link!("open", {});
    await settle();
    expect(get().device).not.toBeNull();
    expect(get().error).toMatch(/USB が無い/);
  });

  it("記録の通知は重複を除いて番号順に足す", async () => {
    const { conn, get } = setup();
    conn.link!("open", {});
    await settle();
    conn.emit("activity", entry(2));
    conn.emit("activity", entry(3));
    expect(get().activity.map((e) => e.seq)).toEqual([1, 2, 3]);
  });

  it("古い記録を読み足し、尽きたら hasOlder が偽", async () => {
    const { conn, state, get } = setup({
      "session.activity.list": ({ before }: any) =>
        ({ entries: before === undefined ? [entry(5), entry(6)] : before === 5 ? [entry(3), entry(4)] : [] }),
    });
    conn.link!("open", {});
    await settle();
    await state.loadOlder();
    expect(get().activity.map((e) => e.seq)).toEqual([3, 4, 5, 6]);
    expect(conn.calls.at(-1)).toEqual(["session.activity.list", { before: 5, limit: 100 }]);
    await state.loadOlder();
    expect(get().hasOlder).toBe(false);
  });

  it("つなぎ直しでデーモンの起動（session.status の boot）が変わっていたら一覧を置き換える（番号の戻りから推さない）", async () => {
    let restarted = false;
    const { conn, get } = setup({
      "session.status": () => ({ boot: restarted ? "b" : "a", device: null, control: { paused: false, paused_by: null, held: 0 },
                                 clients: [], target: null }),
      "session.activity.list": () => ({ entries: restarted ? [entry(1, { method: "probe.info" })] : [entry(7), entry(8)] }),
    });
    conn.link!("open", {});
    await settle();
    conn.link!("closed");
    expect(get().link).toBe("closed");
    restarted = true;
    conn.link!("open", {});
    await settle();
    expect(get().activity.map((e) => [e.seq, e.method])).toEqual([[1, "probe.info"]]);
  });

  it("つなぎ直しで番号が続いていれば足す", async () => {
    let second = false;
    const { conn, get } = setup({
      "session.activity.list": () => ({ entries: second ? [entry(8), entry(9)] : [entry(7), entry(8)] }),
    });
    conn.link!("open", {});
    await settle();
    second = true;
    conn.link!("open", {});
    await settle();
    expect(get().activity.map((e) => e.seq)).toEqual([7, 8, 9]);
  });

  it("操作権・利用者・ターゲットの通知を反映し、AI がつながっていて一時停止していないかの事実が分かる（作業中とは判定しない）", async () => {
    const { conn, state, get } = setup();
    conn.link!("open", {});
    await settle();
    expect(state).not.toHaveProperty("aiWorking");
    expect(state.aiConnectedUnpaused).toBe(true);
    conn.emit("control", { paused: true, paused_by: "gui", held: 2 });
    expect(get().control).toEqual({ paused: true, paused_by: "gui", held: 2 });
    expect(state.aiConnectedUnpaused).toBe(false);
    conn.emit("clients", { clients: [{ id: 1, name: "gui", role: "human" }] });
    conn.emit("control", { paused: false, paused_by: null, held: 0 });
    expect(state.aiConnectedUnpaused).toBe(false);
    conn.emit("target.state", { state: "HALTED", where: "main+4" });
    expect(get().target).toEqual({ state: "HALTED", where: "main+4" });
  });

  it("操作の誤りは error に入り、投げない", async () => {
    const { conn, state, get } = setup({ "target.halt": () => { throw new RpcError(-32021, "未接続"); } });
    conn.link!("open", {});
    await settle();
    await expect(state.act("target.halt")).resolves.toBeUndefined();
    expect(get().error).toBe("未接続");                     // 番号・例外の名前は出さない（lib/api.ts errorText）
  });

  it("電圧を読み直す", async () => {
    const { conn, state, get } = setup({ "probe.voltage": { volts: 3.29, live: true } });
    conn.link!("open", {});
    await settle();
    await state.readVoltage();
    expect(get().voltage).toEqual({ volts: 3.29, live: true });
  });
});

describe("AppState の競合", () => {
  it("状態の読みにターゲットが無くても、先に届いた通知を消さない", async () => {
    const { conn, get } = setup({
      "session.status": { device: null, control: { paused: false, paused_by: null, held: 0 }, clients: [], target: null },
    });
    conn.emit("target.state", { state: "HALTED" });
    conn.link!("open", {});
    await settle();
    expect(get().target).toEqual({ state: "HALTED" });
  });
});

describe("AppState のつなぎ直し（最終レビュー）", () => {
  const list = (from: number, to: number) => Array.from({ length: to - from + 1 }, (_, i) => entry(from + i));

  it("デーモンが別の起動なら、番号が手元より進んでいても置き換える", async () => {
    let boot = "a";
    let entries = list(1, 30);
    const { conn, get } = setup({
      "session.status": () => ({ boot, device: null, control: { paused: false, paused_by: null, held: 0 }, clients: [], target: null }),
      "session.activity.list": ({ before }: any) => ({ entries: before ? entries.filter((e) => e.seq < before).slice(-100) : entries.slice(-100) }),
    });
    conn.link!("open", {});
    await settle();
    boot = "b";
    entries = list(1, 150);
    conn.link!("open", {});
    await settle();
    expect(get().activity.map((e) => e.seq)).toEqual(list(51, 150).map((e) => e.seq));
    expect(get().hasOlder).toBe(true);
  });

  it("同じ起動で切れている間に 100 件を超えて流れても、間を読み足す", async () => {
    let entries = list(1, 30);
    const { conn, get } = setup({
      "session.status": { boot: "a", device: null, control: { paused: false, paused_by: null, held: 0 }, clients: [], target: null },
      "session.activity.list": ({ before, limit }: any) =>
        ({ entries: (before ? entries.filter((e) => e.seq < before) : entries).slice(-limit) }),
    });
    conn.link!("open", {});
    await settle();
    entries = list(1, 250);
    conn.link!("open", {});
    await settle();
    await settle();
    expect(get().activity.map((e) => e.seq)).toEqual(list(1, 250).map((e) => e.seq));
  });

  it("名乗りを断られたら理由を示す", () => {
    const { conn, get } = setup();
    conn.link!("closed", new RpcError(-32012, "トークンが違う"));
    expect(get().error).toBe("トークンが違う");
  });

  it("電圧の計測があれば、つながったときに電圧を読む", async () => {
    const { conn, get } = setup({ "probe.info": { capabilities: { aux: true } }, "probe.voltage": { volts: 3.3 } });
    conn.link!("open", {});
    await settle();
    await settle();
    expect(get().voltage).toEqual({ volts: 3.3 });
  });
});

describe("AppState（後回しにした指摘）", () => {
  it("裏の読みが成功しても、利用者がまだ読んでいない誤りを消さない", async () => {
    const { conn, state, get } = setup({ "target.halt": () => { throw new RpcError(-32021, "未接続"); } });
    conn.link!("open", {});
    await settle();
    await state.act("target.halt");
    await state.act("session.status");
    expect(get().error).toMatch(/未接続/);
    state.clearError();
    expect(get().error).toBeNull();
  });

  it("記録の一覧は新しい方から上限まで持ち、古い方は読み直せる", async () => {
    const { conn, get } = setup();
    conn.link!("open", {});
    await settle();
    for (let i = 3; i < 3 + 6000; i++) conn.emit("activity", entry(i));
    expect(get().activity.length).toBe(5000);
    expect(get().activity.at(-1)?.seq).toBe(6002);
    expect(get().hasOlder).toBe(true);
  });
});
