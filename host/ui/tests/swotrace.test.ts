// SWO Trace のパネルの計算
import { describe, expect, it } from "vitest";
import { E_NO_DATA, RpcError } from "../src/lib/api";
import { configureParams, exceptionRows, tailOffset, traceError, valueText } from "../src/lib/swotrace";
import { deviceError } from "./fake-conn";
import { venum, vint } from "./value-kinds";

describe("SWO Trace", () => {
  it("設定を swo.configure の引数に（空の式は除く。比較器の数で黙って捨てない: 数はターゲットの DWT で決まり、足りなければ API が誤りを返す）", () => {
    const vars = ["a", " ", "b", "c", "d", "e"].map((expression) => ({ expression, access: "write" as const }));
    const p = configureParams({ exceptions: true, timestamps: false, variables: vars, swoBaud: 2e6, coreClockHz: 84e6 });
    expect(p).toEqual({ exception_trace: true, timestamps: false, swo_baud: 2e6, core_clock_hz: 84e6,
                        data_trace: ["a", "b", "c", "d", "e"].map((expression) => ({ expression, access: "write" })) });
    expect((configureParams({ exceptions: false, timestamps: false, variables: [], swoBaud: 1e6, coreClockHz: null }) as any)
      .data_trace).toBeNull();
  });

  it("SWO の速さは使う人が選ぶ（2 MHz を既定にしない。選んでいなければ誤り）", () => {
    expect(configureParams({ exceptions: true, timestamps: false, variables: [], swoBaud: null, coreClockHz: null }))
      .toEqual({ error: "swoBaud" });
  });

  it("例外の回数は API の並びのまま（画面で回数の多い順に並べ替えない）", () => {
    expect(exceptionRows({ A: 1, B: 5 })).toEqual([{ name: "A", count: 1 }, { name: "B", count: 5 }]);
  });

  it("終わりから読む位置・値・例外の並び", () => {
    expect(tailOffset(1200, 500)).toBe(700);
    expect(tailOffset(10, 500)).toBe(0);
    expect(valueText(vint(255))).toBe("255 (0xFF)");
    expect(valueText(venum("RUN", 1))).toBe("RUN (1)");                        // 変数の型で解釈した値
  });
});

describe("swo.trace の誤り", () => {
  it("まだ設定していない（E_NO_DATA）は誤りにしない。ほかは文の語によらず誤りとして出す", () => {
    expect(traceError(new RpcError(E_NO_DATA, "NoData: swo_configure で設定していない",
                                   { exception: "NoData", detail: "swo_configure で設定していない" }))).toBeNull();
    // 機器の誤りは、文に swo_configure があっても誤り
    expect(traceError(deviceError("swo_configure の比較器が足りない"))).toBe("swo_configure の比較器が足りない");
  });
});
