// 主スタック（MSP）の使用量（lib/stack.ts）: target.stack.paint（塗る）と target.stack.usage（読む）
import { describe, expect, it } from "vitest";
import { readUsage, usagePercent } from "../src/lib/stack";
import { FakeConn, deviceError } from "./fake-conn";

describe("readUsage", () => {
  it("使用量をそのまま返す（読み取りは quiet）", async () => {
    const u = { bottom: 0x20000000, top: 0x20001000, size: 4096, used: 1024, fraction: 0.25, overflow: false, painted: true };
    const conn = new FakeConn({ "target.stack.usage": u });
    expect(await readUsage(conn)).toEqual({ usage: u, error: null });
    expect(conn.quiet).toEqual(["target.stack.usage"]);
  });

  it("塗っていない等の誤りは文で", async () => {
    const conn = new FakeConn({ "target.stack.usage": () => { throw deviceError("塗っていない"); } });
    expect(await readUsage(conn)).toEqual({ usage: null, error: "塗っていない" });
  });
});

describe("usagePercent", () => {
  it("API の fraction を百分率に（無ければ null）", () => {
    expect(usagePercent({ fraction: 0.256 })).toBe(25.6);
    expect(usagePercent({})).toBeNull();
  });
});

describe("boundsParams（範囲を人が明示する）", async () => {
  const { boundsParams } = await import("../src/lib/stack");
  it("書いた欄だけ（16 進・10 進の番地か、リンカのシンボルの名前）。空なら送らない", () => {
    expect(boundsParams("", "")).toEqual({ params: {} });
    expect(boundsParams("0x20000000", "0x20001000")).toEqual({ params: { bottom: 0x20000000, top: 0x20001000 } });
    // シンボルの名前は使う側が明示する（fprobe は _estack 等の名前の慣習で選ばない）
    expect(boundsParams("__StackLimit", "_estack")).toEqual({ params: { limit_symbol: "__StackLimit", top_symbol: "_estack" } });
    expect(boundsParams("0xZZ", "")).toEqual({ error: "bottom" });
    expect(boundsParams("", "a b")).toEqual({ error: "top" });
  });

  it("使用量の読み取りにも渡す", async () => {
    const { readUsage } = await import("../src/lib/stack");
    const conn = new FakeConn({ "target.stack.usage": { size: 4096 } });
    await readUsage(conn, { bottom: 1, top: 2 });
    expect(conn.calls).toEqual([["target.stack.usage", { bottom: 1, top: 2 }]]);
  });
});
