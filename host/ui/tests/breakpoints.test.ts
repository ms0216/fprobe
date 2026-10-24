// Breakpoints のパネルの計算: 動作の編集と引数、ログの表示と読み続け、ベクタキャッチ
import { describe, expect, it } from "vitest";
import {
  BreakpointLog, catchState, changesBreakpoints, changesCatch, editOf, formatLog, formatValue, logMarks, parseLogMessage,
  setParams, toggled,
  type LogEntry,
} from "../src/lib/breakpoints";
import { FakeConn } from "./fake-conn";
import { ints, varray, venum, vint, vna, vptr, vstruct } from "./value-kinds";

const entry = (seq: number, extra: Partial<LogEntry> = {}): LogEntry => ({
  seq, host_s: 1, address: 0x20000044, where: "reached_milestone+4 (sram_demo.c:13)", hit: seq, message: "milestone {value}",
  values: { value: vint(256 * seq) }, ...extra,
});

describe("Breakpoints", () => {
  it("Log Message の {式} が読む式になり、文はそのまま添える", () => {
    expect(parseLogMessage("  ")).toBeNull();
    expect(parseLogMessage("at {value} and {counter} / {value}")).toEqual(
      { log: ["value", "counter"], message: "at {value} and {counter} / {value}" });
    expect(parseLogMessage("here")).toEqual({ log: [], message: "here" });          // 式なしのログ（log = []）
  });

  it("編集の欄 ⇔ debug.breakpoint.set の引数（置き直すと替わる、全部空なら動作なし）", () => {
    expect(setParams("sram_demo.c:13", { condition: " counter > 3 ", hitCount: "2", logMessage: "" })).toEqual(
      { location: "sram_demo.c:13", condition: "counter > 3", ignore_count: 2, log: null, message: null });
    expect(setParams("tick", { condition: "", hitCount: "", logMessage: "n={counter}" })).toEqual(
      { location: "tick", condition: null, ignore_count: 0, log: ["counter"], message: "n={counter}" });
    expect(() => setParams("tick", { condition: "", hitCount: "-1", logMessage: "" })).toThrow();
    expect(editOf(null)).toEqual({ condition: "", hitCount: "", logMessage: "" });
    const action = { condition: "x", ignore_count: 3, log: ["value", "counter"], message: "milestone {value}", hits: 1,
                     last_error: null };
    expect(editOf(action)).toEqual({ condition: "x", hitCount: "3", logMessage: "milestone {value} {counter}" });
    expect(editOf({ ...action, message: null, log: ["a"] }).logMessage).toBe("{a}");       // AI が log だけで置いた
    expect(editOf({ ...action, log: null }).logMessage).toBe("");
    expect(editOf({ ...action, log: [], message: "here" }).logMessage).toBe("here");
  });

  it("ログの文は {式} を値に置き換え、message の無いものは 式 = 値", () => {
    expect(formatValue(vint(5))).toBe("5");
    expect(formatValue(vint(256))).toBe("256");
    expect(formatValue(vna("no symbol"))).toBe("<no symbol>");                  // 評価できなかった式
    expect(formatValue(venum("ON", 1))).toBe("ON (1)");
    expect(formatValue(vptr(0x2000, "int"))).toBe("0x2000");
    expect(formatValue(varray(ints(104, 0), 2, { string: "h" }))).toBe('"h"');
    // メンバー名が name・value でも構造体（前は列挙に見えた）
    expect(formatValue(vstruct({ name: vint(1), value: vint(2) }))).toBe(JSON.stringify(vstruct({ name: vint(1), value: vint(2) })));
    expect(formatValue(256)).toBe("256");                                          // kind の無い古い形は JSON のまま
    expect(formatLog(entry(1))).toBe("milestone 256");
    expect(formatLog(entry(1, { values: { value: vint(1), counter: vint(2) } }))).toBe("milestone 1 counter = 2");
    expect(formatLog(entry(1, { message: null, values: { a: vint(1), b: vint(2) } }))).toBe("a = 1, b = 2");
    expect(logMarks([entry(1, { t: 100 }), entry(2)])).toEqual([{ t: 100, label: "milestone 256" }]);
  });

  it("ログを since で続きだけ読み、読み手の位置がログに無ければ（API の last より後）0 から読み直す", async () => {
    let log = [entry(1), entry(2)];
    // 本物のデーモンと同じ: 項が無ければ next = since のまま（番号の戻りでは分からない）、last = いちばん新しい seq
    const responder = ({ since }: any) => {
      const items = log.filter((e) => e.seq > since);
      return { entries: items, next: items.length ? items[items.length - 1].seq : since, last: log.at(-1)?.seq ?? 0 };
    };
    const conn = new FakeConn({ "debug.breakpoint.log": responder });
    const feed = new BreakpointLog(conn.call.bind(conn) as any);
    let seen: LogEntry[] = [];
    feed.subscribe((e) => (seen = e));
    await feed.poll();
    expect(seen.map((e) => e.seq)).toEqual([1, 2]);
    log = [...log, entry(3)];
    await feed.poll();
    expect(seen.map((e) => e.seq)).toEqual([1, 2, 3]);
    expect(conn.calls.at(-1)?.[1]).toEqual({ since: 2, limit: 500 });
    expect(conn.quiet.every((m) => m === "debug.breakpoint.log")).toBe(true);
    feed.clear();
    expect(seen).toEqual([]);
    log = [entry(1)];                                     // 起動し直した（last 1 < since 3）
    await feed.poll();
    expect(seen.map((e) => e.seq)).toEqual([1]);
  });

  it("ベクタキャッチ: 切り替えた集合は表の順、コアに無いものは覚えて足していく", () => {
    expect(toggled(["hard_fault"], "core_reset", true)).toEqual(["core_reset", "hard_fault"]);
    expect(toggled(["core_reset", "hard_fault"], "core_reset", false)).toEqual(["hard_fault"]);
    const a = catchState({ enabled: ["hard_fault"], unsupported: ["secure_fault"] });
    expect(a).toEqual({ enabled: ["hard_fault"], unsupported: ["secure_fault"] });
    expect(catchState({ enabled: [] }, a)).toEqual({ enabled: [], unsupported: ["secure_fault"] });
  });

  it("読み直しのきっかけは変えた操作だけ（読むだけの呼び出しの記録で読み直さない）", () => {
    expect(changesCatch({ method: "target.vector_catch.get", params: {} })).toBe(false);     // 読み（記録に残らない）
    expect(changesCatch({ method: "target.vector_catch", params: { catch: [] } })).toBe(true);
    expect(changesCatch({ method: "target.vector_catch", params: { catch: [] }, error: "x" })).toBe(false);
    expect(changesBreakpoints({ method: "debug.breakpoint.flash", params: {} })).toBe(false);
    expect(changesBreakpoints({ method: "debug.breakpoint.flash", params: { enabled: false } })).toBe(true);
    expect(changesBreakpoints({ method: "debug.breakpoint.set", params: { location: "x" } })).toBe(true);
    expect(changesBreakpoints({ method: "debug.breakpoint.list" })).toBe(false);
    expect(changesBreakpoints({ method: "debug.breakpoint.log" })).toBe(false);
  });
});

describe("ログの画面の上限", () => {
  it("画面に持つ上限を超えたら古い行を捨て、捨てた数を数える（黙って捨てない）", async () => {
    const { BREAKPOINT_LOG_KEEP } = await import("../src/lib/breakpoints");
    const n = BREAKPOINT_LOG_KEEP + 4;
    const all = Array.from({ length: n }, (_, k) => entry(k + 1));
    const conn = new FakeConn({ "debug.breakpoint.log": { entries: all, next: n } });
    const feed = new BreakpointLog(conn.call.bind(conn) as any);
    let seen: LogEntry[] = [];
    feed.subscribe((e) => (seen = e));
    await feed.poll();
    expect(seen.length).toBe(BREAKPOINT_LOG_KEEP);
    expect(feed.discarded).toBe(4);
    feed.clear();
    expect(feed.discarded).toBe(0);
  });
});
