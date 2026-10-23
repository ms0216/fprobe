// ヒープ（lib/heap.ts）: heap.scan の引数と、heap.trace.report の読み取り
import { describe, expect, it } from "vitest";
import { readReport, scanParams } from "../src/lib/heap";
import { FakeConn } from "./fake-conn";

describe("scanParams", () => {
  it("アロケータを選ばなければ送らない（ELF にあるもの全部）。走行中は live", () => {
    expect(scanParams({ allocator: "", live: false, compare: false })).toEqual({});
    expect(scanParams({ allocator: "freertos", live: true, compare: true })).toEqual({ allocator: "freertos", live: true, compare: true });
  });
});

describe("readReport", () => {
  it("記録の要約を quiet で読む。誤りは文で", async () => {
    const conn = new FakeConn({ "heap.trace.report": { allocations: 3, frees: 1, issues: [{ kind: "double_free" }] } });
    expect(await readReport(conn)).toEqual({ report: { allocations: 3, frees: 1, issues: [{ kind: "double_free" }] }, error: null });
    expect(conn.quiet).toEqual(["heap.trace.report"]);
    const bad = new FakeConn({ "heap.trace.report": () => { throw new Error("RuntimeError: 記録が無い"); } });
    expect(await readReport(bad)).toEqual({ report: null, error: "記録が無い" });
  });
});

describe("issueText（走査・記録の問題）", async () => {
  const { issueText } = await import("../src/lib/heap");
  it("説明は detail、場所は address か pointer（無ければ出さない）", () => {
    expect(issueText({ kind: "bad_size", address: 0x2000, detail: "大きさが変" })).toBe("bad_size 0x00002000 大きさが変");
    expect(issueText({ kind: "bad_link", address: null, detail: "輪" })).toBe("bad_link 輪");
    expect(issueText({ kind: "double_free", pointer: 16, caller: "f", detail: "2 回" })).toBe("double_free 0x00000010 2 回 (f)");
  });
});
