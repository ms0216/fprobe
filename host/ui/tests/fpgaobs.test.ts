// FPGA 観測のパネルの計算: 列挙の書き方・ビットの範囲・定義の検査（デーモンの fprobe.fpga_signals と同じ規則）・
// TCK と半周期の換算・状態の文言
import { describe, expect, it } from "vitest";
import {
  editableGroups, formatBits, formatEnum, parseBits, parseEnum, signalsChanged, statusKey, tckToHalfPeriod,
  validateSignals, type SignalDef,
} from "../src/lib/fpgaobs";

const sig = (name: string, bits: string, format: SignalDef["format"] = "hex", enumText = ""): SignalDef =>
  ({ name, ...parseBits(bits)!, format, enum: enumText ? parseEnum(enumText)! : {} });

describe("列挙の書き方", () => {
  it("「0=IDLE, 1=RUN, 0x5=ERR」を値 → 名前に（10 進・16 進）", () => {
    expect(parseEnum("0=IDLE, 1=RUN, 0x5=ERR")).toEqual({ "0": "IDLE", "1": "RUN", "5": "ERR" });
    expect(parseEnum("")).toEqual({});
    expect(formatEnum({ "1": "RUN", "0": "IDLE" })).toBe("0=IDLE, 1=RUN");
  });
  it("形の崩れは null（画面がその場で知らせる）", () => {
    expect(parseEnum("0=IDLE, RUN")).toBeNull();
    expect(parseEnum("x=IDLE")).toBeNull();
    expect(parseEnum("0=")).toBeNull();
  });
});

describe("ビットの範囲", () => {
  it("「7:4」「3」と lsb・幅の行き来", () => {
    expect(parseBits("7:4")).toEqual({ lsb: 4, width: 4 });
    expect(parseBits("3")).toEqual({ lsb: 3, width: 1 });
    expect(parseBits("4:7")).toBeNull();
    expect(parseBits("a")).toBeNull();
    expect(formatBits(4, 4)).toBe("7:4");
    expect(formatBits(3, 1)).toBe("3");
  });
});

describe("定義の検査（デーモンと同じ規則。保存の前に画面で知らせる）", () => {
  it("正しい定義は誤り無し", () => {
    expect(validateSignals({ "0": [sig("mark", "0"), sig("state", "5:4", "enum", "0=IDLE, 1=RUN")] })).toEqual([]);
  });
  it("重なり・16 ビットの外・名前の重複・空の名前・列挙の値が幅の外", () => {
    const errors = validateSignals({
      "0": [sig("a", "3:0"), sig("b", "2"), sig("a", "8"), sig("", "9"),
            { name: "wide", lsb: 12, width: 8, format: "hex", enum: {} }, sig("e", "11:10", "enum", "4=X")],
    });
    expect(errors.map(e => e.key).sort()).toEqual(
      ["fpgaObs.err.duplicate", "fpgaObs.err.emptyName", "fpgaObs.err.enumRange", "fpgaObs.err.overlap",
       "fpgaObs.err.range"].sort());
    expect(errors.find(e => e.key === "fpgaObs.err.overlap")?.params).toEqual({ group: "0", name: "b" });
  });
  it("デーモン（fpga_signals.py）と同じ検査: 名前は 64 文字まで・列挙の値は 0 以上の整数・列挙の名前は空でない（R17）", () => {
    const errors = validateSignals({
      "0": [sig("x".repeat(65), "0"),
            { name: "neg", lsb: 1, width: 2, format: "enum", enum: { "-1": "A" } },
            { name: "frac", lsb: 3, width: 2, format: "enum", enum: { "1.5": "A" } },
            { name: "blank", lsb: 5, width: 2, format: "enum", enum: { "1": " " } }],
    });
    expect(errors.map(e => e.key).sort()).toEqual(
      ["fpgaObs.err.enumName", "fpgaObs.err.enumValue", "fpgaObs.err.enumValue", "fpgaObs.err.longName"].sort());
  });
});

describe("TCK と半周期", () => {
  it("sys 100 MHz: 10 MHz → 半周期 5、12.5 MHz → 4、上限は半周期 2（25 MHz）・下限 65535", () => {
    expect(tckToHalfPeriod(10, 100e6)).toEqual({ half: 5, mhz: 10 });
    expect(tckToHalfPeriod(12.5, 100e6)).toEqual({ half: 4, mhz: 12.5 });
    // 欄が空・0 以下なら無し（NaN の半周期を接続に渡さない。R17）
    for (const bad of [NaN, 0, -1, null as unknown as number]) expect(tckToHalfPeriod(bad, 100e6)).toBeNull();
    expect(tckToHalfPeriod(30, 100e6)).toEqual({ half: 2, mhz: 25 });
    expect(tckToHalfPeriod(0.0001, 100e6)!.half).toBe(65535);
    expect(tckToHalfPeriod(7, 100e6)).toEqual({ half: 8, mhz: 6.25 });          // 求めた速さを超えない
  });
});

describe("状態の文言", () => {
  it("答えない・溢れ・止まっている・動いている", () => {
    expect(statusKey({ running: false, stuck: true, id_ok: false, overflow_seen: false, driving: false }))
      .toBe("fpgaObs.state.stuck");
    expect(statusKey({ running: true, stuck: false, id_ok: true, overflow_seen: true, driving: true }))
      .toBe("fpgaObs.state.overflow");
    expect(statusKey({ running: true, stuck: false, id_ok: true, overflow_seen: false, driving: true }))
      .toBe("fpgaObs.state.running");
    expect(statusKey({ running: false, stuck: false, id_ok: false, overflow_seen: false, driving: false }))
      .toBe("fpgaObs.state.off");
  });
});

describe("信号の表で編集する組（観測する組とは別）", () => {
  it("定義のある組・申告の組の数・いま選んでいる組の和（観測につながっていなくても過去の記録に名前を付けられる）", () => {
    expect(editableGroups(["2", "0"], null, 0)).toEqual([0, 2]);
    expect(editableGroups([], 3, 0)).toEqual([0, 1, 2]);
    expect(editableGroups(["5"], 2, 7)).toEqual([0, 1, 5, 7]);
  });
});

describe("定義の共有（別の窓・AI の変更を読み直す）", () => {
  it("画面の状態の通知で fpga_signals が変わったときだけ", () => {
    expect(signalsChanged({ changed: ["fpga_signals"] })).toBe(true);
    expect(signalsChanged({ changed: ["view", "cursors"] })).toBe(false);
    expect(signalsChanged({})).toBe(false);
  });
});

describe("観測の回路の型の設定", () => {
  it("記録型は速い TCK と多い語数、標本型（1 クロック版）は遅い TCK・1 語・印なし", async () => {
    const { PRESETS } = await import("../src/lib/fpgaobs");
    expect(PRESETS.stream).toEqual({ tckMhz: 10, words: 64, syncPeriod: 20 });
    expect(PRESETS.snapshot).toEqual({ tckMhz: 2.5, words: 1, syncPeriod: 0 });
  });
});

describe("開いた設定を画面の欄へ", () => {
  it("デーモンが覚えている設定（fpga_obs.status の config）を欄の値にする（TCK は半周期から）", async () => {
    const { configToFields, tckToHalfPeriod } = await import("../src/lib/fpgaobs");
    const f = configToFields({ chain: { ir_len: 6, ir_before: 4, dr_after: 1 }, user_ir: 0x22, half_period: 20, words: 1,
                               sync_period: 0 }, 100e6);
    expect(f).toEqual({ irLen: 6, irBefore: 4, irAfter: 0, drBefore: 0, drAfter: 1, userIr: "0x22", tckMhz: 2.5,
                        words: 1, syncPeriod: 0 });
    for (let half = 2; half <= 200; half++) {                            // 欄から開き直すと同じ半周期（丸めの誤差でずれない）
      const back = configToFields({ chain: {}, user_ir: 2, half_period: half, words: 1, sync_period: 0 }, 100e6);
      expect(tckToHalfPeriod(back.tckMhz, 100e6)!.half).toBe(half);
    }
    expect(configToFields({ chain: null, user_ir: 2, half_period: 3, words: 64, sync_period: 20 }, 100e6).irLen).toBe(4);
  });
});
