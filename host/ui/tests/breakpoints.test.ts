// Breakpoints のパネルの計算: 動作の編集と引数、ログの表示と読み続け、ベクタキャッチ
import { describe, expect, it } from "vitest";
import {
  BreakpointLog, catchState, changesBreakpoints, changesCatch, editOf, formatLog, formatValue, logMarks, parseLogMessage,
  setParams, toggled,
  type LogEntry,
} from "../src/lib/breakpoints";
import { FakeConn } from "./fake-conn";

const entry = (seq: number, extra: Partial<LogEntry> = {}): LogEntry => ({
  seq, host_s: 1, address: 0x20000044, where: "reached_milestone+4 (sram_demo.c:13)", hit: seq, message: "milestone {value}",
  values: { value: 256 * seq }, ...extra,
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
    expect(formatValue(5)).toBe("5");
    expect(formatValue(256)).toBe("256");
    expect(formatValue({ error: "no symbol" })).toBe("<no symbol>");
    expect(formatValue({ name: "ON", value: 1 })).toBe("ON (1)");
    expect(formatLog(entry(1))).toBe("milestone 256");
    expect(formatLog(entry(1, { values: { value: 1, counter: 2 } }))).toBe("milestone 1 counter = 2");
    expect(formatLog(entry(1, { message: null, values: { a: 1, b: 2 } }))).toBe("a = 1, b = 2");
    expect(logMarks([entry(1, { t: 100 }), entry(2)])).toEqual([{ t: 100, label: "milestone 256" }]);
  });

  it("ログを since で続きだけ読み、デーモンが起動し直したら読み直す", async () => {
    let log = [entry(1), entry(2)];
    const conn = new FakeConn({
      "debug.breakpoint.log": ({ since }: any) => {
        const items = log.filter((e) => e.seq > since);
        return { entries: items, next: items.length ? items[items.length - 1].seq : since };
      },
    });
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
    log = [entry(1)];                                     // 起動し直した（next 0 < since 3 になる）
    conn.responders["debug.breakpoint.log"] = ({ since }: any) =>
      ({ entries: log.filter((e) => e.seq > since), next: since > 1 ? 1 : log.filter((e) => e.seq > since).at(-1)?.seq ?? since });
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
