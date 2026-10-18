// 実行の設定（ステップ中の割り込み・リセットの種類）: API の引数と、ブラウザに覚える値
import { beforeEach, describe, expect, it } from "vitest";
import { ExecutionSettings, RESET_KINDS, resetParams, stepParams } from "../src/lib/execution";

describe("API の引数", () => {
  it("ステップ: 割り込みを止めるのは既定（引数に出さない）。止めないなら mask_interrupts: false", () => {
    const on = { maskInterrupts: true, resetKind: "default" as const };
    expect(stepParams("over", on)).toEqual({ mode: "over" });
    expect(stepParams("until", on, "main.c:12")).toEqual({ mode: "until", location: "main.c:12" });
    expect(stepParams("into", { ...on, maskInterrupts: false })).toEqual({ mode: "into", mask_interrupts: false });
  });

  it("リセット: 止めて、既定以外の種類は kind で", () => {
    expect(resetParams({ maskInterrupts: true, resetKind: "default" })).toEqual({ halt: true });
    expect(resetParams({ maskInterrupts: true, resetKind: "hardware" })).toEqual({ halt: true, kind: "hardware" });
    expect(RESET_KINDS).toEqual(["default", "system", "core", "hardware", "emulated"]);
  });
});

describe("設定の値", () => {
  beforeEach(() => {
    try {
      localStorage.clear();
    } catch {
      /* localStorage の無い環境 */
    }
  });

  it("変えると知らせ、知らない値は既定に戻す", () => {
    const s = new ExecutionSettings();
    const seen: unknown[] = [];
    const off = s.subscribe((v) => seen.push(v));
    expect(s.get()).toEqual({ maskInterrupts: true, resetKind: "default" });
    s.set({ resetKind: "core" });
    s.set({ maskInterrupts: false });
    expect(s.get()).toEqual({ maskInterrupts: false, resetKind: "core" });
    s.set({ resetKind: "bogus" as any });
    expect(s.get().resetKind).toBe("default");
    off();
    expect(seen.length).toBe(4);
  });
});
