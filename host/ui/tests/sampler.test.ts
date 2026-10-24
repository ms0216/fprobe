// DAP サンプラの取り合いを画面の言葉で（どのパネル・誰が使っているか・止める操作）
import { describe, expect, it } from "vitest";
import { blockedBy, holderOf, readSampler } from "../src/lib/sampler";
import { FakeConn } from "./fake-conn";

describe("DAP サンプラの持ち主", () => {
  it("道具をパネルに、使い始めた利用者はデーモンの事実（probe.sampler.status の client）をそのまま", () => {
    const h = holderOf({ user: "watch", release: "watch.stop", since: 1, client: { id: 2, name: "claude", role: "ai" } });
    expect(h).toEqual({ user: "watch", panel: "dataSampling", title: "panel.dataSampling", release: "watch.stop",
                        client: "claude", role: "ai" });
    // 分からなければ null（操作の記録から推さない）
    expect(holderOf({ user: "profile", release: "profile.stop", since: 1, client: null })?.client).toBeNull();
    expect(holderOf({ user: "profile", release: "profile.stop", since: 1 } as any)?.client).toBeNull();   // 古いデーモン
    expect(holderOf({ user: null, release: null, since: null, client: null })).toBeNull();
    expect(holderOf.length).toBe(1);                                    // 操作の記録を受け取らない
  });

  it("自分の道具なら取り合いではない", () => {
    const h = holderOf({ user: "rtos", release: "rtos.timeline.stop", since: 1, client: null });
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
