// 変数の観測をビューで分け合う: 始めるときは足すだけ、やめるときは自分だけの式を除く、取り合いの検出
import { afterEach, describe, expect, it } from "vitest";
import { claim, missing, planStart, planStop, release, startWatch, stopWatch } from "../src/lib/watchshare";
import { FakeConn } from "./fake-conn";

function daemon(names: string[], rate = 1000) {
  const state = { names, rate };
  const c = new FakeConn({
    "watch.status": () => ({ watches: state.names.map((name) => ({ name, method: "sampler" })), rate_hz: state.rate }),
    "watch.set": ({ expressions, rate_hz }: any) => {
      state.names = expressions;
      state.rate = rate_hz ?? state.rate;
      return { watches: [] };
    },
    "watch.stop": () => {
      state.names = [];
      return { stopped: true };
    },
  });
  const call = c.call.bind(c) as any;
  return { c, state, call, act: call };
}

afterEach(() => {
  release("a");
  release("b");
});

describe("組の計算", () => {
  it("始める: いまの式の後ろに足りない式", () => {
    expect(planStart(["x", "y"], ["y", "z"])).toEqual(["x", "y", "z"]);
  });

  it("やめる: 自分の式だけ除く（ほかのビューが使う式・知らない式は残す）", () => {
    expect(planStop(["ai", "x", "y"], ["x", "y"], new Set(["y"]))).toEqual(["ai", "y"]);
  });

  it("取り合い: 自分の式のうち組に無いもの", () => {
    expect(missing(["x"], ["x", "y"])).toEqual(["y"]);
  });
});

describe("watch.* の操作", () => {
  it("取っている式に足す（置き換えない）。足すものが無く周期も同じなら何もしない", async () => {
    const d = daemon(["ai"]);
    expect(await startWatch(d.call, d.act, "a", ["x"], 1000)).toBe(true);
    expect(d.state.names).toEqual(["ai", "x"]);
    const sets = d.c.calls.filter(([m]) => m === "watch.set").length;
    await startWatch(d.call, d.act, "a", ["x"], 1000);
    expect(d.c.calls.filter(([m]) => m === "watch.set").length).toBe(sets);
    await startWatch(d.call, d.act, "a", ["x"], 100);                      // 周期を変える
    expect(d.state.rate).toBe(100);
    expect(d.c.quiet).toContain("watch.status");
  });

  it("やめると自分の式だけを外し、残りが無ければ watch.stop", async () => {
    const d = daemon([]);
    await startWatch(d.call, d.act, "a", ["x", "shared"], 1000);
    claim("b", ["shared"]);
    await stopWatch(d.call, d.act, "a", ["x", "shared"]);
    expect(d.state.names).toEqual(["shared"]);                             // b がまだ使う
    await stopWatch(d.call, d.act, "b", ["shared"]);
    expect(d.state.names).toEqual([]);
    expect(d.c.calls.at(-1)?.[0]).toBe("watch.stop");
  });
});
