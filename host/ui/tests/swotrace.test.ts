// SWO Trace のパネルの計算
import { describe, expect, it } from "vitest";
import { configureParams, exceptionRows, tailOffset, valueText } from "../src/lib/swotrace";

describe("SWO Trace", () => {
  it("設定を swo.configure の引数に（空の式は除き、比較器は 4 つまで）", () => {
    const vars = ["a", " ", "b", "c", "d", "e"].map((expression) => ({ expression, access: "write" as const }));
    const p = configureParams({ exceptions: true, timestamps: false, variables: vars, swoBaud: 2e6, coreClockHz: 84e6 });
    expect(p).toEqual({ exception_trace: true, timestamps: false, swo_baud: 2e6, core_clock_hz: 84e6,
                        data_trace: ["a", "b", "c", "d"].map((expression) => ({ expression, access: "write" })) });
    expect(configureParams({ exceptions: false, timestamps: false, variables: [], swoBaud: 1e6, coreClockHz: null })
      .data_trace).toBeNull();
  });

  it("終わりから読む位置・値・例外の並び", () => {
    expect(tailOffset(1200, 500)).toBe(700);
    expect(tailOffset(10, 500)).toBe(0);
    expect(valueText(255)).toBe("255 (0xFF)");
    expect(exceptionRows({ A: 1, B: 5 })).toEqual([{ name: "B", count: 5 }, { name: "A", count: 1 }]);
  });
});
