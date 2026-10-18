// Recordings: 長期の記録の欄・書き出しの引数と既定のパス。Logic Analyzer の小さな表示
import { describe, expect, it } from "vitest";
import { formatHz, lastApplied } from "../src/lib/logic";
import { defaultExportPath, exportParams, longTermParams } from "../src/lib/recordings";

describe("記録", () => {
  it("長期の記録: 空の欄は送らない（普通の記録）、MB はバイトに、読めない値は投げる", () => {
    expect(longTermParams({ segmentS: "", maxMb: "", decimateAfterS: "" })).toEqual({});
    expect(longTermParams({ segmentS: "600", maxMb: "1.5", decimateAfterS: "0" }))
      .toEqual({ segment_s: 600, max_bytes: 1572864, decimate_after_s: 0 });
    expect(() => longTermParams({ segmentS: "0", maxMb: "", decimateAfterS: "" })).toThrow();
    expect(() => longTermParams({ segmentS: "", maxMb: "x", decimateAfterS: "" })).toThrow();
  });

  it("書き出し: VCD は範囲を送らない。既定のパスは記録の名前と拡張子", () => {
    expect(exportParams("csv", "a.csv", [1, 2])).toEqual({ format: "csv", path: "a.csv", t0: 1, t1: 2 });
    expect(exportParams("vcd", "a.vcd", [1, 2])).toEqual({ format: "vcd", path: "a.vcd" });
    expect(exportParams("json", "a.json", null)).toEqual({ format: "json", path: "a.json" });
    expect(defaultExportPath("demo", "perfetto")).toBe("demo.perfetto.json");
    expect(defaultExportPath(null, "vcd")).toBe("recording.vcd");
  });
});

describe("Logic Analyzer の表示", () => {
  it("最後に成功した設定の操作（AI のものも）", () => {
    const log = [{ method: "xtrig.route", client: "claude" }, { method: "xtrig.route", client: "ui", error: "x" }];
    expect(lastApplied(log, "xtrig.route")?.client).toBe("claude");
    expect(lastApplied(log, "xtrig.set_match")).toBeNull();
  });

  it("周波数の単位", () => {
    expect([formatHz(1041666.7), formatHz(130208), formatHz(50), formatHz(null)]).toEqual(["1.042 MHz", "130.2 kHz", "50 Hz", "—"]);
  });
});
