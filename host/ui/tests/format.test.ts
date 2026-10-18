import { describe, expect, it } from "vitest";
import { clock, frequency, outcome, plainNumber, seconds } from "../src/lib/format";

describe("format", () => {
  it("値は普通の表記（整数はそのまま、小数は有効数字 6 桁。指数にしない）", () => {
    expect(plainNumber(3735928559)).toBe("3735928559");
    expect(plainNumber(-5)).toBe("-5");
    expect(plainNumber(3.14159265)).toBe("3.14159");
    expect(plainNumber(123456789.25)).toBe("123456789");
    expect(plainNumber(0.000123456789)).toBe("0.000123457");
    expect(plainNumber(1.5e-9)).toBe("0.0000000015");
  });

  it("時刻・所要時間・結果を短くする", () => {
    expect(clock("2026-10-17T12:01:03.456")).toBe("12:01:03");
    expect(seconds(0.0123)).toBe("12.3 ms");
    expect(seconds(1.234)).toBe("1.23 s");
    expect(seconds(undefined)).toBe("");
    expect(outcome({ error: "broken" } as any)).toBe("Error: broken");          // 既定は英語の表
    expect(outcome({ result: { state: "HALTED" } } as any)).toBe('{"state":"HALTED"}');
    expect(outcome({ result: null } as any)).toBe("Done");
    expect(outcome({ result: "x".repeat(200) } as any).length).toBe(80);
  });

  it("周波数は有効数字 6 桁まで（浮動小数の誤差の桁を出さない）", () => {
    expect(frequency(4e6)).toBe("4 MHz");
    expect(frequency(100285714)).toBe("100.286 MHz");
    expect(frequency(100285714 / 500)).toBe("200.571 kHz");
    expect(frequency(100285714 / 1000)).toBe("100.286 kHz");
    expect(frequency(500e3)).toBe("500 kHz");
    expect(frequency(900)).toBe("900 Hz");
  });
});
