// Logic Analyzer: 解読器の欄 → settings、注釈 → 印、トリガの欄 ↔ xtrig の引数、段の図、測定の範囲
import { describe, expect, it } from "vitest";
import {
  decodeMarks, hiddenKinds, measureCall, measureRange, measuredChannels, newForm, patternForm, patternOf, settingValue, settingsOf,
  stageForms, stageLabel, stageState, stagesOf, type DecoderInfo,
} from "../src/lib/logic";

const DECODERS: DecoderInfo[] = [
  { name: "uart", lower: [], summary: "UART", params: [{ name: "channel", required: true }, { name: "baud", required: false, default: null }],
    default_kinds: null, origin: "builtin" },
  { name: "spi", lower: [], summary: "SPI", params: [{ name: "sck", required: true }, { name: "mosi", required: false, default: null },
                                                     { name: "cs", required: true }, { name: "mode", required: false, default: 0 }],
    default_kinds: null, origin: "builtin" },
  { name: "modbus", lower: ["uart"], summary: "Modbus RTU", params: [{ name: "address", required: false, default: null }],
    default_kinds: null, origin: "builtin" },
];

describe("解読器の設定", () => {
  it("必須のチャンネルに順に ch を割り当て、ほかは空（既定）。積み重ねる解読器は下の欄も", () => {
    expect(newForm("spi", DECODERS)).toEqual({ protocol: "spi", values: { sck: "0", mosi: "", cs: "1", mode: "" }, lower: null });
    const f = newForm("modbus", DECODERS);
    expect(f.lower).toEqual({ protocol: "uart", values: { channel: "0", baud: "" }, lower: null });
  });

  it("欄 → settings（空は送らない。数・真偽・並び。下の解読は下の名前のキー）", () => {
    const f = newForm("modbus", DECODERS);
    f.values.address = "0x11";
    f.lower!.values.baud = "9600";
    expect(settingsOf(f)).toEqual({ address: 17, uart: { channel: 0, baud: 9600 } });
    expect([settingValue("true"), settingValue("1,2,3"), settingValue("[4, 5]"), settingValue("0.75"), settingValue("even"),
            settingValue("ir,dr")]).toEqual([true, [1, 2, 3], [4, 5], 0.75, "even", "ir,dr"]);
  });

  it("注釈は区間の印、誤りは tone 1", () => {
    const a = { start: 10, end: 20, start_rel_s: 0, end_rel_s: 0, kind: "data", text: "'U'", value: 85, error: null, fields: null };
    expect(decodeMarks([a, { ...a, start: 30, end: 30, error: "framing", text: "x" }]))
      .toEqual([{ t: 10, tEnd: 20, label: "'U'" }, { t: 30, label: "x", tone: 1 }]);
  });
});

describe("トリガ", () => {
  it("ピンの条件: x = 見ない、0 / 1 = 値、立ち上がり・立ち下がり。往復できる", () => {
    const f = patternForm(null, 4);
    f.level[1] = "1";
    f.level[2] = "0";
    f.rise[3] = true;
    f.fall[0] = true;
    const p = patternOf(f);
    expect(p).toEqual({ mask: 0b0110, value: 0b0010, rise: 0b1000, fall: 0b0001 });
    expect(patternForm(p, 4)).toEqual(f);
  });

  it("段: 事象が無ければ誤り、段 0 の制限時間は送らない、回数は 1 以上", () => {
    const forms = [{ when: ["pattern_a" as const], count: 1, withinUs: "5" },
                   { when: ["pattern_b" as const, "host" as const], count: 0, withinUs: "10" }];
    expect(stagesOf(forms)).toEqual([{ when: ["pattern_a"], count: 1 }, { when: ["pattern_b", "host"], count: 1, within_us: 10 }]);
    expect(() => stagesOf([{ when: [], count: 1, withinUs: "" }])).toThrow();
    expect(stageForms([{ when: ["dap_match", "bogus"], count: 2, within_us: 3 }]))
      .toEqual([{ when: ["dap_match"], count: 2, withinUs: "3" }]);
  });

  it("段の図の文字と状態", () => {
    expect(stageLabel({ when: ["pattern_a", "host"], count: 3, withinUs: "10" }, 1)).toEqual({ events: "A | Host", detail: "×3 ≤10 µs" });
    expect(stageLabel({ when: ["dap_match"], count: 1, withinUs: "10" }, 0)).toEqual({ events: "Match", detail: "" });
    const st = { stage: 1, fired: false };
    expect([0, 1, 2].map((i) => stageState(i, st, true))).toEqual(["done", "current", "waiting"]);
    expect(stageState(2, { stage: 1, fired: true }, true)).toBe("done");
    expect(stageState(0, st, false)).toBe("idle");
  });
});

describe("測定", () => {
  it("基準と選択のカーソルの間（両方あるとき）、無ければ全体", () => {
    expect(measureRange(50, 10)).toEqual([10, 51]);
    expect(measureRange(null, 10)).toBeNull();
    expect(measureCall("capture", [10, 51])).toEqual({ method: "la.measure", params: { start: 10, end: 51 } });
    expect(measureCall("recording", null)).toEqual({ method: "recording.waveform_measure", params: { t0: null, t1: null } });
  });

  it("変化のあるチャンネルだけ、番号の順", () => {
    const ch = (n: number) => ({ rises: n, falls: n, high_ratio: 0.5 });
    expect(measuredChannels({ "10": ch(1), "2": ch(3), "4": ch(0) }).map(([n]) => n)).toEqual([2, 10]);
  });
});

describe("解読の件数と表", () => {
  it("既定の種類の外で表に出ない件数と種類（件数と表の食い違いを説明する）", () => {
    expect(hiddenKinds({ kinds: ["transfer"], counts: { line_reset: 1 } })).toEqual({ n: 1, kinds: ["line_reset"] });
    expect(hiddenKinds({ kinds: ["transfer"], counts: { transfer: 3, line_reset: 2, idle: 1 } }))
      .toEqual({ n: 3, kinds: ["idle", "line_reset"] });
    expect(hiddenKinds({ kinds: null, counts: { frame: 2 } })).toEqual({ n: 0, kinds: [] });
    expect(hiddenKinds({ counts: { frame: 2 } })).toEqual({ n: 0, kinds: [] });              // 古いデーモン
  });
});

describe("laExportParams（捕捉の保存。la.export）", async () => {
  const { laExportParams } = await import("../src/lib/logic");
  it("既定のパスは日時と形式の拡張子。手で変えたパスはそのまま", () => {
    const at = new Date(2026, 9, 22, 21, 5, 9);
    expect(laExportParams("sr", null, at)).toEqual({ format: "sr", path: "capture-20261022-210509.sr" });   // 2 回目に上書きしない
    expect(laExportParams("vcd", null, at)).toEqual({ format: "vcd", path: "capture-20261022-210509.vcd" });
    expect(laExportParams("vcd", "D:/x/a.vcd", at)).toEqual({ format: "vcd", path: "D:/x/a.vcd" });
  });
});
