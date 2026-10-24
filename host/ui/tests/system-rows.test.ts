// システム解析の時間軸の並び: MCU と FPGA が隣り合う（MCU のメモリの標本 → FPGA → ピン → DAP → 端末）。変化の無いピンは隠す
import { describe, expect, it } from "vitest";
import { EMPTY_CONFIG, systemHides, systemOrder, unhide } from "../src/lib/timeline/rows";
import { overviewSpecs } from "../src/lib/timeline/state";

const g = (id: string) => ({ id, rows: [] });

describe("システム解析の段の並び", () => {
  it("MCU・FPGA・ピンを先に、残りは今の並びのまま。それらは開き、ほかのまとまり（時計・操作等）は畳む", () => {
    const groups = ["host", "dap", "uart", "fpga", "clock", "annotation", "logic", "sample"].map(g);
    const c = systemOrder({ ...EMPTY_CONFIG, folded: ["fpga", "annotation"] }, groups);
    expect(c.groups).toEqual(["sample", "fpga", "target", "logic", "dap", "uart", "swo", "rtt", "user", "annotation",
                              "host", "clock"]);
    expect(c.folded).toEqual(["host", "clock", "la"]);
  });
  it("記録の中で変化の無いピンは隠す（隠していた他の段はそのまま）", () => {
    const c = systemOrder({ ...EMPTY_CONFIG, hidden: ["dap:x"] }, [], [1, 2]);
    expect(c.hidden).toEqual(["dap:x", "wave:1", "wave:2"]);
    expect(systemOrder({ ...EMPTY_CONFIG, hidden: ["wave:1"] }, [], [1]).hidden).toEqual(["wave:1"]);
  });
  it("この並びで新しく隠すピンの段（前から隠していた段は入れない）と、それを見せ直す", () => {
    expect(systemHides({ ...EMPTY_CONFIG, hidden: ["wave:1"] }, [1, 2])).toEqual(["wave:2"]);
    const c = { ...EMPTY_CONFIG, hidden: ["dap:x", "wave:1", "wave:2"] };
    expect(unhide(c, ["wave:1", "wave:2"]).hidden).toEqual(["dap:x"]);
  });
  it("縮図の段: stream が無ければロジアナのチャンネルを全部（8 本等で黙って切らない）", () => {
    const logic = { id: "logic", rows: Array.from({ length: 16 }, (_, ch) => ({ id: `wave:${ch}`, group: "logic", type: "logic" as const,
                                                                            title: null, color: 0 })) };
    expect(overviewSpecs([logic as any]).length).toBe(16);
  });
  it("まだ無いまとまりも先に並べておく（記録に切り替えた直後でまとまりがまだ無くても効く）", () => {
    expect(systemOrder(EMPTY_CONFIG, []).groups).toEqual(["sample", "fpga", "target", "logic", "dap", "uart", "swo", "rtt", "user", "annotation"]);
    expect(systemOrder(EMPTY_CONFIG, []).folded).toEqual(["host", "clock", "la"]);   // まとまりがまだ無くても畳む
  });
});
