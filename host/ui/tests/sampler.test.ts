// DAP サンプラの取り合いを画面の言葉で（どのパネル・誰が使っているか・止める操作）
import { describe, expect, it } from "vitest";
import { blockedBy, holderOf, readSampler } from "../src/lib/sampler";
import { FakeConn } from "./fake-conn";

const act = (seq: number, method: string, client: string, role = "human", error?: string) =>
  ({ seq, time: "", client, role, method, params: {}, ...(error ? { error } : {}) });

describe("DAP サンプラの持ち主", () => {
  it("道具をパネルに、使い始めた利用者を記録から引く", () => {
    const h = holderOf({ user: "watch", release: "watch.stop", since: 1 },
                       [act(1, "watch.set", "gui"), act(2, "watch.set", "claude", "ai"), act(3, "watch.set", "x", "ai", "bad")]);
    expect(h).toEqual({ user: "watch", panel: "dataSampling", title: "panel.dataSampling", release: "watch.stop",
                        client: "claude", role: "ai" });
    expect(holderOf({ user: "profile", release: "profile.stop", since: 1 }, [])?.client).toBeNull();
    expect(holderOf({ user: null, release: null, since: null }, [])).toBeNull();
  });

  it("自分の道具なら取り合いではない", () => {
    const h = holderOf({ user: "rtos", release: "rtos.timeline.stop", since: 1 }, []);
    expect(blockedBy(h, "rtos")).toBeNull();
    expect(blockedBy(h, "profile")?.panel).toBe("cpuLoad");
  });

  it("状態は記録に残さずに読み、古いデーモンでは null", async () => {
    const conn = new FakeConn({ "probe.sampler.status": { user: "watch", release: "watch.stop", since: 1 } });
    expect((await readSampler(conn as any))?.user).toBe("watch");
    expect(conn.quiet).toContain("probe.sampler.status");
    expect(await readSampler(new FakeConn({ "probe.sampler.status": () => { throw new Error("x"); } }) as any)).toBeNull();
  });
});
