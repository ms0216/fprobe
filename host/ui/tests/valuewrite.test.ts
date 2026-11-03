// 見えている値をその窓で書く（lib/valuewrite.ts）: どの API で書くかと、欄から引数を組み立てる計算
import { describe, expect, it } from "vitest";
import { Confirmer } from "../src/lib/confirm";
import {
  bytesWriteCall, elementExpr, encodeLE, exprWriteCall, exprWriteMethod, instructionWriteKind, isWriteCall, locationOf,
  memoryWriteCall, parseInteger, performWrite, writeBuilt, writeOneToClear,
} from "../src/lib/valuewrite";

describe("式の書き込み: 道はデーモンが決める（画面は状態で API を選ばない）", () => {
  it("つながっていれば debug.write_expression（止まっていても走っていても。道はデーモンが選ぶ）、未接続は書けない", () => {
    for (const s of ["RUNNING", "SLEEPING", "HALTED", "RESET", "LOCKUP"]) expect(exprWriteMethod(s)).toBe("debug.write_expression");
    for (const s of ["DISCONNECTED", undefined]) expect(exprWriteMethod(s)).toBeNull();
  });

  it("引数: 真偽は数に・フレームを渡したときだけ付ける（状態で形を変えない）", () => {
    expect(exprWriteCall("RUNNING", "x", true)).toEqual({ method: "debug.write_expression", params: { expression: "x", value: 1 } });
    expect(exprWriteCall("HALTED", "x", true)).toEqual({ method: "debug.write_expression", params: { expression: "x", value: 1 } });
    expect(exprWriteCall("HALTED", "x", 5, 2)).toEqual({ method: "debug.write_expression",
                                                         params: { expression: "x", value: 5, frame: 2 } });
    expect(exprWriteCall("DISCONNECTED", "x", 5)).toBeNull();
  });

  it("配列の要素の式（式を括弧で包む・メンバー）", () => {
    expect(elementExpr("buf", 3)).toBe("buf[3]");
    expect(elementExpr("s.items", 0, "value")).toBe("s.items[0].value");
    expect(elementExpr("*p", 1)).toBe("(*p)[1]");
  });
});

describe("メモリの値を書く（番地と大きさは API の結果の事実）", () => {
  it("整数の読み: 16 進・2 進・10 進・負の数。読めなければ null", () => {
    expect(parseInteger("0x1F")).toBe(31n);
    expect(parseInteger(" -0x10 ")).toBe(-16n);
    expect(parseInteger("0b101")).toBe(5n);
    expect(parseInteger("42")).toBe(42n);
    for (const bad of ["", "abc", "1.5", "0x", "--1"]) expect(parseInteger(bad)).toBeNull();
  });

  it("小さい方から並べたバイト（2 の補数）。大きさに収まらなければ null", () => {
    expect(encodeLE(0x12345678n, 4)).toBe("78563412");
    expect(encodeLE(-1n, 2)).toBe("ffff");
    expect(encodeLE(255n, 1)).toBe("ff");
    expect(encodeLE(-128n, 1)).toBe("80");
    expect(encodeLE(256n, 1)).toBeNull();
    expect(encodeLE(-129n, 1)).toBeNull();
    expect(encodeLE(0x1122334455667788n, 8)).toBe("8877665544332211");
  });

  it("target.memory.write の引数。読めない値・収まらない値は誤りの種類", () => {
    expect(memoryWriteCall({ address: 0xE000ED28, size: 4 }, "0x80000")).toEqual(
      { method: "target.memory.write", params: { address: 0xE000ED28, data_hex: "00000800" } });
    expect(memoryWriteCall({ address: 0x20000000, size: 1 }, "300")).toEqual({ error: "range" });
    expect(memoryWriteCall({ address: 0x20000000, size: 4 }, "x")).toEqual({ error: "value" });
    expect(isWriteCall(memoryWriteCall({ address: 0, size: 4 }, "1"))).toBe(true);
    expect(isWriteCall({ error: "value" })).toBe(false);
  });

  it("結果の locations の欄（番地・大きさが数でなければ書けない = null）", () => {
    const task = { locations: { priority: { address: 0x20000010, size: 1, member: "uxPriority" } } };
    expect(locationOf(task, "priority")).toEqual({ address: 0x20000010, size: 1 });
    expect(locationOf(task, "state")).toBeNull();
    expect(locationOf({}, "priority")).toBeNull();
    expect(locationOf(null, "priority")).toBeNull();
    expect(locationOf({ locations: { x: { address: "0x1", size: 4 } } }, "x")).toBeNull();
    expect(locationOf({ locations: { x: { address: 1, size: 0 } } }, "x")).toBeNull();
  });

  it("1 を書いて消すレジスタ（結果の write_one_to_clear にあるか）", () => {
    const report = { write_one_to_clear: ["CFSR", "HFSR", "DFSR"] };
    expect(writeOneToClear(report, "CFSR")).toBe(true);
    expect(writeOneToClear(report, "BFAR")).toBe(false);
    expect(writeOneToClear({}, "CFSR")).toBe(false);
  });
});

describe("命令のバイトを書く（逆アセンブル）", () => {
  it("デーモンの in_flash（命令が Flash の領域に掛かるか）をそのまま: true は flash、false は memory、null・無しは unknown", () => {
    // 重なりの計算は画面でしない（番地が Flash の中でも in_flash を読むだけ）
    expect(instructionWriteKind({ address: 0x20000100, in_flash: true })).toBe("flash");
    expect(instructionWriteKind({ address: 0x08000100, in_flash: false })).toBe("memory");
    expect(instructionWriteKind({ address: 0x08000100, in_flash: null })).toBe("unknown");
    expect(instructionWriteKind({ address: 0x08000100 })).toBe("unknown");
  });

  it("書くバイトはメモリの順の 16 進（空白は無視）で、命令と同じ長さ", () => {
    expect(bytesWriteCall(0x20000100, "00bf", "00 BF")).toEqual(
      { method: "target.memory.write", params: { address: 0x20000100, data_hex: "00bf" } });
    expect(bytesWriteCall(0x20000100, "00bf", "00bf00bf")).toEqual({ error: "length" });
    expect(bytesWriteCall(0x20000100, "00bf", "0g00")).toEqual({ error: "value" });
    expect(bytesWriteCall(0x20000100, "00bf", "0bf")).toEqual({ error: "value" });
  });
});

describe("書く操作は AI の作業中の確認を通す", () => {
  it("AI の作業中に断られたら呼ばない。確かめたら呼ぶ", async () => {
    const calls: [string, unknown][] = [];
    const app = (aiConnectedUnpaused: boolean) => ({ aiConnectedUnpaused, act: async (m: string, p: unknown) => (calls.push([m, p]), { ok: 1 }) });
    const call = { method: "target.memory.write", params: { address: 1, data_hex: "00" } };
    expect(await performWrite(app(false), call)).toEqual({ ok: 1 });
    const c = new Confirmer();
    const denied = performWrite(app(true), call, c);
    c.answer(false);
    expect(await denied).toBeUndefined();
    expect(calls.length).toBe(1);
    const accepted = performWrite(app(true), call, c);
    c.answer(true);
    expect(await accepted).toEqual({ ok: 1 });
    expect(calls.length).toBe(2);
  });

  it("組み立てられなかった欄は呼ばずに、その理由の文（write.error.*）を返す", async () => {
    const calls: string[] = [];
    const app = { aiConnectedUnpaused: false, act: async (m: string) => (calls.push(m), {}) };
    const t = (k: string) => `<${k}>`;
    expect(await writeBuilt(app, memoryWriteCall({ address: 0, size: 1 }, "999"), t as any)).toBe("<write.error.range>");
    expect(calls).toEqual([]);
    expect(await writeBuilt(app, memoryWriteCall({ address: 0, size: 1 }, "9"), t as any)).toBeNull();
    expect(calls).toEqual(["target.memory.write"]);
  });
});
