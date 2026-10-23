import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DaemonConnection, RpcError, TOOL_NAMESPACES, errorText, type LinkState } from "../src/lib/api";
import { FakeWebSocket } from "./fake-ws";

function connect(token?: string) {
  const links: [LinkState, any][] = [];
  const conn = new DaemonConnection("ws://x/api", {
    name: "gui", role: "human", token, topics: ["activity", "control"],
    WebSocketImpl: FakeWebSocket as unknown as typeof WebSocket,
  });
  conn.onLink((state, hello) => links.push([state, hello]));
  conn.start();
  return { conn, links };
}

function openAndGreet() {
  const ws = FakeWebSocket.last;
  ws.open();
  ws.reply("session.hello", { client_id: 1 });
  return ws;
}

describe("DaemonConnection", () => {
  beforeEach(() => {
    FakeWebSocket.instances = [];
    vi.useFakeTimers();
  });
  afterEach(() => vi.useRealTimers());

  it("名乗ってから購読し、両方の応答で open になる", async () => {
    const { links } = connect("s3cret");
    const ws = openAndGreet();
    expect(ws.sent[0]).toMatchObject({
      method: "session.hello",
      params: { name: "gui", role: "human", api_version: 1, token: "s3cret" },
    });
    await vi.waitFor(() => expect(ws.sent[1]?.method).toBe("session.subscribe"));
    expect(ws.sent[1].params).toEqual({ topics: ["activity", "control"] });
    ws.reply("session.subscribe", {});
    await vi.waitFor(() => expect(links.at(-1)).toEqual(["open", { client_id: 1 }]));
  });

  it("古いデーモンが知らない通知（optionalTopics）で購読を断られたら、それを外して購読し直す", async () => {
    const links: [LinkState, any][] = [];
    const conn = new DaemonConnection("ws://x/api", {
      name: "gui", role: "human", topics: ["activity"], optionalTopics: ["ui.state"],
      WebSocketImpl: FakeWebSocket as unknown as typeof WebSocket,
    });
    conn.onLink((state, hello) => links.push([state, hello]));
    conn.start();
    const ws = openAndGreet();
    await vi.waitFor(() => expect(ws.sent[1]?.params).toEqual({ topics: ["activity", "ui.state"] }));
    ws.receive({ jsonrpc: "2.0", id: ws.sent[1].id, error: { code: -32602, message: "session.subscribe: topics.1: 'ui.state' is not one of" } });
    await vi.waitFor(() => expect(ws.sent[2]?.params).toEqual({ topics: ["activity"] }));
    ws.reply("session.subscribe", {});
    await vi.waitFor(() => expect(links.at(-1)?.[0]).toBe("open"));
  });

  it("応答を id で対応付け、誤りは RpcError になる", async () => {
    const { conn, links } = connect();
    const ws = openAndGreet();
    await vi.waitFor(() => expect(ws.sent.length).toBe(2));
    ws.reply("session.subscribe", {});
    await vi.waitFor(() => expect(links.at(-1)?.[0]).toBe("open"));
    const a = conn.call("target.halt");
    const b = conn.call("target.status");
    const [ra, rb] = ws.sent.slice(2);
    ws.receive({ jsonrpc: "2.0", id: rb.id, result: { state: "HALTED" } });
    ws.receive({ jsonrpc: "2.0", id: ra.id, error: { code: -32020, message: "壊れた" } });
    await expect(b).resolves.toEqual({ state: "HALTED" });
    const err = await a.catch((e) => e);
    expect(err).toBeInstanceOf(RpcError);
    expect(err.code).toBe(-32020);
  });

  it("通知は購読した関数へ届く", async () => {
    const { conn } = connect();
    const seen: any[] = [];
    conn.on("control", (d) => seen.push(d));
    const ws = openAndGreet();
    ws.receive({ jsonrpc: "2.0", method: "control", params: { paused: true } });
    ws.receive({ jsonrpc: "2.0", method: "activity", params: { seq: 1 } });
    expect(seen).toEqual([{ paused: true }]);
  });

  it("未接続の call はすぐ失敗する", async () => {
    const { conn } = connect();
    await expect(conn.call("target.halt")).rejects.toThrow(/Not connected to the daemon/);
  });

  it("切れたら保留中の call を失敗させ、1 秒・2 秒と間をあけてつなぎ直す", async () => {
    const { conn, links } = connect();
    const ws = openAndGreet();
    await vi.waitFor(() => expect(ws.sent.length).toBe(2));
    ws.reply("session.subscribe", {});
    await vi.waitFor(() => expect(links.at(-1)?.[0]).toBe("open"));
    const pending = conn.call("target.halt");
    ws.drop();
    await expect(pending).rejects.toThrow(/lost/);                      // 既定は英語の表
    expect(links.at(-1)?.[0]).toBe("closed");
    expect(FakeWebSocket.instances.length).toBe(1);
    vi.advanceTimersByTime(999);
    expect(FakeWebSocket.instances.length).toBe(1);
    vi.advanceTimersByTime(1);
    expect(FakeWebSocket.instances.length).toBe(2);
    FakeWebSocket.last.drop();
    vi.advanceTimersByTime(1999);
    expect(FakeWebSocket.instances.length).toBe(2);
    vi.advanceTimersByTime(1);
    expect(FakeWebSocket.instances.length).toBe(3);
  });

  it("stop の後はつなぎ直さない", () => {
    const { conn } = connect();
    openAndGreet();
    conn.stop();
    vi.advanceTimersByTime(20000);
    expect(FakeWebSocket.instances.length).toBe(1);
  });
});

describe("DaemonConnection の名乗りの誤り", () => {
  beforeEach(() => {
    FakeWebSocket.instances = [];
    vi.useFakeTimers();
  });
  afterEach(() => vi.useRealTimers());

  function refuse(code: number) {
    const { links } = connect();
    const ws = FakeWebSocket.last;
    ws.open();
    ws.receive({ jsonrpc: "2.0", id: ws.sent[0].id, error: { code, message: "断る" } });
    return links;
  }

  it("断られた理由を closed と一緒に渡し、つなぎ直す", async () => {
    const links = refuse(-32012);
    await vi.waitFor(() => expect(links.at(-1)?.[0]).toBe("closed"));
    expect(links.at(-1)?.[1]).toBeInstanceOf(RpcError);
    vi.advanceTimersByTime(1000);
    expect(FakeWebSocket.instances.length).toBe(2);
  });

  it("API の版が合わないときは、つなぎ直しても直らないので止める", async () => {
    const links = refuse(-32010);
    await vi.waitFor(() => expect(links.at(-1)?.[0]).toBe("closed"));
    vi.advanceTimersByTime(30000);
    expect(FakeWebSocket.instances.length).toBe(1);
  });
});

describe("errorText", () => {
  it("デーモンが付ける例外の名前を外し、本文だけにする", () => {
    expect(errorText(new RpcError(-32020, "RuntimeError: DAP サンプラは watch が使っている"))).toBe("DAP サンプラは watch が使っている");
    expect(errorText(new RpcError(-32020, "KeyError: \"シンボル 'x' が無い\""))).toBe("\"シンボル 'x' が無い\"");
    expect(errorText(new RpcError(-32020, "ExpressionError: void は構造体ではない"))).toBe("void は構造体ではない");
    expect(errorText(new RpcError(-32021, "NotConnected: ターゲット未接続"))).toBe("ターゲット未接続");
    expect(errorText(new RpcError(-32021, "NotDisconnected: 接続中は使えない"))).toBe("接続中は使えない");
    expect(errorText(new Error("ふつうの誤り: 本文"))).toBe("ふつうの誤り: 本文");
    expect(errorText("文字列")).toBe("文字列");
  });

  it("AI 向けの道具の名前・引数の案内（target_halt・live=true 等）は利用者に見せない", () => {
    expect(errorText(new RpcError(-32020, "RuntimeError: ターゲットが動いている: 止めてから（target_halt）。一覧は live=true で走行中にも読める")))
      .toBe("ターゲットが動いている: 止めてから");
    expect(errorText(new RpcError(-32020, "RuntimeError: DAP サンプラは watch が使っている（watch_stop を先に）")))
      .toBe("DAP サンプラは watch が使っている");
    expect(errorText(new RpcError(-32023, "NoData: プロファイルが無い（profile_start）"))).toBe("プロファイルが無い");
    expect(errorText(new RpcError(-32020, "KeyError: 変数が無い（my_var）"))).toBe("変数が無い（my_var）");   // 利用者の名前は残す
  });

  it("道具の名前の頭は API のすべての名前空間を含む", () => {
    const spec = JSON.parse(readFileSync(resolve(import.meta.dirname, "../../api/fprobe_api.json"), "utf8"));
    const spaces = new Set(Object.keys(spec.methods).map((m: string) => m.split(".")[0]));
    for (const ns of spaces) expect(TOOL_NAMESPACES, ns).toContain(ns);
  });
});
