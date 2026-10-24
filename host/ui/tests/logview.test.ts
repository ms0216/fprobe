// ログ（lib/logview.ts）: log.start の引数と、log.read の読み足し
import { describe, expect, it } from "vitest";
import { LogView, startParams } from "../src/lib/logview";
import { FakeConn } from "./fake-conn";

describe("startParams", () => {
  it("形式・源と、書いた欄だけ（チャンネルは数・パスは文字）", () => {
    expect(startParams({ format: "defmt", source: "rtt", channel: "", elf: "", til: "" })).toEqual({ format: "defmt", source: "rtt" });
    expect(startParams({ format: "trice", source: "uart", channel: "1", elf: "", til: "til.json" }))
      .toEqual({ format: "trice", source: "uart", channel: 1, til: "til.json" });
  });
});

describe("LogView", () => {
  it("next から読み足し、絞り込みを渡す。読み取りは quiet", async () => {
    const pages = [{ records: [{ seq: 1, text: "a" }, { seq: 2, text: "b" }], next: 2, dropped: 0, running: true },
                   { records: [{ seq: 3, text: "c" }], next: 3, dropped: 1, running: false }];
    let i = 0;
    const conn = new FakeConn({ "log.read": () => pages[i++] });
    const v = new LogView(conn);
    await v.more({ level: "warn", contains: "" });
    await v.more({ level: "warn", contains: "" });
    expect(v.records.map((r) => r.text)).toEqual(["a", "b", "c"]);
    expect(conn.calls).toEqual([["log.read", { since: 0, level: "warn" }], ["log.read", { since: 2, level: "warn" }]]);
    expect(v.dropped).toBe(1);
    expect(v.running).toBe(false);
    expect(conn.quiet).toEqual(["log.read", "log.read"]);
  });

  it("絞り込みを変えたら最初から読み直す", async () => {
    const conn = new FakeConn({ "log.read": { records: [{ seq: 5, text: "x" }], next: 5, dropped: 0, running: true } });
    const v = new LogView(conn);
    await v.more({ level: "", contains: "" });
    v.reset();
    await v.more({ level: "", contains: "x" });
    expect(conn.calls[1]).toEqual(["log.read", { since: 0, contains: "x" }]);
    expect(v.records.length).toBe(1);
  });
});

describe("レビューの指摘（重なる読み取り・200 件で止まる・場所）", async () => {
  const { locationText } = await import("../src/lib/logview");

  it("読み取りが重なっても同じ記録を 2 度足さない（進行中なら待つ・古い世代の応答は捨てる）", async () => {
    let release: () => void = () => {};
    const gate = new Promise<void>((r) => (release = r));
    const conn = new FakeConn({ "log.read": async () => { await gate; return { records: [{ seq: 1, text: "a" }], next: 1, dropped: 0, running: true }; } });
    const v = new LogView(conn);
    const a = v.more({ level: "", contains: "" });
    const b = v.more({ level: "", contains: "" });
    release();
    await Promise.all([a, b]);
    expect(v.records.map((r) => r.seq)).toEqual([1]);
    v.reset();
    expect(v.records).toEqual([]);
  });

  it("API が続きがある（more）と言う間は続けて読む（返った件数・デーモンの既定の件数で推さない）", async () => {
    const page = (from: number, n: number, more: boolean) => ({
      records: Array.from({ length: n }, (_, k) => ({ seq: from + k, text: "" })), next: from + n - 1, dropped: 0, running: false, more });
    const pages = [page(1, 3, true), page(4, 200, true), page(204, 200, false)];
    let i = 0;
    const conn = new FakeConn({ "log.read": () => pages[i++] });
    const v = new LogView(conn);
    await v.more({ level: "", contains: "" });
    expect(v.records.length).toBe(403);
    expect(conn.calls.map((c) => c[1].since)).toEqual([0, 3, 203]);
  });

  it("画面に持つ上限を超えたら古い行を捨て、捨てた数を数える（黙って捨てない）", async () => {
    const { LOG_KEEP } = await import("../src/lib/logview");
    const n = LOG_KEEP + 3;
    const conn = new FakeConn({ "log.read": { records: Array.from({ length: n }, (_, k) => ({ seq: k + 1, text: "" })),
                                              next: n, dropped: 0, running: false, more: false } });
    const v = new LogView(conn);
    await v.more({ level: "", contains: "" });
    expect(v.records.length).toBe(LOG_KEEP);
    expect(v.discarded).toBe(3);
    v.reset();
    expect(v.discarded).toBe(0);
  });

  it("場所は file:line（関数があれば添える）", () => {
    expect(locationText({ file: "main.rs", line: 12, function: "app::run" })).toBe("main.rs:12 app::run");
    expect(locationText({ file: "a.c" })).toBe("a.c");
    expect(locationText(undefined)).toBe("");
  });
});

describe("startParams のチャンネル", () => {
  it("数なら数、名前（defmt 等）なら文字のまま送る（黙って既定のチャンネルにしない）", () => {
    expect(startParams({ format: "defmt", source: "rtt", channel: "defmt", elf: "", til: "" }).channel).toBe("defmt");
    expect(startParams({ format: "defmt", source: "rtt", channel: " 2 ", elf: "", til: "" }).channel).toBe(2);
  });
});
