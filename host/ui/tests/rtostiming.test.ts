// RTOS の時間（lib/rtostiming.ts）: タブの API の読み（時刻の 0 と刻み・記録からの時間軸か）、名前ごとの表の行（API の順
// のまま）、最悪の回・最大の時刻の飛ぶ先
import { describe, expect, it } from "vitest";
import { methodFields } from "../src/lib/crosstrigger";
import { TAB_METHOD, pointReveal, readAnalysis, rowsOf, spanReveal } from "../src/lib/rtostiming";
import { FakeConn, deviceError } from "./fake-conn";

describe("readAnalysis", () => {
  it("タブの API を quiet で読み、t0 と刻みの秒（1 / timebase_hz）、記録から作った時間軸かを返す", async () => {
    const conn = new FakeConn({
      "rtos.timeline.jobs": { t0: 500, timebase_hz: 1e6, tasks: {} },
      "rtos.timeline.get": { window: { source: "recording" } },
    });
    const r = await readAnalysis(conn, "jobs", { task: "net" });
    expect(r.error).toBeNull();
    expect(r.analysis).toMatchObject({ t0: 500, tickS: 1e-6, inRecording: true });
    expect(conn.calls[0]).toEqual(["rtos.timeline.jobs", { task: "net" }]);
    expect(conn.quiet).toEqual(["rtos.timeline.jobs", "rtos.timeline.get"]);
  });

  it("時間軸が無い・memory=true でない等は API の誤りの文のまま", async () => {
    const conn = new FakeConn({ "rtos.timeline.memory": () => { throw deviceError("スタック・ヒープの語を読んでいない"); } });
    expect(await readAnalysis(conn, "stacks")).toEqual({ analysis: null, error: "スタック・ヒープの語を読んでいない" });
  });

  it("タブの API は仕様にある（欄は仕様のまま作る）", () => {
    for (const m of Object.values(TAB_METHOD)) expect(methodFields(m).length, m).toBeGreaterThan(0);
  });
});

describe("rowsOf", () => {
  it("名前ごとの表を API の順のまま行に（並べ替えない）", () => {
    expect(rowsOf({ zeta: { jobs: 1 }, alpha: { jobs: 9 } }).map((r) => [r.name, r.jobs])).toEqual([["zeta", 1], ["alpha", 9]]);
    expect(rowsOf(null)).toEqual([]);
  });
});

describe("飛ぶ先", () => {
  it("区間は start（応答時間は release）から end まで。時刻が無ければ飛ばない", () => {
    expect(spanReveal({ start: 10, end: 20 })).toEqual({ source: "recording", t: 10, tEnd: 20 });
    expect(spanReveal({ release: 5, end: 9 })).toEqual({ source: "recording", t: 5, tEnd: 9 });
    expect(spanReveal(null)).toBeNull();
    expect(pointReveal(42)).toEqual({ source: "recording", t: 42 });
    expect(pointReveal(null)).toBeNull();
  });
});
