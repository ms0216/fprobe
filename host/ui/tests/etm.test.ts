// 故障の直前の命令（lib/etm.ts）: トレースの置き場所から trace.etm.decode → trace.etm.history
import { describe, expect, it } from "vitest";
import { decodeParams, markersAt, readHistory, parseInteger } from "../src/lib/etm";
import { FakeConn } from "./fake-conn";

describe("decodeParams", () => {
  it("ターゲットのメモリのリング（番地・長さ・最も古い位置）か、デーモンの PC のファイル", () => {
    expect(decodeParams({ kind: "memory", address: "0x20040000", length: "4096", oldest: "" }))
      .toEqual({ params: { address: 0x20040000, length: 4096, oldest: 0 } });
    expect(decodeParams({ kind: "file", path: "trace.bin" })).toEqual({ params: { path: "trace.bin" } });
    expect(decodeParams({ kind: "memory", address: "x", length: "4", oldest: "" })).toEqual({ error: "address" });
    expect(decodeParams({ kind: "file", path: "" })).toEqual({ error: "path" });
  });

  it("整数は 10 進・0x の 16 進", () => {
    expect(parseInteger("0x10")).toBe(16);
    expect(parseInteger("12")).toBe(12);
    expect(parseInteger("1.5")).toBeNull();
  });
});

describe("readHistory", () => {
  it("decode の後に history を読む", async () => {
    const conn = new FakeConn({ "trace.etm.decode": { instructions: 10 },
                                "trace.etm.history": { total: 10, instructions: [{ index: 9, address: 4096, mnemonic: "bx", operands: "lr" }],
                                                       markers: [{ kind: "exception", index: 9 }], next_address: 4100, errors: [] } });
    const r = await readHistory(conn, { address: 1, length: 2, oldest: 0 }, 32);
    expect(conn.calls).toEqual([["trace.etm.decode", { address: 1, length: 2, oldest: 0 }], ["trace.etm.history", { count: 32 }]]);
    expect(r.history?.instructions[0].mnemonic).toBe("bx");
    expect(r.error).toBeNull();
  });

  it("decode の誤りはそのまま返し、history は読まない", async () => {
    const conn = new FakeConn({ "trace.etm.decode": () => { throw new Error("RuntimeError: ELF が無い"); } });
    const r = await readHistory(conn, { path: "x" }, 32);
    expect(r).toEqual({ history: null, error: "ELF が無い" });
    expect(conn.calls.map((c) => c[0])).toEqual(["trace.etm.decode"]);
  });
});

describe("markersAt", () => {
  it("その命令の前に起きた出来事", () => {
    const m = [{ kind: "exception", index: 3 }, { kind: "sync", index: 5 }];
    expect(markersAt(m, 3)).toEqual([{ kind: "exception", index: 3 }]);
    expect(markersAt(m, 4)).toEqual([]);
  });
});

describe("レビューの指摘（最後の出来事・解読の誤り）", async () => {
  const { trailingMarkers } = await import("../src/lib/etm");
  it("最後の命令の後の出来事（止まる原因の例外）も出す", () => {
    const h = { instructions: [{ index: 8 }, { index: 9 }], markers: [{ kind: "x", index: 9 }, { kind: "exception", index: 10 }] } as any;
    expect(trailingMarkers(h)).toEqual([{ kind: "exception", index: 10 }]);
  });

  it("解読の誤りと注記も返す", async () => {
    const conn = new FakeConn({ "trace.etm.decode": { errors: ["トレース ID 16 の流れが無い"], note: "n" },
                                "trace.etm.history": { total: 0, instructions: [], markers: [], next_address: null, errors: [] } });
    const r = await readHistory(conn, { path: "x" }, 8);
    expect(r.decodeErrors).toEqual(["トレース ID 16 の流れが無い"]);
  });
});

describe("parseCount（命令の数の欄）", async () => {
  const { parseCount } = await import("../src/lib/etm");
  it("1 以上の整数だけ。ほかは null（黙って既定に置き換えない）", () => {
    expect(parseCount("32")).toBe(32);
    expect(parseCount("0")).toBeNull();
    expect(parseCount("2.5")).toBeNull();
    expect(parseCount("x")).toBeNull();
  });
});
