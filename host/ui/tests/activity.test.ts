// Activity: 役割の絞り込み・誤りだけ、行 ↔ 記録の時刻（host.call の seq と time）
import { describe, expect, it } from "vitest";
import { HOST_CALLS_SQL, HostCallIndex, filterActivity, loadHostCalls, roleOf } from "../src/lib/activity";
import type { ActivityEntry } from "../src/lib/state";
import { FakeConn } from "./fake-conn";

const entry = (seq: number, client: string, role: string, error?: string): ActivityEntry =>
  ({ seq, time: `2026-10-18T10:00:0${seq}.000`, client, role, method: "m", params: {}, ...(error ? { error } : {}) });

describe("絞り込み", () => {
  const rows = [entry(1, "claude", "ai"), entry(2, "gui", "human", "bad"), entry(3, "hook 4", "human"), entry(4, "ci", "script")];
  it("フックは役割 hook に分ける", () => {
    expect(rows.map(roleOf)).toEqual(["ai", "human", "hook", "script"]);
  });
  it("利用者・役割・誤りだけ", () => {
    expect(filterActivity(rows, { role: "human" }).map((e) => e.seq)).toEqual([2]);
    expect(filterActivity(rows, { role: "hook" }).map((e) => e.seq)).toEqual([3]);
    expect(filterActivity(rows, { errorsOnly: true }).map((e) => e.seq)).toEqual([2]);
    expect(filterActivity(rows, { who: "claude", role: "" }).map((e) => e.seq)).toEqual([1]);
  });
});

describe("記録の時刻", () => {
  it("seq と time の組で引く（別の起動の同じ番号と混ぜない）。時刻から直前の操作", () => {
    const index = new HostCallIndex([[500, 2, "2026-10-18T10:00:02.000"], [100, 1, "2026-10-18T10:00:01.000"],
                                     [900, 2, "2026-10-17T09:00:00.000"], [950, null, null]]);
    expect(index.size).toBe(3);
    expect(index.timeOf(entry(2, "gui", "human"))).toBe(500);
    expect(index.timeOf(entry(3, "gui", "human"))).toBeUndefined();
    expect(index.keyAt(600)).toBe("2@2026-10-18T10:00:02.000");
    expect(index.keyAt(50)).toBeUndefined();
  });

  it("いまの記録の操作を recording.query で全部読む。記録が無ければ null", async () => {
    const c = new FakeConn({ "recording.status": { name: "r1" },
                             "recording.query": { columns: [], rows: [[10, 1, "2026-10-18T10:00:01.000"]] } });
    const got = await loadHostCalls(c.call.bind(c) as any);
    expect(got?.name).toBe("r1");
    expect(got?.index.timeOf(entry(1, "x", "ai"))).toBe(10);
    expect(c.calls[1]).toEqual(["recording.query", { sql: HOST_CALLS_SQL, limit: 100000 }]);
    expect(c.quiet).toEqual(["recording.status", "recording.query"]);
    expect(await loadHostCalls(new FakeConn({ "recording.status": { name: null } }).call as any)).toBeNull();
  });
});
