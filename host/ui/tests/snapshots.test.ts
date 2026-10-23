// 故障の状態の保存（lib/snapshots.ts）: snapshot.list と snapshot.info を、Recordings の節の行と「開いている状態」に
import { describe, expect, it } from "vitest";
import { readSnapshots } from "../src/lib/snapshots";
import { FakeConn } from "./fake-conn";

describe("readSnapshots", () => {
  it("一覧（新しい順に並べ替えず、デーモンの順のまま）と開いている状態を読む。読み取りは quiet", async () => {
    const conn = new FakeConn({
      "snapshot.list": { directory: "/s", snapshots: [
        { name: "a", path: "/s/a.fsnap", bytes: 10, created: "2026-10-22T10:00:00", pc: 4096, where: "main.c:3", reason: "fault", note: "" },
        { name: "b", path: "/s/b.fsnap", error: "壊れている" }] },
      "snapshot.info": { open: true, path: "/s/a.fsnap", pc: 4096 },
    });
    const r = await readSnapshots(conn);
    expect(r.rows.map((s) => s.name)).toEqual(["a", "b"]);
    expect(r.rows[1].error).toBe("壊れている");
    expect(r.open?.path).toBe("/s/a.fsnap");
    expect(conn.quiet).toEqual(["snapshot.list", "snapshot.info"]);
  });

  it("開いていなければ open は null、読めなければ誤りの文", async () => {
    const conn = new FakeConn({ "snapshot.list": () => { throw new Error("RuntimeError: 置き場所が無い"); },
                                "snapshot.info": { open: false } });
    const r = await readSnapshots(conn);
    expect(r.rows).toEqual([]);
    expect(r.error).toBe("置き場所が無い");
    expect(r.open).toBeNull();
  });
});
