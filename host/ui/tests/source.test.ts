// 使われ方の記録の「どこから」（lib/source.ts）: 窓・共有の部品ごとに、要求に source を付けて送る
import { describe, expect, it } from "vitest";
import { sourcedApp, sourcedConn } from "../src/lib/source";

const fakeConn = () => {
  const calls: any[] = [];
  return { calls, call: (m: string, p?: any, o?: any) => { calls.push([m, p, o]); return Promise.resolve(1); },
           on: () => () => {}, onLink: () => () => {} };
};

describe("sourcedConn", () => {
  it("要求に source を付ける。呼び手の quiet はそのまま、呼び手が source を渡せばそれを使う", async () => {
    const conn = fakeConn();
    const c = sourcedConn(conn as any, "panel:memory");
    await c.call("target.memory.read" as never, { address: 0 } as never, { quiet: true });
    await c.call("target.halt" as never, undefined, { source: "panel:other" } as never);
    expect(conn.calls).toEqual([["target.memory.read", { address: 0 }, { source: "panel:memory", quiet: true }],
                                ["target.halt", undefined, { source: "panel:other" }]]);
  });
});

describe("sourcedApp", () => {
  it("act だけ source を付け、ほか（状態・購読）は元のものを指す", async () => {
    const seen: any[] = [];
    const app = { n: 3, act(m: string, p?: any, o?: any) { seen.push([m, o]); return Promise.resolve(this.n); },
                  subscribe(fn: (v: number) => void) { fn(this.n); return () => {}; } };
    const a = sourcedApp(app as any, "panel:registers");
    expect(await a.act("target.halt" as never)).toBe(3);
    expect(seen).toEqual([["target.halt", { source: "panel:registers" }]]);
    let got = 0;
    a.subscribe((v: number) => (got = v));
    expect(got).toBe(3);
  });
});
